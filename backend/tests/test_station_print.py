import json
import os
import sys
import tempfile
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp",
    MANIFESTS_DIR="/tmp"))

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import printer  # noqa: E402
import shipments_api  # noqa: E402

SERVER = {"print_mode": "network", "printer_host": "10.0.0.5", "printer_port": "9100", "printer_dpi": "203"}
STATION_PRINTER = {"mode": "network", "host": "10.0.0.9", "port": "9105", "dpi": "300"}


class StationPrintTest(unittest.TestCase):
    """Labels print the way the station that asked is set up, not the way the
    server was once set up for everyone."""

    def setUp(self):
        self.addCleanup(setattr, auth, "user_ships", auth.user_ships)
        auth.user_ships = lambda user_id, role: True
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(shipments_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"], sess["role"] = 1, "user"
        fd, self.label_path = tempfile.mkstemp(suffix=".pdf")
        os.write(fd, b"%PDF-1.4 label")
        os.close(fd)
        self.addCleanup(os.remove, self.label_path)
        self.printed = []
        self.dpis = []
        db = shipments_api.db
        self._orig = (db.query, db.get_setting, printer.print_label, shipments_api.labels.to_zpl,
                      shipments_api.audit, shipments_api._group_label_bytes)
        row = {"id": 7, "label_path": self.label_path, "label_format": "pdf"}
        db.query = lambda sql, params=None, **kw: row if kw.get("one") else [row]
        db.get_setting = lambda key, default=None: SERVER.get(key, default)
        printer.print_label = lambda data, fmt, cfg: self.printed.append((data, fmt, cfg))
        shipments_api.labels.to_zpl = lambda data, fmt, dpi: self.dpis.append(dpi) or b"^XA^XZ"
        shipments_api.audit = lambda *a, **k: None
        shipments_api._group_label_bytes = lambda group_id: (b"LABELS", "pdf")

    def tearDown(self):
        db = shipments_api.db
        (db.query, db.get_setting, printer.print_label, shipments_api.labels.to_zpl,
         shipments_api.audit, shipments_api._group_label_bytes) = self._orig

    def test_a_bought_group_prints_on_the_station_network_printer(self):
        result = shipments_api._print_group("g1", printer.station_config(STATION_PRINTER))
        self.assertEqual((result, self.printed), ("ok", [
            (b"LABELS", "pdf", {"mode": "network", "host": "10.0.0.9", "port": 9105, "dpi": 300})]))

    def test_a_station_printing_from_the_browser_gets_nothing_from_the_server(self):
        for mode in ("browser", "browserprint"):
            with self.subTest(mode=mode):
                result = shipments_api._print_group("g1", printer.station_config({**STATION_PRINTER, "mode": mode}))
                self.assertEqual((result, self.printed), (None, []))

    def test_reprint_goes_to_the_printer_the_station_sent(self):
        res = self.client.post("/api/shipments/7/print", json=STATION_PRINTER)
        self.assertEqual((res.status_code, [p[2] for p in self.printed]), (200, [
            {"mode": "network", "host": "10.0.0.9", "port": 9105, "dpi": 300}]))

    def test_group_reprint_goes_to_the_printer_the_station_sent(self):
        res = self.client.post("/api/shipments/group/g1/print", json=STATION_PRINTER)
        self.assertEqual((res.status_code, [p[2]["host"] for p in self.printed]), (200, ["10.0.0.9"]))

    def test_a_zpl_label_is_drawn_at_the_station_printer_resolution(self):
        self.client.get("/api/shipments/7/label?format=zpl&dpi=300")
        self.client.get("/api/shipments/7/label?format=zpl")
        self.assertEqual(self.dpis, [300, 203])


if __name__ == "__main__":
    unittest.main()

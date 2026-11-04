import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

import printer  # noqa: E402
from providers import labels  # noqa: E402

SERVER = {"print_mode": "network", "printer_host": "10.0.0.5", "printer_port": "9101", "printer_dpi": "300"}
STATION = {"mode": "browserprint", "host": "10.0.0.9", "port": "9100", "dpi": "203"}


class StationConfigTest(unittest.TestCase):
    """A station's printing setup comes with each request; a field an older
    open tab leaves out falls back to the shared setup from before."""

    def setUp(self):
        self.addCleanup(setattr, printer.db, "get_setting", printer.db.get_setting)
        printer.db.get_setting = lambda key, default=None: SERVER.get(key, default)

    def test_a_full_station_setup_ignores_the_server_one(self):
        self.assertEqual(printer.station_config(STATION),
                         {"mode": "browserprint", "host": "10.0.0.9", "port": 9100, "dpi": 203})

    def test_no_station_setup_is_the_old_shared_one(self):
        expected = {"mode": "network", "host": "10.0.0.5", "port": 9101, "dpi": 300}
        for raw in (None, {}, "junk"):
            with self.subTest(raw=raw):
                self.assertEqual(printer.station_config(raw), expected)

    def test_a_blank_host_sent_by_the_station_stays_blank(self):
        self.assertEqual(printer.station_config({**STATION, "host": "  "})["host"], "")

    def test_unknown_values_fall_back_to_safe_defaults(self):
        self.assertEqual(printer.station_config({"mode": "fax", "host": " 10.0.0.9 ", "port": "",
                                                 "dpi": "600"}),
                         {"mode": "browser", "host": "10.0.0.9", "port": 9100, "dpi": 203})

    def test_a_port_that_is_not_a_tcp_port_is_kept_for_the_print_to_refuse(self):
        for port in ("abc", "70000", "0"):
            with self.subTest(port=port):
                with self.assertRaises(printer.PrinterError):
                    printer.network_print(b"^XA^XZ", "10.0.0.9", printer.station_config(
                        {**STATION, "port": port})["port"])


class PrintLabelTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.converted = []
        connections = self.sent

        class FakeSocket:
            def __init__(self, address, timeout):
                self.address = address

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def sendall(self, data):
                connections.append((self.address, data))

        self.addCleanup(setattr, printer.socket, "create_connection", printer.socket.create_connection)
        self.addCleanup(setattr, labels, "to_zpl", labels.to_zpl)
        printer.socket.create_connection = FakeSocket
        labels.to_zpl = lambda data, fmt, dpi: self.converted.append((fmt, dpi)) or b"^XA" + data + b"^XZ"

    def test_the_label_goes_to_the_station_printer_at_its_resolution(self):
        printer.print_label(b"PDF", "pdf", {"mode": "network", "host": "10.0.0.9", "port": 9105, "dpi": 300})
        self.assertEqual((self.converted, self.sent), ([("pdf", 300)], [(("10.0.0.9", 9105), b"^XAPDF^XZ")]))

    def test_a_station_without_a_printer_address_is_told_where_to_set_it(self):
        with self.assertRaisesRegex(printer.PrinterError, "Settings → Printing"):
            printer.print_label(b"PDF", "pdf", {"mode": "network", "host": "", "port": 9100, "dpi": 203})
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()

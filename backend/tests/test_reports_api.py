import json
import sys
import types
import unittest
from datetime import date

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp", MANIFESTS_DIR="/tmp"))

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import backoffice  # noqa: E402
import reports  # noqa: E402
import reports_api  # noqa: E402

RANGE = "from=2026-11-01&to=2026-11-07"
PARCELS = [
    {"source": "parcels", "date": date(2026, 11, 2), "store": "Shop A", "carrier": "UPS", "service": "Ground",
     "ref": "#1001", "tracking": "1Z1", "cost": 12.5, "shipment": "g1"},
]
INVOICES = [
    {"invoice_id": 7, "invoice_number": "INV7", "business_name": "Acme", "tracking": "1Z1",
     "cost": 12.5, "date": date(2026, 11, 2), "carrier": "ups"},     # EasyShip shipped it
    {"invoice_id": 8, "invoice_number": "INV8", "business_name": "Beta", "tracking": "",
     "cost": 20.0, "date": date(2026, 11, 3), "carrier": "ups"},     # shipped elsewhere
]


class ReportsApiTest(unittest.TestCase):
    """The Reports page reads both sources through /api/reports; a BackOffice
    problem never fails it, and only users with the Reports section get in."""

    def setUp(self):
        saved = [(auth, "user_sections"), (reports, "parcel_spend_rows"), (reports, "parcel_matches"),
                 (reports, "reports_db_id"), (backoffice, "get_db_config"),
                 (backoffice, "shipping_spend_invoices")]
        for mod, name in saved:
            self.addCleanup(setattr, mod, name, getattr(mod, name))
        reports_api._cache.clear()
        self.sections = ["reports"]
        auth.user_sections = lambda user_id, role: self.sections
        reports.parcel_spend_rows = lambda f, t: [dict(r) for r in PARCELS]
        reports.parcel_matches = lambda db_id, ids, tracking: ({"1Z1"}, set())
        reports.reports_db_id = lambda: 3
        backoffice.get_db_config = lambda db_id: {"name": "BackOffice Main"}
        backoffice.shipping_spend_invoices = lambda db_id, f, t: [dict(i) for i in INVOICES]
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(reports_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "user"

    def get(self, path):
        res = self.client.get(path)
        return res.status_code, res

    def test_needs_the_reports_section(self):
        self.sections = ["verify"]
        status, res = self.get(f"/api/reports/spend?{RANGE}")
        self.assertEqual((status, res.get_json()), (403, {"error": "You don't have access to Reports"}))

    def test_bad_ranges_are_refused(self):
        cases = ["", "from=2026-11-07&to=2026-11-01", "from=2024-01-01&to=2026-01-01",
                 f"{RANGE}&bucket=year", "from=11/01/2026&to=2026-11-07"]
        self.assertEqual([self.get(f"/api/reports/spend?{q}")[1].get_json()["error"] for q in cases],
                         ["Choose a start and end date", "The start date is after the end date",
                          "Choose a range of two years or less", "Group by day, week or month",
                          "Choose a start and end date"])

    def test_summary_merges_sources_and_skips_the_duplicate(self):
        status, res = self.get(f"/api/reports/spend?{RANGE}&bucket=week")
        body = res.get_json()
        self.assertEqual((status, body["totals"], body["by_carrier"], body["options"]["stores"], body["notices"]),
                         (200, {"cost": 32.5, "shipments": 2, "avg": 16.25},
                          [{"name": "UPS", "cost": 32.5, "shipments": 2, "avg": 16.25, "share": 100.0}],
                          ["BackOffice Main", "Shop A"], []))

    def test_filters_apply_to_the_summary_but_not_the_options(self):
        _, res = self.get(f"/api/reports/spend?{RANGE}&store=backoffice%20main")
        body = res.get_json()
        self.assertEqual((body["totals"]["cost"], body["options"]["stores"]), (20.0, ["BackOffice Main", "Shop A"]))

    def test_backoffice_down_still_answers_with_the_parcel_log(self):
        def down(db_id, f, t):
            raise backoffice.BackofficeError("connection refused")
        backoffice.shipping_spend_invoices = down
        _, res = self.get(f"/api/reports/spend?{RANGE}")
        body = res.get_json()
        self.assertEqual((body["totals"]["cost"], body["notices"]),
                         (12.5, ["BackOffice could not be read (connection refused): showing the parcel log only."]))

    def test_no_backoffice_chosen(self):
        reports.reports_db_id = lambda: None
        _, res = self.get(f"/api/reports/spend?{RANGE}")
        self.assertEqual(res.get_json()["notices"],
                         ["No BackOffice database is chosen for Reports (Settings › Reports): "
                          "showing the parcel log only."])

    def test_rows_are_paged_and_sorted(self):
        _, res = self.get(f"/api/reports/spend/rows?{RANGE}&sort=cost&dir=desc&limit=1")
        self.assertEqual(res.get_json(), {
            "rows": [{"date": "2026-11-03", "source": "backoffice", "store": "BackOffice Main", "carrier": "UPS",
                      "service": "", "ref": "INV8", "tracking": "", "cost": 20.0}],
            "total": 2, "offset": 0, "limit": 1})

    def test_csv_exports(self):
        _, rows = self.get(f"/api/reports/spend.csv?{RANGE}&kind=rows&sort=date&dir=asc")
        _, carriers = self.get(f"/api/reports/spend.csv?{RANGE}&kind=carrier")
        self.assertEqual(
            (rows.headers["Content-Disposition"], rows.get_data(as_text=True).splitlines(),
             carriers.get_data(as_text=True).splitlines()),
            ('attachment; filename="shipping-spend-rows-2026-11-01-to-2026-11-07.csv"',
             ["﻿Date,Source,Store,Carrier,Service,Reference,Tracking,Cost",
              "2026-11-02,Parcel log,Shop A,UPS,Ground,#1001,1Z1,12.50",
              "2026-11-03,BackOffice,BackOffice Main,UPS,,INV8,,20.00"],
             ["﻿Carrier,Shipments,Spend,Average,Share %", "UPS,2,32.50,16.25,100.0"]))

    def test_the_backoffice_read_is_reused_for_a_minute(self):
        calls = []
        backoffice.shipping_spend_invoices = lambda db_id, f, t: calls.append(1) or []
        self.get(f"/api/reports/spend?{RANGE}")
        self.get(f"/api/reports/spend/rows?{RANGE}")
        self.get(f"/api/reports/spend?{RANGE}&fresh=1")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

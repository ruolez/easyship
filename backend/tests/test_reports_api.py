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
     "ref": "#1001", "tracking": "1Z1", "cost": 12.5, "shipment": "g1", "sales_rep": "",
     "bo_db_id": None, "invoice_id": None},
]
INVOICES = [
    {"invoice_id": 7, "invoice_number": "INV7", "business_name": "Acme", "tracking": "1Z1",
     "cost": 12.5, "date": date(2026, 11, 2), "carrier": "ups", "sales_rep": "Anna"},  # EasyShip shipped it
    {"invoice_id": 8, "invoice_number": "INV8", "business_name": "Beta", "tracking": "",
     "cost": 20.0, "date": date(2026, 11, 3), "carrier": "ups", "sales_rep": "Bob"},   # shipped elsewhere
]


SHEET_LINK = "https://docs.google.com/spreadsheets/d/12iZSxRc0fBBfhNFu8QQxwWgR78YrywUAeXAnz-kRKb8/edit?gid=5"
SHEET = """Order ID,TRACKING_COMPANY,Track Your Package,TRACK NUM,TRACKING_URL,Shipping Cost,DATE
TS1,UPS,Track Your Package,1z1,x,12.50,11/2/2026
TS2,SpeeDee,Track Your Package,SP1,x,9.00,11/4/2026
TS2,SpeeDee,Track Your Package,SP2,x,8.00,11/4/2026
TS3,,Track Your Package,,N/A,30.00,11/5/2026
TS4,UPS,Track Your Package,,x,7.00,11/5/2026
TS5,UPS,Track Your Package,1Z9,x,5.00,12/1/2026
"""


class ReportsApiTest(unittest.TestCase):
    """The Reports page reads both sources through /api/reports; a BackOffice
    problem never fails it, and only users with the Reports section get in."""

    def setUp(self):
        saved = [(auth, "user_sections"), (reports, "parcel_spend_rows"), (reports, "parcel_matches"),
                 (reports, "reports_db_id"), (backoffice, "get_db_config"),
                 (backoffice, "shipping_spend_invoices"), (backoffice, "invoice_sales_reps"),
                 (reports, "sheet_link"), (reports, "fetch_sheet"), (reports, "sheet_stores"),
                 (reports, "parcel_order_matches")]
        for mod, name in saved:
            self.addCleanup(setattr, mod, name, getattr(mod, name))
        reports_api._cache.clear()
        reports._sheet_cache.clear()
        self.sections = ["reports"]
        auth.user_sections = lambda user_id, role: self.sections
        reports.parcel_spend_rows = lambda f, t: [dict(r) for r in PARCELS]
        reports.parcel_matches = lambda db_id, ids, tracking: ({"1Z1"}, set())
        reports.reports_db_id = lambda: 3
        backoffice.get_db_config = lambda db_id: {"name": "BackOffice Main"}
        backoffice.shipping_spend_invoices = lambda db_id, f, t: [dict(i) for i in INVOICES]
        backoffice.invoice_sales_reps = lambda db_id, ids: {}
        reports.sheet_link = lambda: ""
        reports.fetch_sheet = lambda url: SHEET
        reports.sheet_stores = lambda: [{"name": "Shop A", "prefix": "TS"}]
        reports.parcel_order_matches = lambda tracking, orders: ({"1Z1"} & set(tracking), {"TS3"} & set(orders))
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
                         (12.5, ["BackOffice could not be read (connection refused): its invoices are left out."]))

    def test_no_backoffice_chosen(self):
        reports.reports_db_id = lambda: None
        _, res = self.get(f"/api/reports/spend?{RANGE}")
        self.assertEqual(res.get_json()["notices"],
                         ["No BackOffice database is chosen for Reports (Settings › Reports): "
                          "its invoices are left out."])

    def test_rows_are_paged_and_sorted(self):
        _, res = self.get(f"/api/reports/spend/rows?{RANGE}&sort=cost&dir=desc&limit=1")
        self.assertEqual(res.get_json(), {
            "rows": [{"date": "2026-11-03", "source": "backoffice", "store": "BackOffice Main", "carrier": "UPS",
                      "service": "", "ref": "INV8", "tracking": "", "cost": 20.0, "sales_rep": "Bob"}],
            "total": 2, "offset": 0, "limit": 1})

    def test_csv_exports(self):
        _, rows = self.get(f"/api/reports/spend.csv?{RANGE}&kind=rows&sort=date&dir=asc")
        _, carriers = self.get(f"/api/reports/spend.csv?{RANGE}&kind=carrier")
        self.assertEqual(
            (rows.headers["Content-Disposition"], rows.get_data(as_text=True).splitlines(),
             carriers.get_data(as_text=True).splitlines()),
            ('attachment; filename="shipping-spend-rows-2026-11-01-to-2026-11-07.csv"',
             ["﻿Date,Source,Store,Sales rep,Carrier,Service,Reference,Tracking,Cost",
              "2026-11-02,Parcel log,Shop A,No sales rep,UPS,Ground,#1001,1Z1,12.50",
              "2026-11-03,BackOffice,BackOffice Main,Bob,UPS,,INV8,,20.00"],
             ["﻿Carrier,Shipments,Spend,Average,Share %", "UPS,2,32.50,16.25,100.0"]))

    def test_parcels_for_invoices_of_the_reports_database_carry_the_invoice_sales_rep(self):
        parcels = PARCELS + [
            {**PARCELS[0], "store": "BackOffice Main", "ref": "INV9", "tracking": "9400", "cost": 7.0,
             "shipment": "g2", "bo_db_id": 3, "invoice_id": 9},       # the Reports database's invoice
            {**PARCELS[0], "store": "Other BO", "ref": "INV9", "tracking": "9401", "cost": 3.0,
             "shipment": "g3", "bo_db_id": 4, "invoice_id": 9},       # same id, another database
        ]
        asked = []
        reports.parcel_spend_rows = lambda f, t: [dict(r) for r in parcels]
        backoffice.invoice_sales_reps = lambda db_id, ids: asked.append((db_id, sorted(ids))) or {9: "anna"}
        _, all_reps = self.get(f"/api/reports/spend?{RANGE}")
        _, anna = self.get(f"/api/reports/spend/rows?{RANGE}&rep=Anna")
        self.assertEqual(
            (asked, all_reps.get_json()["options"]["reps"],
             [(r["ref"], r["sales_rep"]) for r in anna.get_json()["rows"]]),
            ([(3, [9])], ["anna", "Bob", "No sales rep"], [("INV9", "anna")]))

    def test_the_google_sheet_adds_what_the_other_sources_do_not_have(self):
        reports.sheet_link = lambda: SHEET_LINK
        fetched = []
        reports.fetch_sheet = lambda url: fetched.append(url) or SHEET
        _, res = self.get(f"/api/reports/spend?{RANGE}&source=sheet")
        _, rows = self.get(f"/api/reports/spend/rows?{RANGE}&source=sheet&sort=date&dir=asc")
        body = res.get_json()
        self.assertEqual(
            (fetched, body["totals"], body["by_carrier"], body["options"]["sources"][-1],
             [(r["ref"], r["store"], r["tracking"], r["sales_rep"]) for r in rows.get_json()["rows"]]),
            (["https://docs.google.com/spreadsheets/d/12iZSxRc0fBBfhNFu8QQxwWgR78YrywUAeXAnz-kRKb8"
              "/export?format=csv&gid=5"],
             {"cost": 24.0, "shipments": 2, "avg": 12.0},
             [{"name": "SpeeDee", "cost": 17.0, "shipments": 1, "avg": 17.0, "share": 70.8},
              {"name": "UPS", "cost": 7.0, "shipments": 1, "avg": 7.0, "share": 29.2}],
             {"value": "sheet", "label": "Google Sheet"},
             [("TS2", "Shop A", "SP1", "No sales rep"), ("TS2", "Shop A", "SP2", "No sales rep"),
              ("TS4", "Shop A", "", "No sales rep")]))

    def test_a_sheet_tracking_number_backoffice_already_has_is_skipped(self):
        reports.sheet_link = lambda: SHEET_LINK
        reports.fetch_sheet = lambda url: SHEET.replace("SP2", "INV8-TRACK")
        backoffice.shipping_spend_invoices = lambda db_id, f, t: [
            {**INVOICES[1], "tracking": "inv8-track"}]
        _, res = self.get(f"/api/reports/spend/rows?{RANGE}&source=sheet")
        self.assertEqual(sorted(r["tracking"] for r in res.get_json()["rows"]), ["", "SP1"])

    def test_an_unreadable_sheet_leaves_the_other_sources(self):
        reports.sheet_link = lambda: SHEET_LINK

        def private(url):
            raise reports.SheetError("it isn't shared with “Anyone with the link”")
        reports.fetch_sheet = private
        _, res = self.get(f"/api/reports/spend?{RANGE}")
        body = res.get_json()
        self.assertEqual((body["totals"]["cost"], body["notices"]),
                         (32.5, ["The Google Sheet could not be read (it isn't shared with "
                                 "“Anyone with the link”): its rows are left out."]))

    def test_the_sheet_download_is_shared_by_ranges_until_refresh(self):
        reports.sheet_link = lambda: SHEET_LINK
        fetched = []
        reports.fetch_sheet = lambda url: fetched.append(url) or SHEET
        self.get(f"/api/reports/spend?{RANGE}")
        self.get("/api/reports/spend?from=2026-11-01&to=2026-11-30")
        self.get(f"/api/reports/spend?{RANGE}&fresh=1")
        self.assertEqual(len(fetched), 2)

    def test_the_backoffice_read_is_reused_for_a_minute(self):
        calls = []
        backoffice.shipping_spend_invoices = lambda db_id, f, t: calls.append(1) or []
        self.get(f"/api/reports/spend?{RANGE}")
        self.get(f"/api/reports/spend/rows?{RANGE}")
        self.get(f"/api/reports/spend?{RANGE}&fresh=1")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

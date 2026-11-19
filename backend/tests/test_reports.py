import sys
import types
import unittest
from datetime import date

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp", MANIFESTS_DIR="/tmp"))

import backoffice  # noqa: E402
import reports  # noqa: E402

D = date.fromisoformat


def row(source="parcels", day="2026-11-02", store="Main", carrier="USPS", cost=10.0, shipment=None, **kw):
    return {"source": source, "date": D(day), "store": store, "carrier": carrier, "service": "",
            "ref": "R", "tracking": "", "cost": cost, "shipment": shipment or f"{source}:{day}:{cost}", **kw}


def invoice(invoice_id, tracking=""):
    return {"invoice_id": invoice_id, "tracking": tracking}


class PeriodStartTest(unittest.TestCase):
    def test_weeks_start_monday_and_months_on_the_first(self):
        cases = [("2026-11-08", "week", "2026-11-02"),   # Sunday → its Monday
                 ("2026-11-09", "week", "2026-11-09"),   # Monday stays
                 ("2027-01-01", "week", "2026-12-28"),   # across the year end
                 ("2026-11-30", "month", "2026-11-01"),
                 ("2026-11-30", "day", "2026-11-30")]
        self.assertEqual([reports.period_start(D(d), b).isoformat() for d, b, _ in cases],
                         [want for _, _, want in cases])

    def test_next_month_handles_month_ends(self):
        self.assertEqual([reports.next_period(D(d), "month").isoformat()
                          for d in ("2026-01-01", "2026-02-01", "2026-12-01")],
                         ["2026-02-01", "2026-03-01", "2027-01-01"])


class CarrierNameTest(unittest.TestCase):
    def test_postage_providers_and_accounts_become_the_carrier(self):
        cases = [("Endicia", "USPS"), ("Stamps.com", "USPS"), ("stamps_com", "USPS"),
                 ("USPS by ShipStation", "USPS"), ("UPS by ShipStation", "UPS"), ("FedEx One Balance", "FedEx"),
                 ("UPS · East", "UPS"), ("Endicia · Warehouse 2", "USPS"),
                 ("Spee-Dee", "Spee-Dee"), ("ESTES", "ESTES"), ("", ""), (None, "")]
        self.assertEqual([reports.carrier_name(raw) for raw, _ in cases], [want for _, want in cases])


class MergeNamesTest(unittest.TestCase):
    def test_an_empty_name_can_have_its_own_label(self):
        rows = [row(sales_rep=""), row(sales_rep=" anna "), row("backoffice", sales_rep="Anna")]
        self.assertEqual([r["sales_rep"] for r in reports.merge_names(rows, "sales_rep", empty=reports.NO_REP)],
                         ["No sales rep", "anna", "anna"])

    def test_case_and_spaces_merge_and_the_parcel_spelling_wins(self):
        rows = [row("backoffice", carrier=" ups "), row(carrier="UPS"), row("backoffice", carrier=""),
                row("backoffice", carrier="Fed Ex"), row(carrier=None)]
        self.assertEqual([r["carrier"] for r in reports.merge_names(rows, "carrier")],
                         ["UPS", "UPS", "Unknown", "Fed Ex", "Unknown"])


class DropDuplicatesTest(unittest.TestCase):
    def test_only_invoices_already_in_the_parcel_log_go(self):
        invoices = [
            invoice(1, "1Z999"),       # EasyShip wrote this tracking number back
            invoice(2, "1z888"),       # same number, other case
            invoice(3, "9400OLD"),     # re-ship: the invoice keeps its earlier, real shipment
            invoice(4, ""),            # no tracking but a parcel for the invoice
            invoice(5, ""),            # shipped outside EasyShip
        ]
        kept = reports.drop_duplicates(invoices, {"1Z999", "1Z888", None}, {3, 4})
        self.assertEqual([i["invoice_id"] for i in kept], [3, 5])


class FilterRowsTest(unittest.TestCase):
    def test_each_filter_narrows_and_empty_means_all(self):
        rows = [row(carrier="UPS", store="A", sales_rep="Anna"),
                row("backoffice", carrier="USPS", store="B", sales_rep="Bob"),
                row(carrier="USPS", store="A", sales_rep="No sales rep")]
        self.assertEqual(
            [len(reports.filter_rows(rows)), len(reports.filter_rows(rows, carriers=["usps"])),
             len(reports.filter_rows(rows, carriers=["USPS"], stores=["A"])),
             len(reports.filter_rows(rows, sources=["backoffice"])),
             len(reports.filter_rows(rows, reps=["anna", "No sales rep"])),
             len(reports.filter_rows(rows, carriers=["USPS"], reps=["Bob"]))],
            [3, 2, 1, 1, 2, 1])


class SummarizeTest(unittest.TestCase):
    def test_totals_breakdowns_and_every_period(self):
        rows = [
            row(day="2026-11-02", carrier="UPS", store="A", cost=10.0, shipment="g1"),
            row(day="2026-11-02", carrier="UPS", store="A", cost=5.0, shipment="g1"),  # second box
            row("backoffice", day="2026-11-04", carrier="USPS", store="B", cost=15.0, shipment="bo:7"),
        ]
        result = reports.summarize(rows, "day", D("2026-11-01"), D("2026-11-04"))
        self.assertEqual(result, {
            "totals": {"cost": 30.0, "shipments": 2, "avg": 15.0},
            "by_carrier": [{"name": "UPS", "cost": 15.0, "shipments": 1, "avg": 15.0, "share": 50.0},
                           {"name": "USPS", "cost": 15.0, "shipments": 1, "avg": 15.0, "share": 50.0}],
            "by_store": [{"name": "A", "cost": 15.0, "shipments": 1, "avg": 15.0, "share": 50.0},
                         {"name": "B", "cost": 15.0, "shipments": 1, "avg": 15.0, "share": 50.0}],
            "by_period": [
                {"period": "2026-11-01", "cost": 0.0, "shipments": 0, "carriers": {}, "stores": {}},
                {"period": "2026-11-02", "cost": 15.0, "shipments": 1, "carriers": {"UPS": 15.0},
                 "stores": {"A": 15.0}},
                {"period": "2026-11-03", "cost": 0.0, "shipments": 0, "carriers": {}, "stores": {}},
                {"period": "2026-11-04", "cost": 15.0, "shipments": 1, "carriers": {"USPS": 15.0},
                 "stores": {"B": 15.0}},
            ],
        })

    def test_periods_add_up_to_the_total(self):
        rows = [row(day=f"2026-{m:02d}-{d:02d}", cost=float(m + d)) for m in (1, 2, 3) for d in (1, 15, 28)]
        for bucket in reports.BUCKETS:
            result = reports.summarize(rows, bucket, D("2026-01-01"), D("2026-03-31"))
            self.assertAlmostEqual(sum(p["cost"] for p in result["by_period"]), result["totals"]["cost"])

    def test_nothing_shipped(self):
        self.assertEqual(reports.summarize([], "month", D("2026-11-05"), D("2026-11-20"))["totals"],
                         {"cost": 0, "shipments": 0, "avg": 0.0})


class SheetCsvUrlTest(unittest.TestCase):
    def test_the_tab_link_becomes_its_csv_export(self):
        sheet = "12iZSxRc0fBBfhNFu8QQxwWgR78YrywUAeXAnz-kRKb8"
        cases = [f"https://docs.google.com/spreadsheets/d/{sheet}/edit?gid=156688159#gid=156688159",
                 f"https://docs.google.com/spreadsheets/d/{sheet}/edit",
                 "https://example.com/spreadsheets/d/abc", "", None]
        export = f"https://docs.google.com/spreadsheets/d/{sheet}/export?format=csv&gid="
        self.assertEqual([reports.sheet_csv_url(c) for c in cases],
                         [export + "156688159", export + "0", None, None, None])


STORES = [{"name": "TStock", "prefix": "TS"}, {"name": "TStock Wholesale", "prefix": "TSW"},
          {"name": "Pipe World", "prefix": "#PW"}]
SHEET = """Order ID,TRACKING_COMPANY,Track Your Package,TRACK NUM,TRACKING_URL,Shipping Cost,DATE,ORDER TOTAL
TSW7546,SpeeDee,Track Your Package,SP034,https://x,16.65,11/2/2026,
"TSW7570
",,Track Your Package,5924.110626,N/A,$47.13,11/6/2026,
TS8390997,Stamps.com,1Z999,,https://x,"1,015.92",11/09/2026,
pw116447,UPS,Track Your Package,1Z1,https://x,17.89,11/9/2026,
A2Z100011001,UPS,Track Your Package,1Z2,https://x,13.53,11/9/2026,
TSW7571,UPS,Track Your Package,1Z3,https://x,,11/9/2026,
TSW7572,UPS,Track Your Package,1Z4,https://x,0,11/9/2026,
TSW7573,UPS,Track Your Package,1Z5,https://x,12.00,,
,,,,N/A,,,
"""


class ParseSheetTest(unittest.TestCase):
    def test_rows_with_a_cost_and_a_date_become_spend_rows(self):
        rows = reports.parse_sheet(SHEET, STORES)
        self.assertEqual(
            [(r["date"].isoformat(), r["store"], r["carrier"], r["ref"], r["tracking"], r["cost"], r["shipment"])
             for r in rows],
            [("2026-11-02", "TStock Wholesale", "SpeeDee", "TSW7546", "SP034", 16.65, "sheet:TSW7546"),
             ("2026-11-06", "TStock Wholesale", "", "TSW7570", "5924.110626", 47.13, "sheet:TSW7570"),
             ("2026-11-09", "TStock", "USPS", "TS8390997", "1Z999", 1015.92, "sheet:TS8390997"),
             ("2026-11-09", "Pipe World", "UPS", "pw116447", "1Z1", 17.89, "sheet:PW116447"),
             ("2026-11-09", "", "UPS", "A2Z100011001", "1Z2", 13.53, "sheet:A2Z100011001")])

    def test_a_sheet_without_the_needed_columns_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Shipping Cost, DATE"):
            reports.parse_sheet("Order ID,TRACK NUM\nTS1,1Z\n", STORES)


class SheetDuplicatesTest(unittest.TestCase):
    def test_tracking_matches_go_and_untracked_orders_go_when_shipped_here(self):
        rows = [{"tracking": "1z1", "order_key": "TS1"}, {"tracking": "1Z2", "order_key": "TS2"},
                {"tracking": "", "order_key": "TS3"}, {"tracking": "", "order_key": "TS4"}]
        kept = reports.drop_duplicates(rows, {"1Z1"}, {"TS3", "TS2"}, key="order_key")
        self.assertEqual([r["order_key"] for r in kept], ["TS2", "TS4"])


class ShipperNameColumnTest(unittest.TestCase):
    def test_known_names_first_then_first_text_column(self):
        cases = [
            [("ShipperID", "int"), ("Shipper", "nvarchar")],
            [("ShipperID", "int"), ("Code", "nvarchar"), ("ShipperName", "nvarchar")],
            [("ShipperID", "int"), ("AccountID", "nvarchar"), ("Carrier", "varchar")],
            [("ShipperID", "int"), ("Rate", "money")],
            [("ID", "int"), ("Shipper", "nvarchar")],
            [],
        ]
        self.assertEqual([backoffice.pick_shipper_name_column(c) for c in cases],
                         ["Shipper", "ShipperName", "Carrier", None, None, None])


if __name__ == "__main__":
    unittest.main()

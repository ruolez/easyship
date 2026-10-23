import sys
import types
import unittest
from datetime import datetime

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp", MANIFESTS_DIR="/tmp"))

import picklists  # noqa: E402
import shopify_client  # noqa: E402

GID = "gid://shopify/Order/1"
STORE = 3


def line(title="Widget", barcode="0001", qty=1, unfulfilled=None, shipping=True):
    return {"title": title, "sku": f"SKU-{title}", "quantity": qty,
            "unfulfilledQuantity": qty if unfulfilled is None else unfulfilled,
            "requiresShipping": shipping, "variant": {"barcode": barcode}}


def node(lines=(line(),), financial="PAID", fulfillment="UNFULFILLED", fo_status="OPEN", gid=GID, name="#1001", **over):
    base = {
        "id": gid, "name": name, "createdAt": "2026-10-20T15:00:00Z", "cancelledAt": None,
        "displayFinancialStatus": financial, "displayFulfillmentStatus": fulfillment,
        "fulfillmentOrders": {"nodes": [{"status": fo_status}]},
        "tags": ["wholesale"], "customer": {"displayName": "Ann Smith", "tags": ["vip"]},
        "shippingAddress": {"name": "Ann Smith", "address1": "12 Elm St", "address2": "Apt 4", "city": "Austin",
                            "provinceCode": "TX", "zip": "78701", "countryCodeV2": "US"},
        "totalPriceSet": {"shopMoney": {"amount": "42.00"}}, "shippingLine": {"title": "UPS Ground"},
        "risk": {"recommendation": "ACCEPT", "assessments": [{"riskLevel": "LOW"}]},
        "lineItems": {"nodes": list(lines)},
    }
    base.update(over)
    return base


def order(**kw):
    return picklists.from_shopify_order(node(**kw), STORE)


class FromShopifyOrderTest(unittest.TestCase):
    def test_shape(self):
        self.assertEqual(order(lines=[line(qty=3, unfulfilled=2), line("Gone", unfulfilled=0),
                                      line("Tip", barcode="", shipping=False)]), {
            "store_id": STORE, "id": GID, "name": "#1001", "created_at": "2026-10-20", "cancelled": False,
            "fulfillment_status": "UNFULFILLED", "financial_status": "PAID", "fully_paid": True, "on_hold": False,
            "tags": ["wholesale"], "customer": "Ann Smith", "customer_tags": ["vip"],
            "ship_address1": "12 Elm St", "ship_address2": "Apt 4", "ship_city": "Austin", "ship_zip": "78701",
            "ship_state": "TX", "ship_country": "US", "total": "42.00", "shipping_method": "UPS Ground",
            "risk_level": "LOW", "risk_recommendation": "ACCEPT",
            "lines": [{"title": "Widget", "sku": "SKU-Widget", "barcode": "0001", "qty": 2}],
            "units": 2,
        })


class IneligibleReasonTest(unittest.TestCase):
    def test_reasons(self):
        cases = [
            (order(), set(), None),
            (order(financial="PARTIALLY_REFUNDED"), set(), None),
            (order(), {(STORE, GID)}, "Already on a pick list"),
            (order(cancelledAt="2026-10-21T00:00:00Z"), set(), "Cancelled in Shopify"),
            (order(fo_status="ON_HOLD"), set(), "On hold in Shopify"),
            (order(financial="PENDING"), set(), "Not fully paid"),
            (order(financial="AUTHORIZED"), set(), "Not fully paid"),
            (order(fulfillment="PARTIALLY_FULFILLED"), set(), "Already fulfilled or partially fulfilled"),
            (order(lines=[line(shipping=False)]), set(), "Nothing to pick"),
        ]
        self.assertEqual([picklists.ineligible_reason(o, reserved) for o, reserved, _ in cases],
                         [expected for _, _, expected in cases])

    def test_same_order_id_in_another_store_is_not_reserved(self):
        self.assertEqual(picklists.ineligible_reason(order(), {(STORE + 1, GID)}), None)


class SplitBlockedTest(unittest.TestCase):
    def test_orders_with_unknown_or_missing_barcodes_are_left_out_whole(self):
        good = order(name="#1", gid="g1", lines=[line("A", "111")])
        unknown = order(name="#2", gid="g2", lines=[line("A", "111"), line("B", "999")])
        no_code = order(name="#3", gid="g3", lines=[line("C", "")])
        included, blocked = picklists.split_blocked([good, unknown, no_code], {"111"})
        self.assertEqual(([o["name"] for o in included], blocked), (["#1"], [
            {"store_id": STORE, "id": "g2", "name": "#2", "customer": "Ann Smith", "ship_state": "TX", "units": 2,
             "reasons": ["B — barcode 999 is not in BackOffice"]},
            {"store_id": STORE, "id": "g3", "name": "#3", "customer": "Ann Smith", "ship_state": "TX", "units": 1,
             "reasons": ["C has no barcode in Shopify"]},
        ]))


class GroupLinesTest(unittest.TestCase):
    def test_quantities_add_up_per_barcode_across_orders(self):
        orders = [
            order(name="#1", gid="g1", lines=[line("Zebra", "222", qty=2), line("Apple", "111", qty=1)]),
            order(name="#2", gid="g2", lines=[line("Apple", "111", qty=4)]),
        ]
        items = {"111": {"ProductID": 1010, "ProductSKU": "AP", "ProductDescription": "APPLE 12CT"}}
        self.assertEqual(picklists.group_lines(orders, items), [
            {"barcode": "111", "qty": 5, "title": "Apple", "orders": ["#1", "#2"],
             "product_id": 1010, "sku": "AP", "description": "APPLE 12CT"},
            {"barcode": "222", "qty": 2, "title": "Zebra", "orders": ["#1"],
             "product_id": None, "sku": "", "description": "Zebra"},
        ])


class PicklistSearchTest(unittest.TestCase):
    def test_search_starts_at_the_cutoff_day(self):
        self.assertEqual(
            shopify_client.picklist_search(datetime(2026, 9, 22, 18, 30)),
            "created_at:>=2026-09-22 status:open fulfillment_status:unfulfilled "
            "(financial_status:paid OR financial_status:partially_refunded)")


class NumberPrefixTest(unittest.TestCase):
    def test_zero_padded_date_then_tag(self):
        self.assertEqual(picklists.number_prefix(datetime(2026, 1, 2), "19"), "0102202619")


if __name__ == "__main__":
    unittest.main()

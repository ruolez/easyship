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
        "billingAddress": {"address1": "12 elm st.", "address2": "Apt 4", "city": "AUSTIN",
                           "provinceCode": "TX", "zip": "78701-1234", "countryCodeV2": "US"},
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
            "ship_state": "TX", "ship_country": "US", "po_box": False, "billing_matches_shipping": True,
            "total": "42.00", "shipping_method": "UPS Ground",
            "risk_level": "LOW", "risk_recommendation": "ACCEPT",
            "lines": [{"title": "Widget", "sku": "SKU-Widget", "barcode": "0001", "qty": 2}],
            "units": 2,
        })


    def test_po_box_destination_is_flagged(self):
        address = {"address1": "PO Box 638", "city": "Winsted", "provinceCode": "MN", "zip": "55395"}
        self.assertEqual(order(shippingAddress=address)["po_box"], True)


class OrderProductsTest(unittest.TestCase):
    def test_removed_items_are_left_out_and_default_variant_is_blank(self):
        node = {"lineItems": {"nodes": [
            {"title": "Glow Serum", "variantTitle": "30 ml", "sku": "GS-30", "currentQuantity": 2,
             "unfulfilledQuantity": 2, "requiresShipping": True,
             "originalUnitPriceSet": {"shopMoney": {"amount": "19.50"}},
             "image": {"url": "https://cdn.shopify.com/g.jpg"}, "variant": {"barcode": " 0812 "}},
            {"title": "Gift card", "variantTitle": "Default Title", "currentQuantity": 1, "unfulfilledQuantity": 1,
             "requiresShipping": False, "variant": None, "image": None},
            {"title": "Removed", "currentQuantity": 0, "unfulfilledQuantity": 0},
        ]}}
        self.assertEqual(picklists.order_products(node), [
            {"title": "Glow Serum", "variant": "30 ml", "sku": "GS-30", "barcode": "0812", "qty": 2, "to_ship": 2,
             "ships": True, "price": "19.50", "price_original": None, "cost": None, "margin_pct": None,
             "image": "https://cdn.shopify.com/g.jpg"},
            {"title": "Gift card", "variant": "", "sku": "", "barcode": "", "qty": 1, "to_ship": 1,
             "ships": False, "price": None, "price_original": None, "cost": None, "margin_pct": None, "image": None},
        ])

    def test_discounted_price_unit_cost_and_margin(self):
        def item(original, discounted, cost):
            return {"title": "Wrap", "currentQuantity": 1, "unfulfilledQuantity": 1, "requiresShipping": True,
                    "originalUnitPriceSet": {"shopMoney": {"amount": original}},
                    "discountedUnitPriceAfterAllDiscountsSet": {"shopMoney": {"amount": discounted}},
                    "variant": {"barcode": "1", "inventoryItem": {"unitCost": cost and {"amount": cost}}}}
        products = picklists.order_products({"lineItems": {"nodes": [
            item("20.00", "16.00", "12.00"), item("10.00", "10.00", "12.50"), item("5.00", "0.00", "1.00")]}})
        self.assertEqual([(p["price"], p["price_original"], p["cost"], p["margin_pct"]) for p in products],
                         [("16.00", "20.00", "12.00", 25.0), ("10.00", None, "12.50", -25.0),
                          ("0.00", "5.00", "1.00", None)])


class OrderWeightTest(unittest.TestCase):
    def test_unfulfilled_shipping_items_in_pounds(self):
        def item(qty, value, unit, ships=True):
            return {"unfulfilledQuantity": qty, "requiresShipping": ships,
                    "variant": {"inventoryItem": {"measurement": {"weight": {"value": value, "unit": unit}}}}}
        lines = [item(2, 8, "OUNCES"), item(1, 1000, "GRAMS"), item(3, 1, "POUNDS", ships=False),
                 item(0, 5, "POUNDS"), {"unfulfilledQuantity": 1, "variant": None}]
        self.assertEqual(picklists.order_weight_lb({"lineItems": {"nodes": lines}}), 3.2)


class OrderProfitabilityTest(unittest.TestCase):
    ECON = {"available": True, "reason": None, "reason_text": None, "currency": "USD",
            "items_subtotal": 100.0, "shipping_paid": 10.0, "revenue": 110.0, "items_cost": 60.0,
            "line_count": 2, "missing_cost": []}
    ESTIMATE = {"cost": 15.0, "samples": 4, "tolerance_lb": 0.25, "state": "CT", "weight_lb": 2.0}
    NO_ESTIMATE = {"cost": None, "samples": 0, "tolerance_lb": None, "state": "CT", "weight_lb": 2.0}
    THRESHOLDS = {"min_amount": 40.0, "min_margin_pct": None, "enabled": True}

    def test_estimated_shipping_is_deducted_and_checked_against_the_thresholds(self):
        result = picklists.order_profitability(self.ECON, self.ESTIMATE, self.THRESHOLDS)
        self.assertEqual({k: result[k] for k in ("profit", "margin_pct", "includes_shipping", "below_threshold", "reasons")},
                         {"profit": 35.0, "margin_pct": 31.8, "includes_shipping": True, "below_threshold": True,
                          "reasons": ["Profit $35.00 is below the $40.00 minimum"]})

    def test_without_an_estimate_profit_is_before_shipping_and_not_judged(self):
        result = picklists.order_profitability(self.ECON, self.NO_ESTIMATE, self.THRESHOLDS)
        self.assertEqual({k: result[k] for k in ("profit", "includes_shipping", "below_threshold", "reasons", "shipping_estimate")},
                         {"profit": 50.0, "includes_shipping": False, "below_threshold": False, "reasons": [],
                          "shipping_estimate": self.NO_ESTIMATE})

    def test_unavailable_economics_say_why(self):
        econ = {**self.ECON, "available": False, "reason_text": "the order has more than 100 line items"}
        self.assertEqual(picklists.order_profitability(econ, self.ESTIMATE, self.THRESHOLDS),
                         {"available": False, "reason_text": "the order has more than 100 line items",
                          "shipping_estimate": self.ESTIMATE})


class AddressesMatchTest(unittest.TestCase):
    SHIP = {"address1": "252 Wilson Mill Rd", "address2": "", "city": "Oxford", "provinceCode": "PA",
            "zip": "19363", "countryCodeV2": "US"}

    def test_cases(self):
        cases = [
            ({**self.SHIP, "address1": "252  wilson mill rd.", "zip": "19363-4410", "address2": None}, True),
            ({**self.SHIP, "address1": "9 Oak Ave"}, False),
            ({**self.SHIP, "address2": "Apt 2"}, False),
            ({**self.SHIP, "zip": "19364"}, False),
            (None, None),
        ]
        self.assertEqual([picklists.addresses_match(billing, self.SHIP) for billing, _ in cases],
                         [expected for _, expected in cases])


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


class PickIssuesTest(unittest.TestCase):
    def test_every_line_without_a_known_barcode_is_an_issue(self):
        o = order(lines=[line("A", "111"), line("B", "999"), line("C", "")])
        self.assertEqual(picklists.pick_issues(o, {"111"}), [
            {"kind": "unknown_barcode", "text": "B — barcode 999 is not in BackOffice"},
            {"kind": "no_barcode", "text": "C has no barcode in Shopify"},
        ])

    def test_without_a_backoffice_answer_only_missing_barcodes_count(self):
        o = order(lines=[line("B", "999"), line("C", "")])
        self.assertEqual(picklists.pick_issues(o, None),
                         [{"kind": "no_barcode", "text": "C has no barcode in Shopify"}])


class ParseTagListTest(unittest.TestCase):
    def test_comma_list_is_trimmed_and_unique_ignoring_case(self):
        self.assertEqual(picklists.parse_tag_list(" Shop, Login with Shop,,shop ,"), ["Shop", "Login with Shop"])

    def test_empty_setting_hides_nothing(self):
        self.assertEqual([picklists.parse_tag_list(v) for v in (None, "", " , ")], [[], [], []])


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


class ShopifyTagTest(unittest.TestCase):
    def test_tag_names_the_quotation(self):
        self.assertEqual(picklists.shopify_tag("10222026191"), "picklist 10222026191")


class TagTargetsTest(unittest.TestCase):
    STATES = (None, "pending", "tagged", "failed", "removed", "remove_failed")

    def rows(self):
        return [{"id": i, "tag_state": state} for i, state in enumerate(self.STATES)]

    def test_adding_retries_what_is_not_tagged_yet(self):
        self.assertEqual([r["tag_state"] for r in picklists.tag_targets(self.rows(), "add")], ["pending", "failed"])

    def test_removing_touches_every_order_that_may_carry_the_tag(self):
        self.assertEqual([r["tag_state"] for r in picklists.tag_targets(self.rows(), "remove")],
                         ["pending", "tagged", "failed", "remove_failed"])


if __name__ == "__main__":
    unittest.main()

import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp"))

import shopify_client  # noqa: E402


def money(amount):
    return {"shopMoney": {"amount": amount}}


def line(title, sku, current_qty, price, cost, discounted=None):
    return {
        "title": title, "sku": sku, "quantity": current_qty + 1, "currentQuantity": current_qty,
        "unfulfilledQuantity": 0,
        "originalUnitPriceSet": money(price),
        "discountedUnitPriceAfterAllDiscountsSet": money(discounted) if discounted is not None else None,
        "variant": {"inventoryItem": {"unitCost": {"amount": cost} if cost is not None else None}},
    }


def order(**over):
    base = {
        "currencyCode": "USD",
        "currentSubtotalPriceSet": money("85.00"),
        "currentShippingPriceSet": money("7.50"),
        "totalShippingPriceSet": money("9.00"),
        "lineItems": {"pageInfo": {"hasNextPage": False}, "nodes": [
            line("Widget", "W-1", 2, "20.00", "8.00", discounted="18.00"),
            line("Gadget", "G-1", 1, "50.00", None),
            line("Removed", "R-1", 0, "99.00", "1.00"),
        ]},
    }
    base.update(over)
    return base


class EconomicsFromOrderTest(unittest.TestCase):
    """Whole-order economics come from the order's current (post-edit)
    totals; line costs are the variant's inventory unit cost, and a line
    without one is costed at 90% of its discounted selling price."""

    def test_current_totals_and_unit_costs_make_the_economics(self):
        self.assertEqual(shopify_client.economics_from_order(order()), {
            "available": True, "reason": None, "reason_text": None, "currency": "USD",
            "items_subtotal": 85.0, "shipping_paid": 7.5, "revenue": 92.5,
            "items_cost": 61.0,  # 2×8 + 1×50×0.9 (no cost → 90% of price)
            "line_count": 2,
            "missing_cost": [{"description": "Gadget", "sku": "G-1", "quantity": 1}]})

    def test_shipping_falls_back_to_the_original_charge(self):
        econ = shopify_client.economics_from_order(order(currentShippingPriceSet=None))
        self.assertEqual((econ["shipping_paid"], econ["revenue"]), (9.0, 94.0))

    def test_quantity_falls_back_when_current_quantity_is_absent(self):
        li = dict(line("Widget", "W-1", 2, "20.00", None), currentQuantity=None)
        econ = shopify_client.economics_from_order(order(lineItems={"nodes": [li]}))
        self.assertEqual((econ["line_count"], econ["items_cost"]), (1, 54.0))

    def test_more_than_a_page_of_lines_is_unavailable(self):
        econ = shopify_client.economics_from_order(
            order(lineItems={"pageInfo": {"hasNextPage": True}, "nodes": []}))
        self.assertEqual((econ["available"], econ["reason"], econ["currency"]),
                         (False, "lines_truncated", "USD"))


class OrderGateFieldsTest(unittest.TestCase):
    """The order queries carry the hold and payment fields the gate reads,
    and get_order / the Orders list expose the gate built from them."""

    def setUp(self):
        self._orig = (shopify_client._graphql, shopify_client._store)
        shopify_client._store = lambda store_id: {"no_company": False}

    def tearDown(self):
        shopify_client._graphql, shopify_client._store = self._orig

    def test_queries_select_the_gate_fields(self):
        for field in ("displayFinancialStatus", "fullyPaid", "totalOutstandingSet", "fulfillmentHolds"):
            self.assertIn(field, shopify_client.ORDER_DETAIL_QUERY)
            self.assertIn(field, shopify_client.ORDER_GATE_QUERY)
        self.assertIn("displayFinancialStatus", shopify_client.ORDERS_QUERY)

    def test_get_order_carries_the_gate(self):
        node = order(id="gid://shopify/Order/1", name="#1001", displayFinancialStatus="PARTIALLY_PAID",
                     totalOutstandingSet={"shopMoney": {"amount": "30", "currencyCode": "USD"}},
                     fulfillmentOrders={"nodes": [{"id": "fo1", "status": "ON_HOLD", "fulfillmentHolds": []}]})
        shopify_client._graphql = lambda store_id, query, variables=None: {"order": node}
        gate = shopify_client.get_order(1, "gid://shopify/Order/1")["gate"]
        self.assertEqual((gate["shippable"], gate["label"], gate["reasons"]), (False, "On hold", [
            "Fulfillment is on hold in Shopify",
            "The order is only partially paid — $30.00 outstanding"]))

    def test_orders_list_labels_held_and_unpaid_orders(self):
        nodes = [
            {"id": "o1", "name": "#1", "displayFulfillmentStatus": "ON_HOLD", "displayFinancialStatus": "PAID"},
            {"id": "o2", "name": "#2", "displayFulfillmentStatus": "UNFULFILLED", "displayFinancialStatus": "PENDING"},
            {"id": "o3", "name": "#3", "displayFulfillmentStatus": "UNFULFILLED", "displayFinancialStatus": "PAID"},
        ]
        shopify_client._graphql = lambda store_id, query, variables=None: {"orders": {"nodes": nodes}}
        self.assertEqual([o["gate_label"] for o in shopify_client.list_open_orders(1)],
                         ["On hold", "Payment pending", None])


if __name__ == "__main__":
    unittest.main()

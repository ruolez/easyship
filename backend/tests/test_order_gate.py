import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp",
    MANIFESTS_DIR="/tmp"))

import order_gate  # noqa: E402
import shopify_client  # noqa: E402

GID = "gid://shopify/Order/1"


def fo(status, holds=()):
    return {"id": f"gid://shopify/FulfillmentOrder/{status}", "status": status,
            "fulfillmentHolds": [{"reason": r, "reasonNotes": n} for r, n in holds]}


def order(status="PAID", fos=(fo("OPEN"),), outstanding="0.00", **over):
    base = {"displayFulfillmentStatus": "UNFULFILLED", "displayFinancialStatus": status,
            "fullyPaid": status in order_gate.PAID_STATUSES,
            "totalOutstandingSet": {"shopMoney": {"amount": outstanding, "currencyCode": "USD"}}}
    if fos is not None:
        base["fulfillmentOrders"] = {"nodes": list(fos)}
    base.update(over)
    return base


SHIPPABLE = {"shippable": True, "label": None, "reasons": [], "on_hold": False, "fully_paid": True,
             "financial_status": "PAID", "holds": [], "outstanding": 0.0, "currency": "USD"}


class FromShopifyOrderTest(unittest.TestCase):
    """An order ships only when every fulfillment order is off hold and the
    payment is complete; each block names its reason for the packer."""

    def test_paid_open_order_is_shippable(self):
        self.assertEqual(order_gate.from_shopify_order(order()), SHIPPABLE)

    def test_partially_refunded_counts_as_paid(self):
        gate = order_gate.from_shopify_order(order("PARTIALLY_REFUNDED"))
        self.assertEqual((gate["shippable"], gate["fully_paid"], gate["label"]), (True, True, None))

    def test_each_unpaid_status_blocks_with_its_reason(self):
        expected = {
            "PENDING": ("Payment pending", "Payment is pending — the order has not been paid"),
            "AUTHORIZED": ("Payment not captured", "Payment is authorized but has not been captured"),
            "PARTIALLY_PAID": ("Partially paid", "The order is only partially paid"),
            "EXPIRED": ("Payment expired", "The payment authorization has expired"),
            "VOIDED": ("Payment voided", "The payment was voided"),
            "REFUNDED": ("Refunded", "The order has been fully refunded"),
        }
        for status, (label, reason) in expected.items():
            with self.subTest(status=status):
                gate = order_gate.from_shopify_order(order(status))
                self.assertEqual((gate["shippable"], gate["label"], gate["reasons"], gate["fully_paid"]),
                                 (False, label, [reason], False))

    def test_partially_paid_reason_carries_the_outstanding_amount(self):
        gate = order_gate.from_shopify_order(order("PARTIALLY_PAID", outstanding="1234.5"))
        self.assertEqual((gate["reasons"], gate["outstanding"]),
                         (["The order is only partially paid — $1,234.50 outstanding"], 1234.5))

    def test_missing_status_falls_back_to_the_fully_paid_flag(self):
        results = [order_gate.from_shopify_order(order(status=None, fullyPaid=flag))["shippable"]
                   for flag in (False, True, None)]
        self.assertEqual(results, [False, True, True])
        gate = order_gate.from_shopify_order(order(status=None, fullyPaid=False))
        self.assertEqual((gate["label"], gate["reasons"]), ("Not paid", ["The order is not fully paid"]))

    def test_any_held_fulfillment_order_blocks_even_when_another_is_open(self):
        gate = order_gate.from_shopify_order(order(fos=[fo("OPEN"), fo("ON_HOLD")]))
        self.assertEqual((gate["shippable"], gate["label"], gate["on_hold"], gate["reasons"]),
                         (False, "On hold", True, ["Some items are on hold in Shopify"]))

    def test_hold_reasons_and_notes_are_listed(self):
        gate = order_gate.from_shopify_order(order(fos=[
            fo("ON_HOLD", [("AWAITING_PAYMENT", "customer asked to wait"), ("OTHER", None)])]))
        self.assertEqual((gate["reasons"], gate["holds"]), (
            ["Fulfillment is on hold in Shopify: Awaiting payment (customer asked to wait), Other"],
            [{"reason": "AWAITING_PAYMENT", "notes": "customer asked to wait"},
             {"reason": "OTHER", "notes": None}]))

    def test_closed_cancelled_and_scheduled_fulfillment_orders_do_not_block(self):
        gate = order_gate.from_shopify_order(order(fos=[fo("CLOSED"), fo("CANCELLED"), fo("SCHEDULED")]))
        self.assertEqual((gate["shippable"], gate["on_hold"]), (True, False))

    def test_display_status_on_hold_blocks_when_fulfillment_orders_are_absent(self):
        gate = order_gate.from_shopify_order(order(fos=None, displayFulfillmentStatus="ON_HOLD"))
        self.assertEqual((gate["shippable"], gate["label"], gate["reasons"]),
                         (False, "On hold", ["Fulfillment is on hold in Shopify"]))

    def test_hold_and_unpaid_list_both_reasons_under_the_hold_label(self):
        gate = order_gate.from_shopify_order(order("PENDING", fos=[fo("ON_HOLD")]))
        self.assertEqual((gate["label"], gate["reasons"]), ("On hold", [
            "Fulfillment is on hold in Shopify",
            "Payment is pending — the order has not been paid"]))

    def test_empty_order_is_shippable(self):
        self.assertEqual(order_gate.from_shopify_order({})["shippable"], True)
        self.assertEqual(order_gate.from_shopify_order(None)["shippable"], True)

    def test_warning_text_names_the_label_and_reasons(self):
        gate = order_gate.from_shopify_order(order("PARTIALLY_PAID", fos=[fo("ON_HOLD")]))
        self.assertEqual(order_gate.warning_text(gate), (
            "On hold — rates only, a label cannot be bought: Fulfillment is on hold in Shopify; "
            "The order is only partially paid"))


SNAPSHOT = {"shippable": False, "label": "On hold", "reasons": ["Fulfillment is on hold in Shopify"]}


class ForBuyTest(unittest.TestCase):
    """The buy asks Shopify first and falls back to the rate-time snapshot."""

    def setUp(self):
        self.calls = []
        self._orig = shopify_client.get_order_gate
        self.live = SHIPPABLE
        self.error = None

        def fake(store_id, gid):
            self.calls.append((store_id, gid))
            if self.error:
                raise self.error
            return self.live
        shopify_client.get_order_gate = fake

    def tearDown(self):
        shopify_client.get_order_gate = self._orig

    def row(self, **over):
        base = {"shopify_store_id": 3, "shopify_order_id": GID, "order_gate": SNAPSHOT}
        base.update(over)
        return base

    def test_live_answer_wins_over_the_snapshot(self):
        self.assertEqual(order_gate.for_buy(self.row()), (SHIPPABLE, True))
        self.assertEqual(self.calls, [(3, GID)])

    def test_unreachable_shopify_falls_back_to_the_snapshot(self):
        self.error = shopify_client.ShopifyUnavailable("down")
        self.assertEqual(order_gate.for_buy(self.row()), (SNAPSHOT, False))

    def test_order_never_loaded_is_not_looked_up(self):
        self.assertEqual(order_gate.for_buy(self.row(shopify_order_id=None, order_gate=None)), (None, False))
        self.assertEqual(self.calls, [])


class GetOrderGateTest(unittest.TestCase):
    def setUp(self):
        self._orig = shopify_client._graphql
        self.sent = []

        def fake(store_id, query, variables=None):
            self.sent.append((query, variables))
            return self.response
        shopify_client._graphql = fake

    def tearDown(self):
        shopify_client._graphql = self._orig

    def test_sends_the_gate_query_and_returns_the_gate(self):
        self.response = {"order": order("PARTIALLY_PAID", outstanding="5")}
        gate = shopify_client.get_order_gate(3, GID)
        self.assertEqual((self.sent, gate["label"], gate["outstanding"]),
                         ([(shopify_client.ORDER_GATE_QUERY, {"id": GID})], "Partially paid", 5.0))

    def test_missing_order_raises(self):
        self.response = {"order": None}
        with self.assertRaises(shopify_client.ShopifyError):
            shopify_client.get_order_gate(3, GID)


if __name__ == "__main__":
    unittest.main()

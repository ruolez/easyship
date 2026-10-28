import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp"))

import shopify_client  # noqa: E402
from shopify_client import ShopifyError  # noqa: E402

STORE, ORDER = 1, "gid://shopify/Order/1"


class FakeGraphql:
    """Replaces shopify_client._graphql: the order's fulfillment orders, and a
    hold or release per call; refuse = fulfillment order ids Shopify refuses."""

    def __init__(self, fos, refuse=()):
        self.fos = fos
        self.refuse = set(refuse)
        self.calls = []

    def __call__(self, store_id, query, variables=None):
        self.calls.append((query.split("(")[0].split()[-1], variables))
        if "query fulfillmentOrders" in query:
            return {"order": {"fulfillmentOrders": {"nodes": self.fos}}}
        errors = [{"field": ["id"], "message": "Can't hold"}] if variables["id"] in self.refuse else []
        if "fulfillmentOrderHold(" in query:
            hold = None if errors else {"id": f"hold-{variables['id']}"}
            return {"fulfillmentOrderHold": {"fulfillmentHold": hold, "userErrors": errors}}
        if "fulfillmentOrderReleaseHold(" in query:
            return {"fulfillmentOrderReleaseHold": {"fulfillmentOrder": {"id": variables["id"]}, "userErrors": errors}}
        raise AssertionError(f"unexpected query: {query[:60]}")


class HoldOrderTest(unittest.TestCase):
    def setUp(self):
        self._orig = shopify_client._graphql

    def tearDown(self):
        shopify_client._graphql = self._orig

    def test_holds_every_open_fulfillment_order_with_the_reason(self):
        fake = shopify_client._graphql = FakeGraphql([
            {"id": "fo1", "status": "OPEN"}, {"id": "fo2", "status": "CLOSED"}, {"id": "fo3", "status": "OPEN"}])
        holds = shopify_client.hold_order(STORE, ORDER, "INCORRECT_ADDRESS", "Apt number missing")
        hold = {"reason": "INCORRECT_ADDRESS", "reasonNotes": "Apt number missing"}
        self.assertEqual((holds, fake.calls[1:]), (
            [{"fulfillment_order_id": "fo1", "hold_id": "hold-fo1"},
             {"fulfillment_order_id": "fo3", "hold_id": "hold-fo3"}],
            [("picklistHold", {"id": "fo1", "fulfillmentHold": hold}),
             ("picklistHold", {"id": "fo3", "fulfillmentHold": hold})]))

    def test_a_hold_without_notes_sends_only_the_reason(self):
        fake = shopify_client._graphql = FakeGraphql([{"id": "fo1", "status": "OPEN"}])
        shopify_client.hold_order(STORE, ORDER, "OTHER", "")
        self.assertEqual(fake.calls[1], ("picklistHold", {"id": "fo1", "fulfillmentHold": {"reason": "OTHER"}}))

    def test_an_order_with_nothing_open_is_refused_before_any_hold(self):
        fake = shopify_client._graphql = FakeGraphql([{"id": "fo1", "status": "ON_HOLD"}])
        with self.assertRaisesRegex(ShopifyError, "nothing left to put on hold"):
            shopify_client.hold_order(STORE, ORDER, "OTHER")
        self.assertEqual([name for name, _ in fake.calls], ["fulfillmentOrders"])

    def test_a_refused_hold_releases_the_ones_already_placed(self):
        fake = shopify_client._graphql = FakeGraphql(
            [{"id": "fo1", "status": "OPEN"}, {"id": "fo2", "status": "OPEN"}], refuse={"fo2"})
        with self.assertRaisesRegex(ShopifyError, "Can't hold"):
            shopify_client.hold_order(STORE, ORDER, "OTHER")
        self.assertEqual(fake.calls[-1], ("picklistReleaseHold", {"id": "fo1", "holdIds": ["hold-fo1"]}))

    def test_release_lets_go_of_exactly_the_holds_given_per_fulfillment_order(self):
        fake = shopify_client._graphql = FakeGraphql([])
        shopify_client.release_holds(STORE, [
            {"fulfillment_order_id": "fo1", "hold_id": "h1"}, {"fulfillment_order_id": "fo2", "hold_id": "h2"},
            {"fulfillment_order_id": "fo1", "hold_id": "h3"}])
        self.assertEqual(fake.calls, [("picklistReleaseHold", {"id": "fo1", "holdIds": ["h1", "h3"]}),
                                      ("picklistReleaseHold", {"id": "fo2", "holdIds": ["h2"]})])

    def test_a_refused_release_says_why(self):
        shopify_client._graphql = FakeGraphql([], refuse={"fo1"})
        with self.assertRaisesRegex(ShopifyError, "Can't hold"):
            shopify_client.release_holds(STORE, [{"fulfillment_order_id": "fo1", "hold_id": "h1"}])


if __name__ == "__main__":
    unittest.main()

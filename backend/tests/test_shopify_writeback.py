import json
import sys
import types
import unittest

# shopify_client reads config/db at import; stub them (no live DB needed).
sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2025-07", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp"))

import shopify_client  # noqa: E402

STORE, ORDER, FO, FULFILLMENT = 1, "gid://shopify/Order/1", "gid://FO/1", "gid://F/9"


class FakeGraphql:
    """Replaces shopify_client._graphql, recording every (mutation, variables)."""

    def __init__(self, open_fos=True, existing=None):
        self.calls = []
        self.open_fos = open_fos
        self.existing = existing or []

    def __call__(self, store_id, query, variables=None):
        self.calls.append((query, variables))
        if "query fulfillmentOrders" in query:
            nodes = [{"id": FO, "status": "OPEN"}] if self.open_fos else []
            return {"order": {"fulfillmentOrders": {"nodes": nodes}}}
        if "query orderFulfillments" in query:
            return {"order": {"fulfillments": self.existing}}
        if "fulfillmentCreate" in query:
            return {"fulfillmentCreate": {"fulfillment": {"id": FULFILLMENT, "status": "SUCCESS"},
                                          "userErrors": []}}
        if "fulfillmentTrackingInfoUpdate" in query:
            return {"fulfillmentTrackingInfoUpdate": {"fulfillment": {"id": FULFILLMENT, "status": "SUCCESS"},
                                                      "userErrors": []}}
        raise AssertionError(f"unexpected query: {query[:60]}")

    def named(self, fragment):
        return [(q, v) for q, v in self.calls if fragment in q]


class FulfillOrderTest(unittest.TestCase):
    def setUp(self):
        self._orig = shopify_client._graphql

    def tearDown(self):
        shopify_client._graphql = self._orig

    def test_multi_box_creates_with_all_numbers_then_reasserts_them(self):
        fake = shopify_client._graphql = FakeGraphql()
        shopify_client.fulfill_order(STORE, ORDER, "A", "UPS Ground", all_numbers=["A", "B", "C"])
        (_, create_vars), = fake.named("fulfillmentCreate")
        self.assertEqual(create_vars["fulfillment"]["trackingInfo"],
                         {"company": "UPS Ground", "numbers": ["A", "B", "C"]})
        (_, update_vars), = fake.named("fulfillmentTrackingInfoUpdate")
        self.assertEqual(update_vars, {
            "fulfillmentId": FULFILLMENT,
            "trackingInfoInput": {"company": "UPS Ground", "numbers": ["A", "B", "C"]},
            "notifyCustomer": False,
        })

    def test_single_box_sends_one_number_and_skips_the_reassert(self):
        fake = shopify_client._graphql = FakeGraphql()
        shopify_client.fulfill_order(STORE, ORDER, "A", "UPS Ground", all_numbers=["A"])
        (_, create_vars), = fake.named("fulfillmentCreate")
        self.assertEqual(create_vars["fulfillment"]["trackingInfo"],
                         {"company": "UPS Ground", "number": "A"})
        self.assertEqual(fake.named("fulfillmentTrackingInfoUpdate"), [])

    def test_reship_of_fulfilled_order_merges_numbers_into_existing_fulfillment(self):
        fake = shopify_client._graphql = FakeGraphql(open_fos=False, existing=[
            {"id": FULFILLMENT, "status": "SUCCESS",
             "trackingInfo": [{"number": "OLD", "company": "USPS"}]},
        ])
        shopify_client.fulfill_order(STORE, ORDER, "A", "UPS Ground", all_numbers=["A", "B"])
        self.assertEqual(fake.named("fulfillmentCreate"), [])
        (_, update_vars), = fake.named("fulfillmentTrackingInfoUpdate")
        self.assertEqual(update_vars["trackingInfoInput"],
                         {"company": "USPS", "numbers": ["OLD", "A", "B"]})

    def test_create_user_errors_raise_before_any_reassert(self):
        fake = shopify_client._graphql = FakeGraphql()
        real_call = fake.__call__

        def with_error(store_id, query, variables=None):
            if "fulfillmentCreate" in query:
                fake.calls.append((query, variables))
                return {"fulfillmentCreate": {"fulfillment": None,
                                              "userErrors": [{"field": None, "message": "boom"}]}}
            return real_call(store_id, query, variables)

        shopify_client._graphql = with_error
        with self.assertRaises(shopify_client.ShopifyError):
            shopify_client.fulfill_order(STORE, ORDER, "A", "UPS", all_numbers=["A", "B"])
        self.assertEqual(fake.named("fulfillmentTrackingInfoUpdate"), [])


if __name__ == "__main__":
    unittest.main()

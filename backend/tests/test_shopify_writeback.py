import json
import sys
import types
import unittest

# shopify_client reads config/db at import; stub them (no live DB needed).
sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp"))

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


class TrackingCompanyTest(unittest.TestCase):
    """Shopify only builds tracking links for (and other apps only leave alone)
    the carrier names on its own list, spelled exactly."""

    CASES = [
        # (umbrella_name, courier_name, expected Shopify company)
        ("UPS", "UPS® Ground", "UPS"),
        ("FedEx", "FedEx Ground®", "FedEx"),
        (None, "FedEx 2Day®", "FedEx"),
        ("", "UPS® Ground", "UPS"),
        ("USPS", "Priority Mail", "USPS"),
        ("Stamps.com", "USPS Ground Advantage", "USPS"),
        ("stamps_com", "Ground Advantage", "USPS"),
        ("Endicia", "Priority Mail Express", "USPS"),
        ("ups_walleted", "Ground", "UPS"),
        ("DHL Express", "DHL Express Worldwide", "DHL Express"),
        ("DHL eCommerce", "DHL SmartMail Parcel", "DHL eCommerce"),
        ("Canada Post", "Expedited Parcel", "Canada Post"),
        ("OnTrac", "OnTrac Ground", "OnTrac"),
        ("Dummy", "Dummy - QrCode", "Dummy - QrCode"),
        (None, None, "Other"),
    ]

    def test_maps_provider_carrier_names_to_shopify_company_names(self):
        self.assertEqual(
            [shopify_client.tracking_company(u, c) for u, c, _ in self.CASES],
            [expected for _, _, expected in self.CASES],
        )


class FulfillOrderTest(unittest.TestCase):
    def setUp(self):
        self._orig = shopify_client._graphql

    def tearDown(self):
        shopify_client._graphql = self._orig

    def test_multi_box_creates_with_all_numbers_then_reasserts_them(self):
        fake = shopify_client._graphql = FakeGraphql()
        shopify_client.fulfill_order(STORE, ORDER, "A", "UPS® Ground", all_numbers=["A", "B", "C"],
                                     umbrella_name="UPS")
        (_, create_vars), = fake.named("fulfillmentCreate")
        self.assertEqual(create_vars["fulfillment"]["trackingInfo"],
                         {"company": "UPS", "numbers": ["A", "B", "C"]})
        (_, update_vars), = fake.named("fulfillmentTrackingInfoUpdate")
        self.assertEqual(update_vars, {
            "fulfillmentId": FULFILLMENT,
            "trackingInfoInput": {"company": "UPS", "numbers": ["A", "B", "C"]},
            "notifyCustomer": False,
        })

    def test_single_box_sends_one_number_and_skips_the_reassert(self):
        fake = shopify_client._graphql = FakeGraphql()
        shopify_client.fulfill_order(STORE, ORDER, "A", "UPS® Ground", all_numbers=["A"])
        (_, create_vars), = fake.named("fulfillmentCreate")
        self.assertEqual(create_vars["fulfillment"]["trackingInfo"],
                         {"company": "UPS", "number": "A"})
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

    def test_reship_respells_a_non_shopify_company_left_on_the_fulfillment(self):
        fake = shopify_client._graphql = FakeGraphql(open_fos=False, existing=[
            {"id": FULFILLMENT, "status": "SUCCESS",
             "trackingInfo": [{"number": "OLD", "company": "UPS Ground"}]},
        ])
        shopify_client.fulfill_order(STORE, ORDER, "A", "UPS® Ground", all_numbers=["A"], umbrella_name="UPS")
        (_, update_vars), = fake.named("fulfillmentTrackingInfoUpdate")
        self.assertEqual(update_vars["trackingInfoInput"], {"company": "UPS", "numbers": ["OLD", "A"]})

    def test_reship_onto_a_fulfillment_without_a_company_uses_the_shopify_name(self):
        fake = shopify_client._graphql = FakeGraphql(open_fos=False, existing=[
            {"id": FULFILLMENT, "status": "SUCCESS", "trackingInfo": []},
        ])
        shopify_client.fulfill_order(STORE, ORDER, "A", "FedEx Ground®", all_numbers=["A", "B"],
                                     umbrella_name="FedEx")
        (_, update_vars), = fake.named("fulfillmentTrackingInfoUpdate")
        self.assertEqual(update_vars["trackingInfoInput"], {"company": "FedEx", "numbers": ["A", "B"]})

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

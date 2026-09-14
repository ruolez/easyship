import sys
import types
import unittest

import requests

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2025-07", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp"))

import shopify_client  # noqa: E402

STORE = {"id": 1, "shop_domain": "x.myshopify.com", "access_token": "t"}
QUERY = "query q { shop { name } }"
OK_BODY = {"data": {"shop": {"name": "x"}}}


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        return self._body


def ok():
    return FakeResponse(200, OK_BODY)


def status(code):
    return FakeResponse(code, None, f"http {code}")


def throttled():
    return FakeResponse(200, {"errors": [{"message": "Throttled",
                                          "extensions": {"code": "THROTTLED"}}]})


def rejected():
    return FakeResponse(200, {"errors": [{"message": "Field 'x' doesn't exist"}]})


class GraphqlRetryTest(unittest.TestCase):
    """_graphql retries only what a second attempt can fix and never sleeps
    for real."""

    def setUp(self):
        self._orig = (shopify_client._store, requests.post, shopify_client._sleep)
        shopify_client._store = lambda store_id: STORE
        self.sleeps = []
        shopify_client._sleep = self.sleeps.append

    def tearDown(self):
        shopify_client._store, requests.post, shopify_client._sleep = self._orig

    def run_with(self, outcomes):
        """Each outcome is a response, or an exception instance to raise."""
        calls = []

        def fake_post(url, **kwargs):
            calls.append(kwargs["json"])
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        requests.post = fake_post
        return calls

    def test_network_error_then_5xx_then_success_returns_data_after_two_sleeps(self):
        calls = self.run_with([requests.ConnectionError("down"), status(503), ok()])
        self.assertEqual(shopify_client._graphql(1, QUERY), OK_BODY["data"])
        self.assertEqual((len(calls), self.sleeps), (3, [1.0, 3.0]))

    def test_throttled_and_429_are_retried(self):
        self.run_with([throttled(), status(429), ok()])
        self.assertEqual(shopify_client._graphql(1, QUERY), OK_BODY["data"])

    def test_gives_up_after_three_attempts_as_unavailable(self):
        calls = self.run_with([status(502), status(502), status(502)])
        with self.assertRaises(shopify_client.ShopifyUnavailable) as ctx:
            shopify_client._graphql(1, QUERY)
        self.assertEqual(len(calls), 3)
        self.assertIn("after 3 attempts", str(ctx.exception))

    def test_client_error_is_not_retried(self):
        calls = self.run_with([status(401)])
        with self.assertRaises(shopify_client.ShopifyError) as ctx:
            shopify_client._graphql(1, QUERY)
        self.assertNotIsInstance(ctx.exception, shopify_client.ShopifyUnavailable)
        self.assertEqual((len(calls), self.sleeps), (1, []))

    def test_graphql_rejection_is_not_retried(self):
        calls = self.run_with([rejected()])
        with self.assertRaises(shopify_client.ShopifyError):
            shopify_client._graphql(1, QUERY)
        self.assertEqual(len(calls), 1)


class ResolveOrderTest(unittest.TestCase):
    """resolve_order turns a scanned number into the order's gid and real name."""

    def setUp(self):
        self._orig = shopify_client._graphql

    def tearDown(self):
        shopify_client._graphql = self._orig

    def fake(self, by_query):
        calls = []

        def graphql(store_id, query, variables=None):
            calls.append(variables["query"])
            return {"orders": {"nodes": by_query.get(variables["query"], [])}}
        shopify_client._graphql = graphql
        return calls

    def test_hash_prefixed_match_wins_first(self):
        node = {"id": "gid://shopify/Order/5", "name": "#1234"}
        calls = self.fake({"name:#1234": [node]})
        self.assertEqual(shopify_client.resolve_order(1, " #1234 "), node)
        self.assertEqual(calls, ["name:#1234"])

    def test_falls_back_to_bare_name(self):
        node = {"id": "gid://shopify/Order/6", "name": "TS1234"}
        calls = self.fake({"name:TS1234": [node]})
        self.assertEqual(shopify_client.resolve_order(1, "TS1234"), node)
        self.assertEqual(calls, ["name:#TS1234", "name:TS1234"])

    def test_unknown_number_returns_none(self):
        self.fake({})
        self.assertIsNone(shopify_client.resolve_order(1, "9999"))


if __name__ == "__main__":
    unittest.main()

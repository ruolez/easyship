import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import requests

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp"))

import shopify_auth  # noqa: E402
from shopify_errors import ShopifyError, ShopifyUnavailable  # noqa: E402

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
# Shopify's documented OAuth example: secret "hush" signs these parameters.
DOC_SECRET = "hush"
DOC_PARAMS = {"code": "0907a61c0c8d55e99db179b68161bc00", "shop": "some-shop.myshopify.com",
              "state": "0.6784241404160823", "timestamp": "1337178173",
              "hmac": "700e2dadb827fcc8609e9d5ce208b2e9cdaab9df07390d2cbca10d7c328fc4bf"}


class FakeResponse:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def app_store(**overrides):
    return {"id": 7, "name": "TSW", "shop_domain": "tsw.myshopify.com", "auth_method": "app",
            "client_id": "cid", "client_secret": "csecret", "access_token": "old",
            "token_grant": "client_credentials", "token_expires_at": NOW - timedelta(minutes=1),
            "refresh_token": None, "refresh_expires_at": None, "auth_error": None, **overrides}


class PureHelpersTest(unittest.TestCase):
    def test_verify_hmac_accepts_shopify_documented_example(self):
        self.assertTrue(shopify_auth.verify_hmac(DOC_PARAMS, DOC_SECRET))

    def test_verify_hmac_rejects_tampered_param_wrong_secret_and_missing_hmac(self):
        tampered = {**DOC_PARAMS, "shop": "evil.myshopify.com"}
        missing = {k: v for k, v in DOC_PARAMS.items() if k != "hmac"}
        self.assertEqual(
            [shopify_auth.verify_hmac(tampered, DOC_SECRET), shopify_auth.verify_hmac(DOC_PARAMS, "other"),
             shopify_auth.verify_hmac(missing, DOC_SECRET)],
            [False, False, False])

    def test_shop_domain_regex_is_anchored(self):
        cases = {"tsw.myshopify.com": True, "a-b1.myshopify.com": True, "x.myshopify.com.evil.test": False,
                 "evil.com/x.myshopify.com": False, "-x.myshopify.com": False, "myshopify.com": False}
        self.assertEqual({d: bool(shopify_auth.SHOP_RE.fullmatch(d)) for d in cases}, cases)

    def test_normalize_domain_strips_scheme_path_and_case(self):
        self.assertEqual(
            [shopify_auth.normalize_domain(v) for v in (" HTTPS://TSW.myshopify.com/admin ", "tsw.myshopify.com/", None)],
            ["tsw.myshopify.com", "tsw.myshopify.com", ""])

    def test_missing_scopes_counts_write_as_covering_read(self):
        granted = ["write_orders", "read_customers", "read_products", "read_inventory",
                   "write_merchant_managed_fulfillment_orders"]
        self.assertEqual(shopify_auth.missing_scopes(granted), [])
        self.assertEqual(shopify_auth.missing_scopes(["read_orders"]),
                         [s for s in shopify_auth.REQUIRED_SCOPES if s != "read_orders"])

    def test_store_auth_problem_per_method(self):
        p = shopify_auth.store_auth_problem
        self.assertEqual(
            [p("token", "x.com", "shpat", "", ""), p("token", "x.myshopify.com", "", "", ""),
             p("app", "tsw.myshopify.com", "", "cid", "sec"), p("app", "shop.example.com", "", "cid", "sec"),
             p("app", "tsw.myshopify.com", "", "cid", ""), p("other", "tsw.myshopify.com", "", "", "")],
            [None, "An Admin API access token is required", None,
             "A Shopify app needs the store's *.myshopify.com domain",
             "The app's client ID and client secret are required", "Unknown connection method"])

    def test_authorize_url_targets_the_store_with_required_scopes(self):
        url = urlparse(shopify_auth.authorize_url(app_store(), "https://10.0.0.5:5558/cb", "st8"))
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "tsw.myshopify.com", "/admin/oauth/authorize"))
        self.assertEqual(parse_qs(url.query), {
            "client_id": ["cid"], "scope": [",".join(shopify_auth.REQUIRED_SCOPES)],
            "redirect_uri": ["https://10.0.0.5:5558/cb"], "state": ["st8"]})

    def test_token_is_fresh_honours_margin_and_non_expiring_tokens(self):
        fresh = shopify_auth.token_is_fresh
        margin = shopify_auth.TOKEN_MARGIN
        self.assertEqual(
            [fresh({"access_token": "t", "token_expires_at": NOW + margin + timedelta(seconds=1)}, NOW),
             fresh({"access_token": "t", "token_expires_at": NOW + margin}, NOW),
             fresh({"access_token": "t", "token_expires_at": None}, NOW),
             fresh({"access_token": "", "token_expires_at": None}, NOW)],
            [True, False, True, False])


class TokenPostTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, requests, "post", requests.post)

    def answer(self, response):
        def post(url, data=None, headers=None, timeout=None):
            if isinstance(response, Exception):
                raise response
            return response
        requests.post = post

    def refusal_code(self):
        with self.assertRaises(shopify_auth.ShopifyAuthRefused) as caught:
            shopify_auth._token_post("tsw.myshopify.com", {})
        return caught.exception.code

    def test_json_oauth_error_code_is_kept(self):
        self.answer(FakeResponse(400, {"error": "shop_not_permitted", "error_description": "nope"}))
        self.assertEqual(self.refusal_code(), "shop_not_permitted")

    def test_plain_text_oauth_error_code_is_parsed(self):
        self.answer(FakeResponse(400, None, "Oauth error invalid_client: Client authentication failed"))
        self.assertEqual(self.refusal_code(), "invalid_client")

    def test_ok_status_without_token_is_a_refusal(self):
        self.answer(FakeResponse(200, {"scope": "read_orders"}))
        self.assertEqual(self.refusal_code(), "http_200")

    def test_network_failure_5xx_and_429_are_transient(self):
        for response in (requests.ConnectionError("down"), FakeResponse(503, None, "busy"), FakeResponse(429)):
            self.answer(response)
            with self.assertRaises(ShopifyUnavailable):
                shopify_auth._token_post("tsw.myshopify.com", {})


class FreshStoreTest(unittest.TestCase):
    """fresh_store renews an app store's token only when it runs out, and
    turns a terminal refusal into a reconnect prompt."""

    def setUp(self):
        self.row = app_store()
        self.posts, self.saved, self.failed = [], [], []
        self.responses = []
        for name in ("_now", "_read", "_save", "mark_failed"):
            self.addCleanup(setattr, shopify_auth, name, getattr(shopify_auth, name))
        self.addCleanup(setattr, shopify_auth, "db", shopify_auth.db)
        self.addCleanup(setattr, requests, "post", requests.post)
        shopify_auth.db = types.SimpleNamespace(query=lambda *a, **k: None, execute=lambda *a, **k: None)
        shopify_auth._now = lambda: NOW
        shopify_auth._read = lambda store_id: self.row
        shopify_auth._save = lambda store, grant, data: (
            self.saved.append((grant, data)) or {**store, "access_token": data["access_token"], "token_grant": grant})
        shopify_auth.mark_failed = lambda store_id, error: self.failed.append(store_id)

        def post(url, data=None, headers=None, timeout=None):
            self.posts.append((url, data))
            return self.responses.pop(0)
        requests.post = post

    def test_token_store_is_returned_untouched(self):
        store = {"id": 1, "name": "Old", "auth_method": "token", "access_token": "shpat"}
        self.assertIs(shopify_auth.fresh_store(store), store)
        self.assertEqual(self.posts, [])

    def test_fresh_app_token_needs_no_call(self):
        self.row = app_store(token_expires_at=NOW + timedelta(hours=2))
        self.assertEqual(shopify_auth.fresh_store(self.row)["access_token"], "old")
        self.assertEqual(self.posts, [])

    def test_expired_client_credentials_token_is_minted_again(self):
        self.responses = [FakeResponse(200, {"access_token": "new", "scope": "read_orders", "expires_in": 86399})]
        store = shopify_auth.fresh_store(self.row)
        self.assertEqual((store["access_token"], self.posts, [g for g, _ in self.saved]), (
            "new",
            [("https://tsw.myshopify.com/admin/oauth/access_token",
              {"grant_type": "client_credentials", "client_id": "cid", "client_secret": "csecret"})],
            ["client_credentials"]))

    def test_expired_install_token_refreshes_and_keeps_the_rotated_refresh_token(self):
        self.row = app_store(token_grant="authorization_code", refresh_token="r1",
                             refresh_expires_at=NOW + timedelta(days=30))
        body = {"access_token": "new", "expires_in": 3600, "refresh_token": "r2", "refresh_token_expires_in": 7776000}
        self.responses = [FakeResponse(200, body)]
        shopify_auth.fresh_store(self.row)
        self.assertEqual((self.posts[0][1], self.saved), (
            {"grant_type": "refresh_token", "refresh_token": "r1", "client_id": "cid", "client_secret": "csecret"},
            [("authorization_code", body)]))

    def test_refused_refresh_clears_the_connection_and_asks_for_reconnect(self):
        self.row = app_store(token_grant="authorization_code", refresh_token="r1")
        self.responses = [FakeResponse(401, {"error": "invalid_request"})]
        with self.assertRaises(ShopifyError) as caught:
            shopify_auth.fresh_store(self.row)
        self.assertEqual((str(caught.exception), self.failed),
                         (shopify_auth.RECONNECT.format(name="TSW"), [7]))

    def test_expired_refresh_token_asks_for_reconnect_without_calling_shopify(self):
        self.row = app_store(token_grant="authorization_code", refresh_token="r1",
                             refresh_expires_at=NOW - timedelta(seconds=1))
        with self.assertRaises(ShopifyError):
            shopify_auth.fresh_store(self.row)
        self.assertEqual((self.posts, self.failed), ([], [7]))

    def test_shopify_outage_during_refresh_keeps_the_tokens(self):
        self.row = app_store(token_grant="authorization_code", refresh_token="r1")
        self.responses = [FakeResponse(502, None, "bad gateway")]
        with self.assertRaises(ShopifyUnavailable):
            shopify_auth.fresh_store(self.row)
        self.assertEqual((self.saved, self.failed), ([], []))

    def test_never_connected_and_failed_stores_say_which(self):
        messages = []
        for row in (app_store(token_grant=None, access_token=""),
                    app_store(token_grant=None, access_token="", auth_error="refused")):
            with self.assertRaises(ShopifyError) as caught:
                shopify_auth.fresh_store(row)
            messages.append(str(caught.exception))
        self.assertEqual(messages, [shopify_auth.NOT_CONNECTED.format(name="TSW"),
                                    shopify_auth.RECONNECT.format(name="TSW")])

    def test_token_renewed_by_another_worker_is_reused(self):
        self.row = app_store(access_token="theirs", token_expires_at=NOW + timedelta(hours=20))
        stale = app_store()
        self.assertEqual(shopify_auth.fresh_store(stale)["access_token"], "theirs")
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()

import hashlib
import hmac
import sys
import types
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp"))

from flask import Flask  # noqa: E402

import settings_api  # noqa: E402
import shopify_auth  # noqa: E402
import shopify_client  # noqa: E402
from shopify_errors import ShopifyError  # noqa: E402

ORIGIN = "https://192.168.89.14:5558"
CALLBACK = shopify_auth.CALLBACK_PATH
SECRET = "csecret"
CREATED = datetime(2026, 11, 1, tzinfo=timezone.utc)


def store_row(**overrides):
    return {"id": 7, "name": "TSW", "shop_domain": "tsw.myshopify.com", "prefix": "", "no_company": False,
            "is_active": True, "created_at": CREATED, "auth_method": "app", "client_id": "cid",
            "client_secret": SECRET, "access_token": "", "token_grant": None, "token_expires_at": None,
            "refresh_token": None, "refresh_expires_at": None, "granted_scopes": None, "connected_at": None,
            "auth_error": None, **overrides}


def signed(params, secret=SECRET):
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return {**params, "hmac": hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()}


class ShopifyStoreRoutesTest(unittest.TestCase):
    def setUp(self):
        self.rows = {7: store_row()}
        self.executed, self.disconnected, self.exchanged, self.audits = [], [], [], []
        self.mint = lambda store: store
        db = settings_api.db
        patched = [(db, "query"), (db, "execute"), (settings_api, "audit"),
                   (shopify_auth, "mint_client_credentials"), (shopify_auth, "exchange_code"),
                   (shopify_auth, "disconnect"), (shopify_client, "check_connection")]
        for obj, name in patched:
            self.addCleanup(setattr, obj, name, getattr(obj, name))
        db.query = self.fake_query
        db.execute = self.fake_execute
        settings_api.audit = lambda action, detail=None, **k: self.audits.append(action)
        shopify_auth.mint_client_credentials = lambda store: self.mint(store)
        shopify_auth.exchange_code = lambda store, code: self.exchanged.append(code) or {
            **store, "token_grant": "authorization_code", "granted_scopes": "write_orders"}
        shopify_auth.disconnect = self.disconnected.append
        shopify_client.check_connection = lambda store_id: ("TSW Shop", ["read_orders"])
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(settings_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as s:
            s["user_id"], s["role"] = 1, "admin"

    def fake_query(self, sql, params=None, one=False):
        if "ORDER BY id" in sql:
            return list(self.rows.values())
        if "FROM shopify_stores WHERE id" in sql:
            return self.rows.get(params[0])
        return None

    def fake_execute(self, sql, params=None, returning=False):
        self.executed.append((sql, params))
        return {"id": 9} if returning else None

    def connect(self, origin=ORIGIN):
        resp = self.client.post("/api/shopify-stores/7/connect", json={"origin": origin})
        return resp, resp.get_json() or {}

    def refuse(self, code):
        def mint(store):
            raise shopify_auth.ShopifyAuthRefused(f"refused ({code})", code=code)
        self.mint = mint

    def start_install(self):
        self.refuse("shop_not_permitted")
        _, data = self.connect()
        return parse_qs(urlparse(data["url"]).query)["state"][0]

    def landed(self, resp):
        parsed = urlparse(resp.headers["Location"])
        return parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}

    # ---- connect ----
    def test_client_credentials_success_connects_without_a_redirect(self):
        resp, data = self.connect()
        self.assertEqual((resp.status_code, data), (200, {
            "ok": True, "shop": "TSW Shop", "grant": "client_credentials",
            "missing_scopes": [s for s in shopify_auth.REQUIRED_SCOPES if s != "read_orders"]}))
        self.assertEqual(self.audits, ["store.connected"])

    def test_store_outside_the_org_gets_the_install_page(self):
        self.refuse("shop_not_permitted")
        resp, data = self.connect()
        url = urlparse(data["url"])
        self.assertEqual((resp.status_code, url.netloc, url.path, data["redirect_uri"]),
                         (200, "tsw.myshopify.com", "/admin/oauth/authorize", ORIGIN + CALLBACK))
        with self.client.session_transaction() as s:
            self.assertEqual(s[settings_api.SHOPIFY_OAUTH_KEY]["store_id"], 7)

    def test_bad_client_credentials_stop_with_an_error_and_no_install(self):
        self.refuse("invalid_client")
        resp, data = self.connect()
        self.assertEqual((resp.status_code, "url" in data), (400, False))
        with self.client.session_transaction() as s:
            self.assertNotIn(settings_api.SHOPIFY_OAUTH_KEY, s)

    def test_unknown_shop_stops_with_an_error_and_no_install(self):
        self.refuse("http_404")
        resp, data = self.connect()
        self.assertEqual((resp.status_code, data), (400, {
            "error": "Shopify has no store at tsw.myshopify.com — check the shop domain"}))

    def test_connect_refuses_token_stores_and_foreign_origins(self):
        resp_origin, _ = self.connect(origin="https://evil.test/path")
        self.rows[7] = store_row(auth_method="token")
        resp_token, _ = self.connect()
        self.assertEqual((resp_origin.status_code, resp_token.status_code), (400, 404))

    # ---- callback ----
    def callback(self, params):
        return self.landed(self.client.get(f"{CALLBACK}?{urlencode(params)}"))

    def test_signed_callback_exchanges_the_code(self):
        state = self.start_install()
        path, query = self.callback(signed({"code": "c1", "shop": "tsw.myshopify.com", "state": state,
                                            "timestamp": "1762500000", "host": "abc"}))
        self.assertEqual((path, query, self.exchanged),
                         ("/settings.html", {"shopify": "connected", "store": "TSW"}, ["c1"]))

    def test_callback_rejects_forged_state_bad_signature_and_wrong_shop(self):
        cases = {
            "state": lambda st: signed({"code": "c1", "shop": "tsw.myshopify.com", "state": "forged"}),
            "hmac": lambda st: signed({"code": "c1", "shop": "tsw.myshopify.com", "state": st}, secret="wrong"),
            "shop": lambda st: signed({"code": "c1", "shop": "other.myshopify.com", "state": st}),
        }
        errors = {}
        for name, params in cases.items():
            state = self.start_install()
            errors[name] = "shopify_error" in self.callback(params(state))[1]
        self.assertEqual((errors, self.exchanged), ({"state": True, "hmac": True, "shop": True}, []))

    def test_declined_install_reports_shopifys_reason(self):
        state = self.start_install()
        _, query = self.callback({"error": "access_denied", "error_description": "Merchant declined", "state": state})
        self.assertIn("Merchant declined", query["shopify_error"])

    def test_refused_code_exchange_is_reported(self):
        def exchange(store, code):
            raise ShopifyError("Shopify refused the sign-in (invalid_request): bad code")
        shopify_auth.exchange_code = exchange
        state = self.start_install()
        _, query = self.callback(signed({"code": "c1", "shop": "tsw.myshopify.com", "state": state}))
        self.assertIn("invalid_request", query["shopify_error"])

    def test_callback_without_an_admin_session_goes_to_login(self):
        with self.client.session_transaction() as s:
            s.clear()
        self.assertEqual(self.callback({"code": "c1"})[0], "/login.html")

    def test_pasted_landing_address_completes_the_install(self):
        state = self.start_install()
        pasted = f"https://localhost{CALLBACK}?" + urlencode(signed({"code": "c2", "shop": "tsw.myshopify.com",
                                                                      "state": state}))
        resp = self.client.post("/api/shopify-stores/7/oauth/complete", json={"url": pasted})
        self.assertEqual((resp.status_code, resp.get_json(), self.exchanged),
                         (200, {"ok": True, "store": "TSW"}, ["c2"]))

    def test_disconnect_clears_an_app_store(self):
        resp = self.client.post("/api/shopify-stores/7/disconnect")
        self.assertEqual((resp.status_code, self.disconnected, self.audits), (200, [7], ["store.disconnected"]))

    # ---- create / update / list ----
    def test_create_app_store_needs_credentials_and_a_myshopify_domain(self):
        base = {"name": "New", "auth_method": "app", "client_id": "cid", "client_secret": "sec"}
        statuses = [self.client.post("/api/shopify-stores", json={**base, **extra}).status_code for extra in (
            {"shop_domain": "https://New.myshopify.com/"}, {"shop_domain": "new.example.com"},
            {"shop_domain": "new.myshopify.com", "client_secret": ""})]
        self.assertEqual(statuses, [200, 400, 400])
        self.assertEqual(self.executed[0][1], ("New", "new.myshopify.com", "", "", False, "app", "cid", "sec"))

    def test_update_with_masked_secret_keeps_credentials_and_connection(self):
        self.rows[7] = store_row(token_grant="client_credentials", access_token="minted")
        resp = self.client.put("/api/shopify-stores/7", json={"name": "TSW", "client_secret": settings_api.MASK})
        self.assertEqual((resp.get_json(), self.disconnected), ({"ok": True, "connected": True}, []))

    def test_changing_app_credentials_drops_the_connection(self):
        self.rows[7] = store_row(token_grant="client_credentials", access_token="minted")
        resp = self.client.put("/api/shopify-stores/7", json={"client_secret": "rotated"})
        self.assertEqual((resp.get_json(), self.disconnected), ({"ok": True, "connected": False}, [7]))

    def test_switching_an_app_store_to_token_needs_a_pasted_token(self):
        self.rows[7] = store_row(token_grant="client_credentials", access_token="minted")
        blank = self.client.put("/api/shopify-stores/7", json={"auth_method": "token", "access_token": ""})
        pasted = self.client.put("/api/shopify-stores/7", json={"auth_method": "token", "access_token": "shpat_1"})
        self.assertEqual((blank.status_code, pasted.status_code, self.disconnected), (400, 200, [7]))
        self.assertEqual(self.executed[-1][1], ("shpat_1", 7))

    def test_list_masks_the_secret_and_reports_connection_state(self):
        self.rows = {7: store_row(token_grant="authorization_code", granted_scopes="write_orders,read_customers"),
                     8: store_row(id=8, auth_method="token", client_secret="", access_token="shpat")}
        listed = self.client.get("/api/shopify-stores").get_json()
        self.assertEqual(
            [(s["client_secret"], s["connected"], s["token_grant"], "access_token" in s, len(s["missing_scopes"]))
             for s in listed],
            [(settings_api.MASK, True, "authorization_code", False, 4), ("", True, None, False, 0)])


if __name__ == "__main__":
    unittest.main()

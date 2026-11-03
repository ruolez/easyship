import sys
import types
import unittest
from urllib.parse import parse_qs, urlparse

# settings_api imports modules that need a live DB / network; stub them.
sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp"))

from flask import Flask  # noqa: E402

import providers  # noqa: E402
import settings_api  # noqa: E402
from providers.base import ProviderError  # noqa: E402

KEY = "stamps"
ORIGIN = "https://192.168.89.14"
CALLBACK = "/api/oauth/stamps/callback"


class FakeOAuthProvider:
    name = KEY
    label = "Endicia (direct)"
    modes = ("sandbox", "production")
    oauth_callback_path = CALLBACK

    def __init__(self):
        self.completed = []
        self.disconnected = []
        self.refuse_code = False

    def active_mode(self):
        return "sandbox"

    def authorize_url(self, mode, redirect_uri, state):
        return f"https://signin.test/authorize?mode={mode}&redirect_uri={redirect_uri}&state={state}"

    def complete_sign_in(self, mode, code, redirect_uri):
        if self.refuse_code:
            raise ProviderError("Stamps.com sign-in error (400): invalid_grant", status=400)
        self.completed.append((mode, code, redirect_uri))
        return "acme-ship"

    def disconnect(self, mode):
        self.disconnected.append(mode)


class OAuthRoutesTest(unittest.TestCase):
    def setUp(self):
        self.provider = FakeOAuthProvider()
        self.addCleanup(setattr, providers, "get_provider", providers.get_provider)
        providers.get_provider = lambda name: self.provider if name == KEY else None
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(settings_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as s:
            s["user_id"], s["role"] = 1, "admin"

    def start(self, mode="production", origin=ORIGIN):
        resp = self.client.post(f"/api/providers/{KEY}/oauth/start", json={"mode": mode, "origin": origin})
        return resp, (resp.get_json() or {})

    def state_of(self, url):
        return parse_qs(urlparse(url).query)["state"][0]

    def landed(self, resp):
        parsed = urlparse(resp.headers["Location"])
        return parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}

    def test_start_builds_the_callback_from_the_page_origin(self):
        resp, data = self.start()
        self.assertEqual((resp.status_code, data["redirect_uri"]), (200, ORIGIN + CALLBACK))
        self.assertIn("mode=production", data["url"])

    def test_start_rejects_an_origin_that_is_not_a_bare_address(self):
        for origin in ("", "javascript:alert(1)", "https://evil.test/path", "ftp://x"):
            with self.subTest(origin=origin):
                resp, _ = self.start(origin=origin)
                self.assertEqual(resp.status_code, 400)

    def test_callback_completes_the_pending_sign_in(self):
        _, data = self.start()
        resp = self.client.get(f"{CALLBACK}?code=c-1&state={self.state_of(data['url'])}")
        self.assertEqual(self.landed(resp), ("/settings.html", {
            "oauth": "connected", "provider": "Endicia (direct)", "account": "acme-ship"}))
        self.assertEqual(self.provider.completed, [("production", "c-1", ORIGIN + CALLBACK)])

    def test_callback_with_a_forged_state_completes_nothing(self):
        self.start()
        resp = self.client.get(f"{CALLBACK}?code=c-1&state=forged")
        path, query = self.landed(resp)
        self.assertEqual((path, "oauth_error" in query, self.provider.completed), ("/settings.html", True, []))

    def test_a_state_is_used_once(self):
        _, data = self.start()
        url = f"{CALLBACK}?code=c-1&state={self.state_of(data['url'])}"
        self.client.get(url)
        _, query = self.landed(self.client.get(url))
        self.assertEqual((len(self.provider.completed), "oauth_error" in query), (1, True))

    def test_a_declined_sign_in_reports_the_reason(self):
        _, data = self.start()
        resp = self.client.get(f"{CALLBACK}?error=access_denied&error_description=User+cancelled"
                               f"&state={self.state_of(data['url'])}")
        self.assertIn("User cancelled", self.landed(resp)[1]["oauth_error"])

    def test_a_refused_code_reports_the_provider_error(self):
        self.provider.refuse_code = True
        _, data = self.start()
        resp = self.client.get(f"{CALLBACK}?code=c-1&state={self.state_of(data['url'])}")
        self.assertIn("invalid_grant", self.landed(resp)[1]["oauth_error"])

    def test_callback_without_an_admin_session_goes_to_login(self):
        with self.client.session_transaction() as s:
            s.clear()
        resp = self.client.get(f"{CALLBACK}?code=c-1&state=x")
        self.assertEqual((resp.status_code, urlparse(resp.headers["Location"]).path), (302, "/login.html"))

    def test_pasted_landing_address_completes_the_sign_in(self):
        _, data = self.start()
        pasted = f"https://localhost{CALLBACK}?code=c-2&state={self.state_of(data['url'])}"
        resp = self.client.post(f"/api/providers/{KEY}/oauth/complete", json={"url": pasted})
        self.assertEqual((resp.status_code, resp.get_json()), (200, {"ok": True, "mode": "production",
                                                                     "account": "acme-ship"}))

    def test_disconnect_signs_the_mode_out(self):
        resp = self.client.post(f"/api/providers/{KEY}/oauth/disconnect", json={"mode": "production"})
        self.assertEqual((resp.status_code, self.provider.disconnected), (200, ["production"]))


if __name__ == "__main__":
    unittest.main()

import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp"))

from flask import Flask, jsonify  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import auth  # noqa: E402

PASSWORD = "packer-pass"
USER = {"id": 7, "username": "pat", "role": "user", "password_hash": generate_password_hash(PASSWORD)}


class RememberedSignInTest(unittest.TestCase):
    """"Keep me signed in" makes the session cookie outlive the browser; such
    a sign-in is checked against the users table on every request."""

    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30
        app.register_blueprint(auth.bp)

        @app.get("/private")
        @auth.login_required
        def private():
            return jsonify({"role": auth.session["role"]})

        @app.get("/admin-only")
        @auth.admin_required
        def admin_only():
            return jsonify({"ok": True})

        self.client = app.test_client()
        self.row = {"role": "user"}
        self._orig = (auth.db.query, auth.db.execute)
        auth.db.query = self._query
        auth.db.execute = lambda *a, **k: None

    def tearDown(self):
        auth.db.query, auth.db.execute = self._orig

    def _query(self, sql, params=None, one=False):
        if "password_hash" in sql or sql.startswith("SELECT * FROM users"):
            return USER
        if "can_ship" in sql:
            return {"can_ship": True}
        if "allowed_sections" in sql:
            return {"allowed_sections": []}
        return self.row

    def sign_in(self, remember):
        resp = self.client.post("/api/auth/login", json={"username": "pat", "password": PASSWORD,
                                                         "remember": remember})
        self.assertEqual(resp.status_code, 200)
        return resp.headers["Set-Cookie"]

    def test_remember_sets_an_expiring_cookie_and_a_plain_sign_in_a_browser_session_one(self):
        self.assertEqual(["Expires=" in self.sign_in(True), "Expires=" in self.sign_in(False)], [True, False])

    def test_remembered_sign_in_ends_when_the_user_is_deactivated(self):
        self.sign_in(True)
        self.row = None
        self.assertEqual([self.client.get("/private").status_code, self.client.get("/private").status_code],
                         [401, 401])

    def test_remembered_sign_in_follows_a_role_change(self):
        self.sign_in(True)
        self.row = {"role": "admin"}
        self.assertEqual((self.client.get("/private").json, self.client.get("/admin-only").status_code),
                         ({"role": "admin"}, 200))

    def test_browser_session_sign_in_is_not_rechecked(self):
        self.sign_in(False)
        self.row = None
        self.assertEqual(self.client.get("/private").status_code, 200)


if __name__ == "__main__":
    unittest.main()

import json
import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp"))

from flask import Flask, jsonify  # noqa: E402

import auth  # noqa: E402
import providers  # noqa: E402
import settings_api  # noqa: E402

KEYS = ["easyship", "shipstation-5"]
NO_ACCESS = ("Give the user at least one shipping account or one section — "
             "to remove all access, deactivate the user")


class HomePageTest(unittest.TestCase):
    def test_shippers_land_on_scan_and_others_on_their_first_section(self):
        cases = [
            (True, []), (True, ["verify"]), (False, ["verify", "verify_override"]),
            (False, ["picklists", "verify"]), (False, ["verify_override"]), (False, []),
        ]
        self.assertEqual(
            [auth.home_page(ships, sections) for ships, sections in cases],
            ["/index.html", "/index.html", "/verify.html", "/picklists.html", "/settings.html", "/settings.html"])


class UserShipsTest(unittest.TestCase):
    def run_case(self, role, row):
        orig = auth.db.query
        auth.db.query = lambda sql, params=None, one=False: row
        try:
            return auth.user_ships(7, role)
        finally:
            auth.db.query = orig

    def test_per_role_flag_and_inactive_user(self):
        self.assertEqual(
            [self.run_case("admin", {"can_ship": False}), self.run_case("user", {"can_ship": True}),
             self.run_case("user", {"can_ship": False}), self.run_case("user", None)],
            [True, True, False, False])


class ShipRequiredTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"

        @app.get("/shipping")
        @auth.ship_required
        def shipping():
            return jsonify({"ok": True})

        self.client = app.test_client()
        self.ships = True
        self._orig = auth.db.query
        auth.db.query = lambda sql, params=None, one=False: {"can_ship": self.ships}

    def tearDown(self):
        auth.db.query = self._orig

    def status(self, sess):
        with self.client.session_transaction() as s:
            s.clear()
            s.update(sess)
        return self.client.get("/shipping").status_code

    def test_refuses_signed_out_and_non_shipping_users(self):
        signed_out = self.status({})
        shipper = self.status({"user_id": 7, "role": "user"})
        self.ships = False
        verify_only = self.status({"user_id": 7, "role": "user"})
        admin = self.status({"user_id": 1, "role": "admin"})
        self.assertEqual([signed_out, shipper, verify_only, admin], [401, 200, 403, 200])


class EnabledForNonShipperTest(unittest.TestCase):
    def test_a_user_who_does_not_ship_gets_no_account(self):
        class P:
            def __init__(self, name):
                self.name = name
        orig = (providers.enabled_providers, providers.db.query)
        providers.enabled_providers = lambda: [P("easyship")]
        providers.db.query = lambda sql, params=None, one=False: {"allowed_providers": None, "can_ship": False}
        try:
            self.assertEqual(providers.enabled_for_user(7, "user"), [])
        finally:
            providers.enabled_providers, providers.db.query = orig


class SetUserAccessRouteTest(unittest.TestCase):
    """PUT /api/users/<id>/access saves shipping accounts and sections together:
    no account checked means the user does not ship, which needs a section."""

    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(settings_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess.update({"user_id": 1, "role": "admin"})
        self.role = "user"
        self.writes = []
        self._orig = (settings_api.db.query, settings_api.db.execute, providers.instance_keys)
        settings_api.db.query = lambda sql, params=None, one=False: {"id": 7, "role": self.role, "username": "pat"}
        settings_api.db.execute = lambda sql, params=None, returning=False: (
            self.writes.append(params) if sql.lstrip().startswith("UPDATE users") else None)
        providers.instance_keys = lambda: KEYS

    def tearDown(self):
        settings_api.db.query, settings_api.db.execute, providers.instance_keys = self._orig

    def put(self, accounts, sections):
        resp = self.client.put("/api/users/7/access",
                               json={"allowed_providers": accounts, "allowed_sections": sections})
        return resp.status_code, resp.get_json()

    def test_verify_only_user_is_stored_as_not_shipping(self):
        self.assertEqual(
            (self.put([], ["verify", "bogus"]), self.writes),
            ((200, {"ok": True, "can_ship": False, "allowed_providers": None, "allowed_sections": ["verify"]}),
             [(False, None, json.dumps(["verify"]), 7)]))

    def test_a_subset_of_accounts_ships_and_all_accounts_is_unrestricted(self):
        self.assertEqual(
            ([self.put(["shipstation-5"], [])[1]["allowed_providers"], self.put(KEYS, ["picklists"])[1]["allowed_providers"]],
             self.writes),
            ([["shipstation-5"], None],
             [(True, json.dumps(["shipstation-5"]), None, 7), (True, None, json.dumps(["picklists"]), 7)]))

    def test_nothing_at_all_is_refused_without_writing(self):
        self.assertEqual(
            ([self.put([], []), self.put(["bogus"], ["nope"])], self.writes),
            ([(400, {"error": NO_ACCESS})] * 2, []))

    def test_admins_cannot_be_restricted(self):
        self.role = "admin"
        self.assertEqual((self.put([], ["verify"])[0], self.writes), (400, []))


class CreateUserAccessTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(settings_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess.update({"user_id": 1, "role": "admin"})
        self.inserts = []
        self._orig = (settings_api.db.query, settings_api.db.execute, providers.instance_keys)
        settings_api.db.query = lambda sql, params=None, one=False: None
        settings_api.db.execute = lambda sql, params=None, returning=False: (
            self.inserts.append(params[2:]) or {"id": 9} if sql.lstrip().startswith("INSERT INTO users") else None)
        providers.instance_keys = lambda: KEYS

    def tearDown(self):
        settings_api.db.query, settings_api.db.execute, providers.instance_keys = self._orig

    def create(self, role, accounts, sections):
        return self.client.post("/api/users", json={
            "username": "misha", "password": "secret", "role": role,
            "allowed_providers": accounts, "allowed_sections": sections}).status_code

    def test_verify_only_user_is_created_and_a_user_with_nothing_is_refused(self):
        self.assertEqual(
            ([self.create("user", [], ["verify"]), self.create("user", [], []), self.create("admin", [], [])],
             self.inserts),
            ([200, 400, 200],
             [("user", False, None, json.dumps(["verify"])), ("admin", True, None, None)]))


if __name__ == "__main__":
    unittest.main()

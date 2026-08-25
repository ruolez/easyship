import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

import providers  # noqa: E402


class FakeProvider:
    def __init__(self, name):
        self.name = name


def patch(registered, enabled, assignments):
    """Swap the configured instances, enablement and users table for one test."""
    orig = (providers.instance_keys, providers.enabled_providers, providers.db.query)
    providers.instance_keys = lambda: list(registered)
    providers.enabled_providers = lambda: [FakeProvider(n) for n in enabled]
    providers.db.query = lambda sql, params=None, one=False: (
        {"allowed_providers": assignments.get(params[0])} if params and params[0] in assignments else None)
    return orig


def restore(orig):
    providers.instance_keys, providers.enabled_providers, providers.db.query = orig


REGISTERED = ["easyship", "shippo", "easypost", "shipstation", "shipstation-5"]


class EnabledForUserTest(unittest.TestCase):
    def run_case(self, enabled, assignments, user_id, role):
        orig = patch(REGISTERED, enabled, assignments)
        try:
            return [p.name for p in providers.enabled_for_user(user_id, role)]
        finally:
            restore(orig)

    def test_admin_always_gets_every_enabled_provider(self):
        self.assertEqual(
            self.run_case(["easyship", "shippo"], {7: ["shippo"]}, 7, "admin"),
            ["easyship", "shippo"])

    def test_unassigned_user_gets_every_enabled_provider(self):
        self.assertEqual(
            self.run_case(["easyship", "shippo"], {7: None}, 7, "user"),
            ["easyship", "shippo"])

    def test_assigned_user_is_restricted_to_the_intersection(self):
        self.assertEqual(
            self.run_case(["easyship", "shippo", "shipstation"], {7: ["shippo", "easypost"]}, 7, "user"),
            ["shippo"])

    def test_empty_intersection_stays_empty_without_easyship_fallback(self):
        self.assertEqual(
            self.run_case(["easyship"], {7: ["shipstation"]}, 7, "user"),
            [])

    def test_globally_disabled_assignment_is_filtered_out(self):
        self.assertEqual(
            self.run_case(["easyship", "shippo"], {7: ["easyship", "easypost"]}, 7, "user"),
            ["easyship"])

    def test_second_instance_of_a_platform_is_permissioned_on_its_own(self):
        self.assertEqual(
            self.run_case(["shipstation", "shipstation-5"], {7: ["shipstation-5"]}, 7, "user"),
            ["shipstation-5"])


class SanitizeAllowedTest(unittest.TestCase):
    def run_case(self, names):
        orig = patch(REGISTERED, REGISTERED, {})
        try:
            return providers.sanitize_allowed(names)
        finally:
            restore(orig)

    def test_unknown_names_are_dropped_and_order_is_creation_order(self):
        self.assertEqual(self.run_case(["shipstation-5", "bogus", "easyship"]), ["easyship", "shipstation-5"])

    def test_none_and_empty_mean_unrestricted(self):
        self.assertEqual((self.run_case(None), self.run_case([])), (None, None))

    def test_selecting_everything_means_unrestricted(self):
        self.assertEqual(self.run_case(list(reversed(REGISTERED))), None)


class EnabledRouteTest(unittest.TestCase):
    """GET /api/providers/enabled returns only what the caller may use."""

    def setUp(self):
        from flask import Flask
        import settings_api
        self.settings_api = settings_api
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.register_blueprint(settings_api.bp)

    def call(self, user_id, role):
        from flask import session
        with self.app.test_request_context("/api/providers/enabled"):
            session["user_id"] = user_id
            session["role"] = role
            import json
            return [p["name"] for p in json.loads(self.settings_api.enabled_providers().get_data())]

    def test_user_sees_subset_and_admin_sees_all(self):
        class FakeFull(FakeProvider):
            label = "X"
            def is_test_mode(self):
                return False
        orig = (providers.instance_keys, providers.enabled_providers, providers.db.query)
        providers.instance_keys = lambda: REGISTERED
        providers.enabled_providers = lambda: [FakeFull("easyship"), FakeFull("shippo")]
        providers.db.query = lambda sql, params=None, one=False: {"allowed_providers": ["shippo"]}
        try:
            self.assertEqual((self.call(7, "user"), self.call(1, "admin")),
                             (["shippo"], ["easyship", "shippo"]))
        finally:
            providers.instance_keys, providers.enabled_providers, providers.db.query = orig


class SetUserProvidersRouteTest(unittest.TestCase):
    """PUT /api/users/<id>/providers refuses an empty selection — there is no
    'no accounts' state, so nothing checked must not mean unrestricted."""

    def setUp(self):
        from flask import Flask
        import settings_api
        self.settings_api = settings_api
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.register_blueprint(settings_api.bp)
        self._orig = (providers.instance_keys, providers.db.query, providers.db.execute)
        providers.instance_keys = lambda: ["easyship", "shipstation-5"]
        providers.db.query = lambda sql, params=None, one=False: {"id": 7, "role": "user", "username": "pat"}
        self.writes = []  # UPDATE users writes only; the audit-log INSERT is filtered out
        providers.db.execute = lambda sql, params=None, returning=False: (
            self.writes.append(params) if sql.lstrip().startswith("UPDATE users") else None)

    def tearDown(self):
        providers.instance_keys, providers.db.query, providers.db.execute = self._orig

    def call(self, allowed):
        import json
        from flask import session
        with self.app.test_request_context("/api/users/7/providers", method="PUT", json={"allowed_providers": allowed}):
            session["user_id"] = 1
            session["role"] = "admin"
            resp = self.settings_api.set_user_providers(7)
            body, status = (resp, 200) if not isinstance(resp, tuple) else resp
            return status, json.loads(body.get_data())

    def test_empty_and_unknown_selections_are_refused_without_writing(self):
        self.assertEqual(
            (self.call([])[0], self.call(["bogus"])[0], self.writes),
            (400, 400, []))

    def test_a_real_subset_is_stored(self):
        status, body = self.call(["shipstation-5"])
        self.assertEqual((status, body["allowed_providers"], self.writes), (200, ["shipstation-5"], [('["shipstation-5"]', 7)]))


if __name__ == "__main__":
    unittest.main()

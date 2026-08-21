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
    """Swap the registry, enablement and users table for one test."""
    orig = (providers.registered_names, providers.enabled_providers, providers.db.query)
    providers.registered_names = lambda: list(registered)
    providers.enabled_providers = lambda: [FakeProvider(n) for n in enabled]
    providers.db.query = lambda sql, params=None, one=False: (
        {"allowed_providers": assignments.get(params[0])} if params and params[0] in assignments else None)
    return orig


def restore(orig):
    providers.registered_names, providers.enabled_providers, providers.db.query = orig


REGISTERED = ["easyship", "shippo", "easypost", "shipstation"]


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


class SanitizeAllowedTest(unittest.TestCase):
    def run_case(self, names):
        orig = patch(REGISTERED, REGISTERED, {})
        try:
            return providers.sanitize_allowed(names)
        finally:
            restore(orig)

    def test_unknown_names_are_dropped_and_order_is_registration_order(self):
        self.assertEqual(self.run_case(["shipstation", "bogus", "easyship"]), ["easyship", "shipstation"])

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
        orig = (providers.registered_names, providers.enabled_providers, providers.db.query)
        providers.registered_names = lambda: REGISTERED
        providers.enabled_providers = lambda: [FakeFull("easyship"), FakeFull("shippo")]
        providers.db.query = lambda sql, params=None, one=False: {"allowed_providers": ["shippo"]}
        try:
            self.assertEqual((self.call(7, "user"), self.call(1, "admin")),
                             (["shippo"], ["easyship", "shippo"]))
        finally:
            providers.registered_names, providers.enabled_providers, providers.db.query = orig


if __name__ == "__main__":
    unittest.main()

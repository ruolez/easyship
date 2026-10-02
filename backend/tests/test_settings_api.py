import json
import sys
import types
import unittest

# settings_api imports modules that need a live DB / network; stub them.
sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp"))

from flask import Flask, session  # noqa: E402
from werkzeug.security import check_password_hash  # noqa: E402

import providers  # noqa: E402
import settings_api  # noqa: E402

CATALOG = [
    {"id": "ups_ground", "name": "UPS Ground", "umbrella_name": "UPS"},
    {"id": "ups_nda", "name": "UPS Next Day Air", "umbrella_name": "UPS"},
    {"id": "fedex_ground", "name": "FedEx Ground", "umbrella_name": "FedEx"},
]


class FakeProvider:
    name = "fake"
    label = "Fake"

    def __init__(self, catalog, excluded):
        self.catalog = catalog
        self.excluded = excluded

    def list_courier_services(self):
        return list(self.catalog)

    def get_excluded_service_ids(self):
        return set(self.excluded)


class AvailableServicesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.register_blueprint(settings_api.bp)
        self._orig = (providers.get_provider, providers.enabled_for_user)

    def tearDown(self):
        providers.get_provider, providers.enabled_for_user = self._orig

    def _call(self, provider):
        providers.get_provider = lambda name: provider
        providers.enabled_for_user = lambda user_id, role: [provider]
        with self.app.test_request_context("/api/providers/fake/services/available"):
            session["user_id"] = 1
            return json.loads(settings_api.provider_available_services("fake").get_data())

    def test_excluded_services_and_empty_carriers_are_dropped(self):
        self.assertEqual(self._call(FakeProvider(CATALOG, {"ups_nda", "fedex_ground"})), {
            "has_catalog": True,
            "services": [{"id": "ups_ground", "name": "UPS Ground", "umbrella_name": "UPS"}],
        })

    def test_provider_without_catalog(self):
        self.assertEqual(self._call(FakeProvider([], set())), {"has_catalog": False, "services": []})


PASSWORD = "open-sesame"


class ProfitSettingsTest(unittest.TestCase):
    """The profit thresholds must be numbers, need a bypass password on file
    before they can be turned on, and the password is stored hashed."""

    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(settings_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "admin"
        self.stored = {}
        db = settings_api.db
        self._orig = (db.get_setting, db.set_setting, providers.descriptors,
                      providers.enabled_providers, settings_api.audit)
        db.get_setting = lambda key, default=None: self.stored.get(key, default)
        db.set_setting = lambda key, value: self.stored.__setitem__(key, value)
        providers.descriptors = lambda: []
        providers.enabled_providers = lambda: []
        settings_api.audit = lambda *a, **k: None

    def tearDown(self):
        db = settings_api.db
        (db.get_setting, db.set_setting, providers.descriptors,
         providers.enabled_providers, settings_api.audit) = self._orig

    def _put(self, body):
        res = self.client.put("/api/settings", json=body)
        return res.status_code, json.loads(res.get_data())

    def test_a_threshold_without_a_password_is_refused(self):
        self.assertEqual(self._put({"profit_min_amount": "5"}),
                         (400, {"error": "Set a bypass password before turning on the profit check"}))
        self.assertEqual(self.stored, {})

    def test_the_password_is_stored_hashed_and_the_mask_leaves_it_alone(self):
        status, _ = self._put({"profit_min_amount": "5", "profit_bypass_password": PASSWORD})
        self.assertEqual(status, 200)
        stored = self.stored["profit_bypass_password"]
        self.assertNotEqual(stored, PASSWORD)
        self.assertTrue(check_password_hash(stored, PASSWORD))
        self._put({"profit_min_margin_pct": "20", "profit_bypass_password": settings_api.MASK})
        self.assertEqual((self.stored["profit_bypass_password"], self.stored["profit_min_margin_pct"]),
                         (stored, "20"))

    def test_thresholds_must_be_numbers_in_range(self):
        self.stored["profit_bypass_password"] = "hash"
        cases = {("profit_min_amount", "five"): "Minimum profit must be a number",
                 ("profit_min_amount", "-1"): "Minimum profit cannot be negative",
                 ("profit_min_margin_pct", "101"): "Minimum margin cannot exceed 100"}
        for (key, value), error in cases.items():
            with self.subTest(key=key, value=value):
                self.assertEqual(self._put({key: value}), (400, {"error": error}))

    def test_clearing_the_password_while_the_check_is_on_is_refused(self):
        self.stored.update({"profit_bypass_password": "hash", "profit_min_amount": "5"})
        self.assertEqual(self._put({"profit_bypass_password": ""})[0], 400)
        self.assertEqual(self.stored["profit_bypass_password"], "hash")

    def test_blank_thresholds_turn_the_check_off_without_a_password(self):
        self.assertEqual(self._put({"profit_min_amount": "", "profit_min_margin_pct": ""})[0], 200)

    def test_client_settings_report_whether_the_check_is_on(self):
        def enabled():
            return json.loads(self.client.get("/api/settings/client").get_data())["profit_gate_enabled"]
        self.assertFalse(enabled())
        self.stored["profit_min_margin_pct"] = "20"
        self.assertTrue(enabled())


if __name__ == "__main__":
    unittest.main()

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

import providers  # noqa: E402
import settings_api  # noqa: E402

CATALOG = [
    {"id": "ups_ground", "name": "UPS Ground", "umbrella_name": "UPS"},
    {"id": "ups_nda", "name": "UPS Next Day Air", "umbrella_name": "UPS"},
    {"id": "fedex_ground", "name": "FedEx Ground", "umbrella_name": "FedEx"},
]


class FakeProvider:
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
        self._orig = (providers.get_provider, providers.registered_names)
        providers.registered_names = lambda: ["fake"]

    def tearDown(self):
        providers.get_provider, providers.registered_names = self._orig

    def _call(self, provider):
        providers.get_provider = lambda name: provider
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


if __name__ == "__main__":
    unittest.main()

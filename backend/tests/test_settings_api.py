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

import auth  # noqa: E402
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
        self.addCleanup(setattr, auth, "user_ships", auth.user_ships)
        auth.user_ships = lambda user_id, role: True  # access itself: test_user_access
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

    def test_the_audit_names_only_the_settings_that_changed(self):
        audits = []
        settings_api.audit = lambda action, detail=None, **k: audits.append((action, detail))
        self.stored.update({"printer_host": "10.0.0.5", "printer_port": "9100"})
        self._put({"printer_host": "10.0.0.9", "printer_port": "9100", "label_timeout_seconds": "",
                   "profit_bypass_password": PASSWORD})
        self._put({"printer_host": "10.0.0.9", "profit_bypass_password": settings_api.MASK})
        self.assertEqual(audits, [("settings.update", {"keys": ["printer_host", "profit_bypass_password"]}),
                                  ("settings.update", {"keys": []})])

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


class PicklistRulesSettingsTest(unittest.TestCase):
    """Rules saved before an operator was renamed come back in today's form,
    so the editor shows them as they still match."""

    setUp = ProfitSettingsTest.setUp
    tearDown = ProfitSettingsTest.tearDown

    def test_saved_tag_rules_are_read_back_with_contains(self):
        legacy = [{"id": "w", "name": "Wholesale", "action": "hide", "match": "all",
                   "conditions": [{"field": "order_tag", "op": "has", "value": "wholesale"},
                                  {"field": "customer_tag", "op": "not_has", "value": "vip"}]}]
        self.stored["picklist_rules"] = json.dumps(legacy)
        out = json.loads(self.client.get("/api/settings").get_data())["picklist_rules"]
        self.assertEqual(json.loads(out), [{**legacy[0], "conditions": [
            {"field": "order_tag", "op": "contains", "value": "wholesale"},
            {"field": "customer_tag", "op": "not_contains", "value": "vip"}]}])

    def test_no_saved_rules_reads_back_empty(self):
        self.assertEqual(json.loads(self.client.get("/api/settings").get_data())["picklist_rules"], "")


class PicklistPresetsSettingsTest(unittest.TestCase):
    """Presets point at rules by id, so they are checked against the rules
    that will be in place after the save."""

    setUp = ProfitSettingsTest.setUp
    tearDown = ProfitSettingsTest.tearDown

    RULE = {"id": "big", "name": "Big orders", "action": "keep", "match": "all",
            "conditions": [{"field": "order_total", "op": "at_least", "value": 300}]}

    def preset(self, *rule_ids):
        return {"id": "p1", "name": "Retail", "join": "and",
                "groups": [{"join": "or", "items": [{"rule": r, "negate": False} for r in rule_ids]}]}

    def put(self, **body):
        res = self.client.put("/api/settings", json=body)
        return res.status_code

    def test_presets_keep_only_rules_that_are_stored(self):
        self.stored["picklist_rules"] = json.dumps([self.RULE])
        self.put(picklist_presets=json.dumps([self.preset("big", "gone")]))
        self.assertEqual(json.loads(self.stored["picklist_presets"]), [self.preset("big")])

    def test_presets_are_checked_against_rules_saved_at_the_same_time(self):
        self.stored["picklist_rules"] = json.dumps([self.RULE])
        self.put(picklist_rules="[]", picklist_presets=json.dumps([self.preset("big")]))
        self.assertEqual((self.stored["picklist_rules"], self.stored["picklist_presets"]), ("[]", "[]"))

    def test_presets_read_back_without_items_of_deleted_rules(self):
        self.stored["picklist_rules"] = json.dumps([self.RULE])
        self.stored["picklist_presets"] = json.dumps([self.preset("big", "gone")])
        out = json.loads(self.client.get("/api/settings").get_data())["picklist_presets"]
        self.assertEqual(json.loads(out), [self.preset("big")])


if __name__ == "__main__":
    unittest.main()

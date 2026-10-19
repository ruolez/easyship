import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

from providers import base  # noqa: E402

GLOBAL = {
    "origin_company": "Global Co", "origin_contact": "Ann", "origin_address1": "1 Main",
    "origin_address2": "", "origin_city": "Austin", "origin_state": "TX", "origin_zip": "78701",
    "origin_phone": "555", "origin_email": "ops@global.test",
}
OWN = {
    "shipstation-5_origin_company": "East Co", "shipstation-5_origin_address1": "9 Dock",
    "shipstation-5_origin_city": "Newark", "shipstation-5_origin_state": "NJ",
    "shipstation-5_origin_zip": "07101", "shipstation-5_origin_phone": " 777 ",
}
ADDRESS_REQUIRED = {
    "origin_address1": "Address 1", "origin_city": "City", "origin_state": "State", "origin_zip": "ZIP",
}


def with_settings(values):
    orig = base.db.get_setting
    base.db.get_setting = lambda key, default=None: values.get(key, default)
    return orig


class OriginSettingsTest(unittest.TestCase):
    def test_global_origin_when_override_is_off(self):
        orig = with_settings({**GLOBAL, **OWN})
        try:
            self.assertEqual(base.origin_settings("shipstation-5"), GLOBAL)
        finally:
            base.db.get_setting = orig

    def test_override_on_takes_own_address_and_falls_back_per_contact_field(self):
        orig = with_settings({**GLOBAL, **OWN, "shipstation-5_origin_override": "true"})
        try:
            out = base.origin_settings("shipstation-5")
        finally:
            base.db.get_setting = orig
        self.assertEqual(out, {
            "origin_company": "East Co", "origin_contact": "Ann", "origin_address1": "9 Dock",
            "origin_address2": "", "origin_city": "Newark", "origin_state": "NJ", "origin_zip": "07101",
            "origin_phone": "777", "origin_email": "ops@global.test",
        })

    def test_override_with_only_phone_and_email_keeps_the_global_address(self):
        orig = with_settings({
            **GLOBAL, "shipstation-5_origin_override": "true",
            "shipstation-5_origin_phone": "777", "shipstation-5_origin_email": "east@acct.test",
        })
        try:
            out = base.origin_settings("shipstation-5")
        finally:
            base.db.get_setting = orig
        self.assertEqual(out, {**GLOBAL, "origin_phone": "777", "origin_email": "east@acct.test"})

    def test_own_address_never_inherits_a_global_address_line(self):
        orig = with_settings({
            **GLOBAL, "origin_address2": "Suite 100", **OWN, "shipstation-5_origin_override": "true",
        })
        try:
            out = base.origin_settings("shipstation-5")
        finally:
            base.db.get_setting = orig
        self.assertEqual((out["origin_address1"], out["origin_address2"]), ("9 Dock", ""))

    def test_partial_own_address_reports_the_missing_address_fields(self):
        orig = with_settings({
            **GLOBAL, "shipstation-5_origin_override": "true", "shipstation-5_origin_address1": "9 Dock",
        })
        try:
            missing = base.missing_origin_fields(base.origin_settings("shipstation-5"), ADDRESS_REQUIRED)
        finally:
            base.db.get_setting = orig
        self.assertEqual(missing, ["City", "State", "ZIP"])

    def test_override_on_with_nothing_set_is_the_global_origin(self):
        orig = with_settings({**GLOBAL, "shipstation-5_origin_override": "true"})
        try:
            self.assertEqual(base.origin_settings("shipstation-5"), GLOBAL)
        finally:
            base.db.get_setting = orig

    def test_descriptor_keys_are_namespaced_by_instance(self):
        d = base.origin_descriptor("shipstation-5")
        self.assertEqual(
            (d["origin_override_key"], [f["key"] for f in d["origin_fields"]][:2], len(d["origin_fields"])),
            ("shipstation-5_origin_override",
             ["shipstation-5_origin_company", "shipstation-5_origin_contact"], 9))

    def test_descriptor_marks_the_street_address_fields_as_one_unit(self):
        d = base.origin_descriptor("shipstation-5")
        self.assertEqual(
            [f["key"] for f in d["origin_fields"] if f.get("address")],
            [f"shipstation-5_origin_{f}" for f in ("address1", "address2", "city", "state", "zip")])


class ProviderIdentityTest(unittest.TestCase):
    def test_no_arg_constructor_is_the_primary_instance(self):
        class P(base.ShippingProvider):
            platform = "acme"
            label = "Acme"
            create_draft_shipments = get_excluded_service_ids = set_excluded_service_ids = None
            buy_labels = poll_shipments = fetch_labels = cancel_all = None
            list_item_categories = list_courier_services = active_mode = None
            test_connection = descriptor = None
        primary, second = P(), P("acme-7", "Acme West")
        self.assertEqual(
            ((primary.name, primary.label, primary.setting_key("token")),
             (second.name, second.label, second.platform, second.platform_label, second.setting_key("token"))),
            (("acme", "Acme", "acme_token"), ("acme-7", "Acme West", "acme", "Acme", "acme-7_token")))


if __name__ == "__main__":
    unittest.main()

import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

from providers import shipstation as ss  # noqa: E402
from providers.endicia import NOT_CONNECTED, NOT_SELECTED, EndiciaProvider  # noqa: E402
from providers.base import LabelStatus, ProviderError  # noqa: E402
from tests.test_shipstation_provider import rate  # noqa: E402

KEY, ALIAS = "endicia-7", "Endicia"
ENDICIA, UPS = "se-300", "se-100"
CARRIERS = [
    {"carrier_id": UPS, "carrier_code": "ups", "friendly_name": "UPS", "nickname": "UPS",
     "services": [{"service_code": "ups_ground", "name": "UPS® Ground"}]},
    {"carrier_id": ENDICIA, "carrier_code": "stamps_com", "friendly_name": "Stamps.com", "nickname": "Endicia",
     "services": [{"service_code": "usps_priority_mail", "name": "USPS Priority Mail"},
                  {"service_code": "usps_ground_advantage", "name": "USPS Ground Advantage"}]},
]


def settings(carrier_id=ENDICIA, **extra):
    values = {f"{KEY}_api_key": "key", f"{KEY}_carrier_id": carrier_id, **extra}
    return lambda key, default=None: values.get(key, default)


class EndiciaTest(unittest.TestCase):
    def setUp(self):
        self._orig = (ss._request, ss._carriers, ss._origin_address, ss.db.get_setting)
        self.calls = []
        ss._carriers = lambda auth, force=False: CARRIERS
        ss._origin_address = lambda *a, **k: {"name": "W", "address_line1": "9 Dock", "city_locality": "D",
                                              "state_province": "TX", "postal_code": "2", "country_code": "US"}
        ss.db.get_setting = settings()
        self.provider = EndiciaProvider(KEY, ALIAS)

    def tearDown(self):
        ss._request, ss._carriers, ss._origin_address, ss.db.get_setting = self._orig

    def fake_rates(self, rates):
        def fake_request(method, path, json_body=None, params=None, timeout=45, auth=None):
            self.calls.append((method, path, json_body))
            return {"shipment_id": "se-s-1", "rate_response": {"rates": rates}}
        ss._request = fake_request


class RatingTest(EndiciaTest):
    def test_rating_sends_only_the_selected_carrier_id(self):
        self.fake_rates([])
        self.provider.create_draft_shipments({"address1": "1 Main"}, [{"weight": 1}], [])
        bodies = [b for m, p, b in self.calls if (m, p) == ("POST", "/v2/rates")]
        self.assertEqual([b["rate_options"]["carrier_ids"] for b in bodies], [[ENDICIA]])

    def test_rates_carry_the_alias_as_umbrella(self):
        self.fake_rates([
            {**rate(UPS, "ups_ground", 9.0), "carrier_id": ENDICIA, "carrier_code": "stamps_com",
             "carrier_friendly_name": "Stamps.com", "service_code": "usps_priority_mail"},
        ])
        _, rates, warnings = self.provider.create_draft_shipments({"address1": "1 Main"}, [{"weight": 1}], [])
        self.assertEqual(
            ([(r.provider, r.provider_service_id, r.courier_name, r.umbrella_name) for r in rates], warnings),
            ([(KEY, f"{ENDICIA}:usps_priority_mail", "USPS Priority Mail", ALIAS)], []))

    def test_unset_carrier_raises_settings_hint(self):
        ss.db.get_setting = settings(carrier_id="")
        self.fake_rates([])
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_draft_shipments({"address1": "1 Main"}, [{"weight": 1}], [])
        self.assertEqual((str(ctx.exception), self.calls), (NOT_SELECTED, []))

    def test_missing_carrier_raises(self):
        ss.db.get_setting = settings(carrier_id="se-999")
        self.fake_rates([])
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_draft_shipments({"address1": "1 Main"}, [{"weight": 1}], [])
        self.assertEqual((str(ctx.exception), self.calls), (NOT_CONNECTED, []))

    def test_courier_services_are_filtered_to_the_carrier(self):
        self.assertEqual(self.provider.list_courier_services(), [
            {"id": f"{ENDICIA}:usps_ground_advantage", "umbrella_name": ALIAS, "name": "USPS Ground Advantage"},
            {"id": f"{ENDICIA}:usps_priority_mail", "umbrella_name": ALIAS, "name": "USPS Priority Mail"},
        ])


class BuyTest(EndiciaTest):
    def setUp(self):
        super().setUp()

        def fake_request(method, path, json_body=None, params=None, timeout=45, auth=None):
            self.calls.append((method, path, json_body))
            if (method, path) == ("GET", "/v2/labels"):
                return {"labels": []}
            if method == "GET" and path.startswith("/v2/shipments/"):
                return {"shipment_id": "se-s-9",
                        "ship_to": {"name": "A", "address_line1": "1 Main", "city_locality": "X",
                                    "state_province": "TX", "postal_code": "1", "country_code": "US"},
                        "ship_from": None,
                        "packages": [{"weight": {"value": 1, "unit": "pound"}}]}
            if (method, path) == ("POST", "/v2/labels"):
                return {"label_id": "se-l-9", "status": "completed", "carrier_id": ENDICIA,
                        "carrier_code": "stamps_com", "service_code": "usps_priority_mail",
                        "tracking_number": "9400", "shipment_cost": {"currency": "usd", "amount": 7.5}}
            raise AssertionError(f"unexpected {method} {path}")

        ss._request = fake_request

    def test_bought_label_uses_alias_umbrella(self):
        out = self.provider.buy_labels(["se-s-9"], f"{ENDICIA}:usps_priority_mail")
        state = out["se-s-9"]
        self.assertEqual(
            (state.label_status, state.tracking_numbers, state.courier_name, state.courier_umbrella_name, state.cost),
            (LabelStatus.READY, ["9400"], "USPS Priority Mail", ALIAS, 7.5))

    def test_buy_rejects_service_from_another_carrier(self):
        with self.assertRaises(ProviderError):
            self.provider.buy_labels(["se-s-9"], f"{UPS}:ups_ground")
        self.assertEqual(self.calls, [])

    def test_polled_label_keeps_alias_after_carrier_reselection(self):
        ss.db.get_setting = settings(carrier_id=UPS)
        ss._request = lambda method, path, json_body=None, params=None, timeout=45, auth=None: {
            "labels": [{"label_id": "se-l-9", "status": "completed", "carrier_id": ENDICIA,
                        "carrier_code": "stamps_com", "service_code": "usps_priority_mail"}]}
        state = self.provider.poll_shipments(["se-l-9"])["se-l-9"]
        self.assertEqual(state.courier_umbrella_name, ALIAS)


class SettingsSurfaceTest(EndiciaTest):
    def test_descriptor_has_carrier_select_with_options_endpoint(self):
        d = self.provider.descriptor()
        carrier = next(f for f in d["fields"] if f["key"] == f"{KEY}_carrier_id")
        self.assertEqual(
            (d["name"], d["label"], d["platform"], d["platform_label"],
             [f["key"] for f in d["fields"] if f["type"] == "secret"],
             carrier["type"], carrier["options_endpoint"], d["supports"], d["services_endpoint"]),
            (KEY, ALIAS, "endicia", "Endicia (via ShipStation)", [f"{KEY}_api_key"],
             "select", f"/api/providers/{KEY}/carriers", {"service_exclusions": True},
             f"/api/providers/{KEY}/services"))

    def test_descriptor_fields_are_namespaced_by_the_instance_key(self):
        d = self.provider.descriptor()
        keys = [d["enabled_key"], d["origin_override_key"]] + [f["key"] for f in d["fields"]] \
            + [f["key"] for f in d["origin_fields"]]
        self.assertTrue(all(k.startswith(f"{KEY}_") for k in keys), keys)

    def test_connection_summary_names_selected_carrier(self):
        self.assertEqual(self.provider._connection_summary(CARRIERS),
                         "Endicia — Stamps.com · Endicia, 2 service(s)")

    def test_connection_summary_flags_test_labels(self):
        ss.db.get_setting = settings(**{f"{KEY}_test_labels": "true"})
        self.assertEqual(self.provider._connection_summary(CARRIERS),
                         "Endicia — Stamps.com · Endicia, 2 service(s) — test labels ON (no charge)")

    def test_connection_summary_raises_when_carrier_absent(self):
        ss.db.get_setting = settings(carrier_id="se-999")
        with self.assertRaises(ProviderError) as ctx:
            self.provider._connection_summary(CARRIERS)
        self.assertEqual(str(ctx.exception), NOT_CONNECTED)


if __name__ == "__main__":
    unittest.main()

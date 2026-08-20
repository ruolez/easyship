import sys
import types
import unittest

# The provider reads settings through db; stub it so the pure helpers import
# without a live Postgres. Tests that need a setting patch `ss.db.get_setting`.
sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

from providers import shipstation as ss  # noqa: E402
from providers.base import LabelStatus, ProviderError  # noqa: E402

UPS, FEDEX = "se-100", "se-200"
CATALOG = {(UPS, "ups_ground"): "UPS® Ground", (UPS, "ups_nda"): "UPS Next Day Air®",
           (FEDEX, "fedex_ground"): "FedEx Ground®"}
NAMES = {UPS: "UPS", FEDEX: "FedEx"}


def rate(carrier_id, service_code, shipping, other=0.0, confirmation=0.0, days=3, **extra):
    return {
        "rate_id": f"se-r-{carrier_id}-{service_code}-{shipping}",
        "carrier_id": carrier_id,
        "carrier_code": "ups" if carrier_id == UPS else "fedex",
        "carrier_friendly_name": NAMES[carrier_id],
        "service_code": service_code,
        "service_type": service_code.upper(),
        "shipping_amount": {"currency": "usd", "amount": shipping},
        "other_amount": {"currency": "usd", "amount": other},
        "insurance_amount": {"currency": "usd", "amount": 0.0},
        "confirmation_amount": {"currency": "usd", "amount": confirmation},
        "delivery_days": days,
        "validation_status": "valid",
        "warning_messages": [],
        "error_messages": [],
        **extra,
    }


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class CombineRatesTest(unittest.TestCase):
    def test_single_box_sums_all_amounts_and_flags_cheapest_best_value(self):
        box = [rate(UPS, "ups_ground", 10.00, other=1.50, confirmation=0.0, days=3),
               rate(FEDEX, "fedex_ground", 9.00, other=3.00, days=2)]
        out = [r.to_ui() for r in ss._combine_rates([box], CATALOG, NAMES)]
        self.assertEqual(out, [
            {"provider": "shipstation", "courier_service_id": f"{UPS}:ups_ground",
             "courier_name": "UPS® Ground", "umbrella_name": "UPS", "total_charge": 11.5,
             "currency": "USD", "min_delivery_time": 3, "max_delivery_time": 3, "value_for_money_rank": 1},
            {"provider": "shipstation", "courier_service_id": f"{FEDEX}:fedex_ground",
             "courier_name": "FedEx Ground®", "umbrella_name": "FedEx", "total_charge": 12.0,
             "currency": "USD", "min_delivery_time": 2, "max_delivery_time": 2, "value_for_money_rank": None},
        ])

    def test_multi_box_keeps_only_common_services_and_sums_across_boxes(self):
        box1 = [rate(UPS, "ups_ground", 10.0, days=3), rate(UPS, "ups_nda", 40.0, days=1)]
        box2 = [rate(UPS, "ups_ground", 12.0, days=4), rate(FEDEX, "fedex_ground", 9.0)]
        out = [(r.provider_service_id, r.total_charge, r.max_delivery_time)
               for r in ss._combine_rates([box1, box2], CATALOG, NAMES)]
        self.assertEqual(out, [(f"{UPS}:ups_ground", 22.0, 4)])

    def test_invalid_and_errored_rates_are_dropped(self):
        box = [rate(UPS, "ups_ground", 10.0, validation_status="invalid"),
               rate(UPS, "ups_nda", 40.0, error_messages=["Package too heavy"]),
               rate(FEDEX, "fedex_ground", 9.0)]
        out = [r.provider_service_id for r in ss._combine_rates([box], CATALOG, NAMES)]
        self.assertEqual(out, [f"{FEDEX}:fedex_ground"])

    def test_cheapest_duplicate_service_wins_and_names_fall_back_to_service_type(self):
        box = [rate(UPS, "ups_ground", 12.0), rate(UPS, "ups_ground", 10.0)]
        out = [(r.courier_name, r.umbrella_name, r.total_charge) for r in ss._combine_rates([box])]
        self.assertEqual(out, [("UPS_GROUND", "UPS", 10.0)])

    def test_box_without_rates_yields_no_combined_rates(self):
        self.assertEqual(ss._combine_rates([[rate(UPS, "ups_ground", 10.0)], []]), [])


class ServiceIdTest(unittest.TestCase):
    def test_round_trip(self):
        sid = ss._service_id({"carrier_id": UPS, "service_code": "ups_ground"})
        self.assertEqual(ss._split_service_id(sid), (UPS, "ups_ground"))

    def test_malformed_id_raises(self):
        with self.assertRaises(ProviderError):
            ss._split_service_id("ups_ground")


class LabelStateTest(unittest.TestCase):
    def label(self, status, **extra):
        return {"label_id": "se-l-1", "status": status, "carrier_id": UPS, "carrier_code": "ups",
                "service_code": "ups_ground", "tracking_number": "1Z999",
                "shipment_cost": {"currency": "usd", "amount": 8.25},
                "insurance_cost": {"currency": "usd", "amount": 0.75}, **extra}

    def test_completed_label_is_ready_with_tracking_and_cost(self):
        state = ss._to_state(self.label("completed"), CATALOG, NAMES)
        self.assertEqual(
            (state.provider_shipment_id, state.label_status, state.tracking_numbers,
             state.courier_name, state.courier_umbrella_name, state.cost, state.error_message),
            ("se-l-1", LabelStatus.READY, ["1Z999"], "UPS® Ground", "UPS", 9.0, None))

    def test_status_mapping(self):
        mapping = {s: ss._label_status({"status": s}) for s in ("completed", "processing", "error", "voided", None)}
        self.assertEqual(mapping, {
            "completed": LabelStatus.READY, "processing": LabelStatus.PENDING,
            "error": LabelStatus.FAILED, "voided": LabelStatus.NOT_CREATED, None: LabelStatus.NOT_CREATED})

    def test_error_label_carries_a_reason(self):
        state = ss._to_state(self.label("error", tracking_number=None))
        self.assertEqual((state.label_status, state.tracking_numbers, state.error_message),
                         (LabelStatus.FAILED, [], "Label rejected by ShipStation"))

    def test_failed_state_from_purchase_rejection(self):
        state = ss._failed_state("se-s-1", ProviderError("ShipStation error (400): bad address", status=400))
        self.assertEqual((state.label_status, state.error_message, state.provider_shipment_id),
                         (LabelStatus.FAILED, "ShipStation error (400): bad address", None))


class ExtractErrorTest(unittest.TestCase):
    def test_joins_error_entries_with_field_and_code(self):
        resp = FakeResponse(400, {"request_id": "x", "errors": [
            {"error_source": "shipstation", "error_type": "validation", "error_code": "field_value_required",
             "message": "postal_code is required", "field_name": "ship_to.postal_code"},
            {"error_source": "carrier", "error_type": "business_rules", "error_code": "unspecified",
             "message": "Weight exceeds maximum"}]})
        self.assertEqual(ss._extract_error(resp),
                         "ShipStation error (400): field_value_required: ship_to.postal_code: postal_code is required"
                         " | Weight exceeds maximum")

    def test_non_json_body(self):
        self.assertEqual(ss._extract_error(FakeResponse(502, None, "<html>Bad gateway</html>")),
                         "ShipStation error (502): <html>Bad gateway</html>")


class BuildersTest(unittest.TestCase):
    def test_package_uses_pounds_and_inches_with_dimension_defaults(self):
        self.assertEqual(ss._build_package({"weight": "2.5", "length": "10", "width": "", "height": "abc"}), {
            "weight": {"value": 2.5, "unit": "pound"},
            "dimensions": {"unit": "inch", "length": 10.0, "width": 1.0, "height": 1.0},
        })

    def test_rate_total_sums_amount_objects(self):
        self.assertEqual(ss._rate_total(rate(UPS, "ups_ground", 10.0, other=1.25, confirmation=2.0,
                                             tax_amount={"currency": "usd", "amount": 0.5})), 13.75)

    def test_inline_shipment_whitelists_draft_fields(self):
        draft = {
            "shipment_id": "se-s-1", "shipment_status": "pending",
            "ship_to": {"name": "A", "address_line1": "1 Main", "city_locality": "Austin", "state_province": "TX",
                        "postal_code": "78701", "country_code": "US", "geolocation": [{"x": 1}], "email": ""},
            "ship_from": None,
            "confirmation": "signature",
            "packages": [{"package_id": "se-p-1", "weight": {"value": 2, "unit": "pound"},
                          "dimensions": {"unit": "inch", "length": 1, "width": 1, "height": 1},
                          "tracking_number": None, "label_messages": {"reference1": None}}],
        }
        origin = {"name": "Warehouse", "address_line1": "9 Dock", "city_locality": "Dallas",
                  "state_province": "TX", "postal_code": "75001", "country_code": "US"}
        self.assertEqual(ss._inline_shipment(draft, UPS, "ups_ground", "se-s-1", origin), {
            "carrier_id": UPS, "service_code": "ups_ground", "external_shipment_id": "se-s-1",
            "ship_to": {"name": "A", "address_line1": "1 Main", "city_locality": "Austin", "state_province": "TX",
                        "postal_code": "78701", "country_code": "US"},
            "ship_from": origin,
            "packages": [{"weight": {"value": 2, "unit": "pound"},
                          "dimensions": {"unit": "inch", "length": 1, "width": 1, "height": 1},
                          "label_messages": {"reference1": None}}],
            "confirmation": "signature",
        })

    def test_carrier_names_disambiguate_duplicate_accounts(self):
        carriers = [{"carrier_id": "se-1", "carrier_code": "ups", "friendly_name": "UPS", "nickname": "Main"},
                    {"carrier_id": "se-2", "carrier_code": "ups", "friendly_name": "UPS", "nickname": "Returns"},
                    {"carrier_id": "se-3", "carrier_code": "usps", "friendly_name": "USPS", "nickname": "USPS"}]
        self.assertEqual(ss._carrier_names(carriers), {"se-1": "UPS · Main", "se-2": "UPS · Returns", "se-3": "USPS"})


class DescriptorTest(unittest.TestCase):
    def test_test_mode_follows_setting(self):
        provider = ss.ShipStationProvider()
        original = ss.db.get_setting
        try:
            ss.db.get_setting = lambda key, default=None: {"shipstation_test_labels": "false"}.get(key, default)
            off = provider.is_test_mode()
            ss.db.get_setting = lambda key, default=None: {"shipstation_test_labels": "true"}.get(key, default)
            on = provider.is_test_mode()
        finally:
            ss.db.get_setting = original
        self.assertEqual((off, on), (False, True))

    def test_descriptor_exposes_secret_key_and_service_exclusions(self):
        d = ss.ShipStationProvider().descriptor()
        self.assertEqual(
            ([f["key"] for f in d["fields"] if f["type"] == "secret"], d["supports"], d["enabled_key"], d["modes"]),
            (["shipstation_api_key"], {"service_exclusions": True}, "shipstation_enabled", []))


if __name__ == "__main__":
    unittest.main()

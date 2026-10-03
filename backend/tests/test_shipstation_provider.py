import sys
import threading
import types
import unittest

# The provider reads settings through db; stub it so the pure helpers import
# without a live Postgres. Tests that need a setting patch `ss.db.get_setting`.
sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

from providers import shipstation as ss  # noqa: E402
from providers.base import LabelStatus, ProviderError  # noqa: E402

UPS, FEDEX, STAMPS = "se-100", "se-200", "se-300"
CATALOG = {(UPS, "ups_ground"): "UPS® Ground", (UPS, "ups_nda"): "UPS Next Day Air®",
           (FEDEX, "fedex_ground"): "FedEx Ground®"}
NAMES = {UPS: "UPS", FEDEX: "FedEx", STAMPS: "Stamps.com"}
CODES = {UPS: "ups", FEDEX: "fedex", STAMPS: "stamps_com"}

# UPS rates multi-package shipments; its SurePost and every USPS service only
# rate one package at a time.
MIXED_CARRIERS = [
    {"carrier_id": UPS, "carrier_code": "ups", "friendly_name": "UPS",
     "has_multi_package_supporting_services": True,
     "services": [{"service_code": "ups_ground", "name": "UPS® Ground", "is_multi_package_supported": True},
                  {"service_code": "ups_surepost", "name": "UPS SurePost", "is_multi_package_supported": False}]},
    {"carrier_id": STAMPS, "carrier_code": "stamps_com", "friendly_name": "Stamps.com",
     "has_multi_package_supporting_services": False,
     "services": [{"service_code": "usps_priority_mail", "name": "USPS Priority Mail",
                   "is_multi_package_supported": False}]},
]


def rate(carrier_id, service_code, shipping, other=0.0, confirmation=0.0, days=3, **extra):
    return {
        "rate_id": f"se-r-{carrier_id}-{service_code}-{shipping}",
        "carrier_id": carrier_id,
        "carrier_code": CODES[carrier_id],
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
    def test_sums_all_amounts_and_flags_cheapest_best_value(self):
        rates = [rate(UPS, "ups_ground", 10.00, other=1.50, confirmation=0.0, days=3),
                 rate(FEDEX, "fedex_ground", 9.00, other=3.00, days=2)]
        out = [r.to_ui() for r in ss._combine_rates(rates, CATALOG, NAMES)]
        self.assertEqual(out, [
            {"provider": "shipstation", "courier_service_id": f"{UPS}:ups_ground",
             "courier_name": "UPS® Ground", "umbrella_name": "UPS", "total_charge": 11.5,
             "currency": "USD", "min_delivery_time": 3, "max_delivery_time": 3, "value_for_money_rank": 1},
            {"provider": "shipstation", "courier_service_id": f"{FEDEX}:fedex_ground",
             "courier_name": "FedEx Ground®", "umbrella_name": "FedEx", "total_charge": 12.0,
             "currency": "USD", "min_delivery_time": 2, "max_delivery_time": 2, "value_for_money_rank": None},
        ])

    def test_invalid_and_errored_rates_are_dropped(self):
        rates = [rate(UPS, "ups_ground", 10.0, validation_status="invalid"),
                 rate(UPS, "ups_nda", 40.0, error_messages=["Package too heavy"]),
                 rate(FEDEX, "fedex_ground", 9.0)]
        out = [r.provider_service_id for r in ss._combine_rates(rates, CATALOG, NAMES)]
        self.assertEqual(out, [f"{FEDEX}:fedex_ground"])

    def test_cheapest_duplicate_service_wins_and_names_fall_back_to_service_type(self):
        rates = [rate(UPS, "ups_ground", 12.0), rate(UPS, "ups_ground", 10.0)]
        out = [(r.courier_name, r.umbrella_name, r.total_charge) for r in ss._combine_rates(rates)]
        self.assertEqual(out, [("UPS_GROUND", "UPS", 10.0)])

    def test_no_rates_yields_no_quotes(self):
        self.assertEqual(ss._combine_rates([]), [])

    def test_per_box_quotes_keep_only_services_every_box_got_and_sum_them(self):
        box1 = [rate(STAMPS, "usps_priority_mail", 7.0, days=2), rate(UPS, "ups_surepost", 6.0, days=5)]
        box2 = [rate(STAMPS, "usps_priority_mail", 9.0, other=0.5, days=3)]
        out = [(r.provider_service_id, r.total_charge, r.min_delivery_time, r.value_for_money_rank)
               for r in ss._combine_per_box([box1, box2], CATALOG, NAMES)]
        self.assertEqual(out, [(f"{STAMPS}:usps_priority_mail", 16.5, 3, 1)])


class MultiPackageSupportTest(unittest.TestCase):
    def test_service_flag_wins_then_carrier_flag_then_assumed_supported(self):
        carriers = MIXED_CARRIERS + [{"carrier_id": FEDEX, "carrier_code": "fedex",
                                      "services": [{"service_code": "fedex_ground"}]}]
        self.assertEqual(
            [ss._supports_multi_package(carriers, cid, code) for cid, code in
             ((UPS, "ups_ground"), (UPS, "ups_surepost"), (STAMPS, "usps_priority_mail"),
              (STAMPS, "usps_unlisted"), (FEDEX, "fedex_ground"), ("se-999", "x"))],
            [True, False, False, False, True, True])


class MultiBoxRatingTest(unittest.TestCase):
    """Two or more boxes: multi-package-capable services are rated as one
    shipment, the rest one box at a time, and the quotes are merged."""

    def setUp(self):
        self._orig = (ss._request, ss._carriers, ss._origin_address, ss.db.get_setting)
        self.calls = []
        ss._carriers = lambda auth, force=False: MIXED_CARRIERS
        ss._origin_address = lambda *a, **k: {"name": "W"}
        ss.db.get_setting = lambda key, default=None: "key" if key.endswith("_api_key") else default

    def tearDown(self):
        ss._request, ss._carriers, ss._origin_address, ss.db.get_setting = self._orig

    def fake_rates(self, responses):
        """responses: {carrier_ids tuple: response} for the multi-package call,
        or {carrier_ids tuple: {box weight: response}} for the per-box calls,
        which run in parallel and so cannot be answered in call order."""
        lock = threading.Lock()

        def fake_request(method, path, json_body=None, params=None, timeout=45, auth=None):
            ids = tuple(json_body["rate_options"]["carrier_ids"])
            packages = json_body["shipment"]["packages"]
            with lock:
                self.calls.append((ids, len(packages)))
            resp = responses[ids]
            return resp if "shipment_id" in resp else resp[packages[0]["weight"]["value"]]
        ss._request = fake_request

    def rate_boxes(self):
        return ss.ShipStationProvider().create_draft_shipments(
            {"address1": "PO Box 1"}, [{"weight": 1}, {"weight": 2}], [])

    def test_mixed_carriers_rate_both_ways_and_share_the_multi_package_draft(self):
        self.fake_rates({
            (UPS,): {"shipment_id": "se-M", "rate_response": {"rates": [
                rate(UPS, "ups_ground", 20.0), rate(UPS, "ups_surepost", 1.0)]}},
            (UPS, STAMPS): {
                1.0: {"shipment_id": "se-B1", "rate_response": {"rates": [
                    rate(STAMPS, "usps_priority_mail", 7.0), rate(UPS, "ups_surepost", 6.0),
                    rate(UPS, "ups_ground", 11.0)]}},
                2.0: {"shipment_id": "se-B2", "rate_response": {"rates": [
                    rate(STAMPS, "usps_priority_mail", 8.0), rate(UPS, "ups_surepost", 6.5)]}},
            },
        })
        drafts, rates, warnings = self.rate_boxes()
        self.assertEqual(sorted(self.calls), [((UPS,), 2), ((UPS, STAMPS), 1), ((UPS, STAMPS), 1)])
        self.assertEqual(
            ([d.provider_shipment_id for d in drafts],
             [(r.provider_service_id, r.total_charge, r.value_for_money_rank) for r in rates], warnings),
            (["se-M#1", "se-M#2"],
             [(f"{UPS}:ups_surepost", 12.5, 1), (f"{STAMPS}:usps_priority_mail", 15.0, None),
              (f"{UPS}:ups_ground", 20.0, None)],
             []))

    def test_only_single_package_carriers_draft_one_shipment_per_box(self):
        ss._carriers = lambda auth, force=False: MIXED_CARRIERS[1:]
        self.fake_rates({(STAMPS,): {
            1.0: {"shipment_id": "se-B1", "rate_response": {"rates": [rate(STAMPS, "usps_priority_mail", 7.0)]}},
            2.0: {"shipment_id": "se-B2", "rate_response": {"rates": [rate(STAMPS, "usps_priority_mail", 8.0)]}},
        }})
        drafts, rates, _ = self.rate_boxes()
        self.assertEqual(([d.provider_shipment_id for d in drafts], [r.total_charge for r in rates]),
                         (["se-B1", "se-B2"], [15.0]))

    def test_a_carrier_refusal_on_the_multi_package_call_does_not_hide_per_box_quotes(self):
        self.fake_rates({
            (UPS,): {"shipment_id": "se-M", "rate_response": {"rates": [], "errors": [
                {"message": "Address appears to be a PO Box. UPS does not deliver to PO Boxes."}]}},
            (UPS, STAMPS): {
                1.0: {"shipment_id": "se-B1", "rate_response": {"rates": [rate(STAMPS, "usps_priority_mail", 7.0)]}},
                2.0: {"shipment_id": "se-B2", "rate_response": {"rates": [rate(STAMPS, "usps_priority_mail", 8.0)]}},
            },
        })
        drafts, rates, _ = self.rate_boxes()
        self.assertEqual(([d.provider_shipment_id for d in drafts], [r.provider_service_id for r in rates]),
                         (["se-M#1", "se-M#2"], [f"{STAMPS}:usps_priority_mail"]))

    def test_no_quotes_anywhere_raises_with_every_carrier_reason(self):
        self.fake_rates({
            (UPS,): {"shipment_id": "se-M", "rate_response": {"rates": [], "errors": [
                {"message": "UPS does not deliver to PO Boxes."}]}},
            (UPS, STAMPS): {
                1.0: {"shipment_id": "se-B1", "rate_response": {"rates": [], "errors": [{"message": "Too heavy"}]}},
                2.0: {"shipment_id": "se-B2", "rate_response": {"rates": []}},
            },
        })
        with self.assertRaises(ProviderError) as ctx:
            self.rate_boxes()
        self.assertEqual(str(ctx.exception),
                         "ShipStation rating failed: UPS does not deliver to PO Boxes. | Too heavy")


class BoxIdTest(unittest.TestCase):
    def test_single_box_keeps_the_plain_shipment_id(self):
        self.assertEqual(ss._box_id("se-1", 0, 1), "se-1")
        self.assertEqual(ss._split_box_id("se-1"), ("se-1", None))

    def test_multi_box_ids_round_trip(self):
        ids = [ss._box_id("se-1", i, 3) for i in range(3)]
        self.assertEqual(ids, ["se-1#1", "se-1#2", "se-1#3"])
        self.assertEqual([ss._split_box_id(b) for b in ids],
                         [("se-1", 0), ("se-1", 1), ("se-1", 2)])


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

    def test_multi_package_label_gives_each_box_its_own_tracking_and_split_cost(self):
        label = self.label("completed", tracking_number="1Z-MASTER", packages=[
            {"package_id": "p1", "tracking_number": "1Z-BOX1",
             "label_download": {"pdf": "https://l/1.pdf"}},
            {"package_id": "p2", "tracking_number": "1Z-BOX2",
             "label_download": {"pdf": "https://l/2.pdf"}},
        ])
        s1 = ss._to_state(label, CATALOG, NAMES, box_id="se-s-1#1")
        s2 = ss._to_state(label, CATALOG, NAMES, box_id="se-s-1#2")
        self.assertEqual(
            (s1.tracking_numbers, s1.cost, s2.tracking_numbers, s2.cost,
             s1.provider_shipment_id, s2.provider_shipment_id),
            (["1Z-BOX1"], 4.5, ["1Z-BOX2"], 4.5, "se-l-1", "se-l-1"))
        self.assertEqual(
            (ss._download_url(s1.raw, "pdf"), ss._download_url(s2.raw, "pdf")),
            ("https://l/1.pdf", "https://l/2.pdf"))

    def test_single_package_label_uses_the_whole_label_download(self):
        label = self.label("completed", label_download={"pdf": "https://l/all.pdf"},
                           packages=[{"tracking_number": "1Z999"}])
        state = ss._to_state(label, CATALOG, NAMES, box_id="se-s-1")
        self.assertEqual((state.tracking_numbers, state.cost, ss._download_url(state.raw, "pdf")),
                         (["1Z999"], 9.0, "https://l/all.pdf"))

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


class GroupedBuyTest(unittest.TestCase):
    """A multi-box order is ONE shipment: buying its boxes issues one purchase."""

    def setUp(self):
        self._orig = (ss._request, ss._carriers, ss._origin_address)
        self.calls = []

        def fake_request(method, path, json_body=None, params=None, timeout=45, auth=None):
            self.calls.append((method, path))
            if path == "/v2/labels" and method == "GET":
                return {"labels": []}  # idempotency guard: nothing bought yet
            if path.startswith("/v2/shipments/") and method == "GET":
                return {"shipment_id": "se-s-9",
                        "ship_to": {"name": "A", "address_line1": "1 Main", "city_locality": "X",
                                    "state_province": "TX", "postal_code": "1", "country_code": "US"},
                        "ship_from": None,
                        "packages": [{"weight": {"value": 1, "unit": "pound"}},
                                     {"weight": {"value": 2, "unit": "pound"}}]}
            if path == "/v2/labels" and method == "POST":
                self.post_body = json_body
                return {"label_id": "se-l-9", "status": "completed", "carrier_id": UPS,
                        "carrier_code": "ups", "service_code": "ups_ground",
                        "shipment_cost": {"currency": "usd", "amount": 10.0},
                        "packages": [{"tracking_number": "1Z-A"}, {"tracking_number": "1Z-B"}]}
            raise AssertionError(f"unexpected {method} {path}")

        ss._request = fake_request
        ss._carriers = lambda auth, force=False: []
        ss._origin_address = lambda *a, **k: {"name": "W", "address_line1": "9 Dock", "city_locality": "D",
                                              "state_province": "TX", "postal_code": "2", "country_code": "US"}
        self.settings_read = []

        def get_setting(key, default=None):
            self.settings_read.append(key)
            return "key" if key.endswith("_api_key") else default

        ss.db.get_setting = get_setting

    def tearDown(self):
        ss._request, ss._carriers, ss._origin_address = self._orig
        ss.db.get_setting = lambda *a, **k: None

    def test_two_boxes_one_purchase_with_per_box_tracking(self):
        out = ss.ShipStationProvider().buy_labels(["se-s-9#1", "se-s-9#2"], f"{UPS}:ups_ground")
        posts = [c for c in self.calls if c == ("POST", "/v2/labels")]
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(self.post_body["shipment"]["packages"]), 2)
        self.assertEqual(self.post_body["shipment"]["external_shipment_id"], "se-s-9")
        self.assertEqual(
            {bid: (st.tracking_numbers, st.cost, st.label_status) for bid, st in out.items()},
            {"se-s-9#1": (["1Z-A"], 5.0, LabelStatus.READY),
             "se-s-9#2": (["1Z-B"], 5.0, LabelStatus.READY)})

    def test_single_package_service_buys_each_box_from_its_slice_of_the_shared_draft(self):
        ss._carriers = lambda auth, force=False: MIXED_CARRIERS
        bodies = []
        orig = ss._request

        def fake(method, path, json_body=None, params=None, timeout=45, auth=None):
            if (method, path) == ("POST", "/v2/labels"):
                bodies.append(json_body["shipment"])
                return {"label_id": f"se-l-{len(bodies)}", "status": "completed", "carrier_id": STAMPS,
                        "carrier_code": "stamps_com", "service_code": "usps_priority_mail",
                        "tracking_number": f"94{len(bodies)}",
                        "shipment_cost": {"currency": "usd", "amount": 7.0},
                        "packages": [{"tracking_number": f"94{len(bodies)}"}]}
            return orig(method, path, json_body, params, timeout, auth)

        ss._request = fake
        out = ss.ShipStationProvider().buy_labels(["se-s-9#1", "se-s-9#2"], f"{STAMPS}:usps_priority_mail")
        self.assertEqual(
            sorted((b["external_shipment_id"], b["packages"]) for b in bodies),
            [("se-s-9#1", [{"weight": {"value": 1, "unit": "pound"}}]),
             ("se-s-9#2", [{"weight": {"value": 2, "unit": "pound"}}])])
        self.assertEqual(
            {bid: (len(st.tracking_numbers), st.cost, st.label_status) for bid, st in out.items()},
            {"se-s-9#1": (1, 7.0, LabelStatus.READY), "se-s-9#2": (1, 7.0, LabelStatus.READY)})
        self.assertNotEqual(out["se-s-9#1"].provider_shipment_id, out["se-s-9#2"].provider_shipment_id)

    def test_poll_finds_per_box_labels_bought_from_a_shared_draft(self):
        orig = ss._request

        def fake(method, path, json_body=None, params=None, timeout=45, auth=None):
            if (method, path) == ("GET", "/v2/labels") and "#" in params["external_shipment_id"]:
                return {"labels": [{"label_id": f"se-l-{params['external_shipment_id'][-1]}",
                                    "status": "completed", "carrier_id": UPS, "carrier_code": "ups",
                                    "service_code": "ups_surepost", "tracking_number": "1Z"}]}
            return orig(method, path, json_body, params, timeout, auth)

        ss._request = fake
        out = ss.ShipStationProvider().poll_shipments(["se-s-9#1", "se-s-9#2"])
        self.assertEqual({bid: (st.provider_shipment_id, st.label_status) for bid, st in out.items()},
                         {"se-s-9#1": ("se-l-1", LabelStatus.READY), "se-s-9#2": ("se-l-2", LabelStatus.READY)})

    def test_second_instance_reads_its_own_api_key(self):
        ss.ShipStationProvider("shipstation-5", "East").buy_labels(["se-s-9"], f"{UPS}:ups_ground")
        self.assertIn("shipstation-5_api_key", self.settings_read)
        self.assertNotIn("shipstation_api_key", self.settings_read)

    def test_cancel_voids_a_shared_label_once(self):
        voids = []
        orig = ss._request

        def fake(method, path, json_body=None, params=None, timeout=45, auth=None):
            voids.append((method, path))
            return {"approved": True}

        ss._request = fake
        try:
            errors = ss.ShipStationProvider().cancel_all(["se-l-9#1", "se-l-9#2", "se-l-9"])
        finally:
            ss._request = orig
        self.assertEqual((errors, voids), ([], [("PUT", "/v2/labels/se-l-9/void")]))


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

    def test_second_instance_descriptor_is_namespaced_by_its_key(self):
        d = ss.ShipStationProvider("shipstation-5", "East").descriptor()
        keys = [d["enabled_key"], d["origin_override_key"]] + [f["key"] for f in d["fields"]] \
            + [f["key"] for f in d["origin_fields"]]
        self.assertEqual(
            (d["name"], d["label"], d["platform"], d["platform_label"],
             all(k.startswith("shipstation-5_") for k in keys),
             d["services_endpoint"]),
            ("shipstation-5", "East", "shipstation", "ShipStation", True,
             "/api/providers/shipstation-5/services"))

    def test_default_hooks_pass_carriers_through(self):
        provider = ss.ShipStationProvider()
        carriers = [{"carrier_id": UPS, "carrier_code": "ups", "friendly_name": "UPS"},
                    {"carrier_id": FEDEX, "carrier_code": "fedex", "friendly_name": "FedEx"}]
        self.assertEqual((provider.rating_carriers(carriers), provider.carrier_names(carriers)),
                         (carriers, {UPS: "UPS", FEDEX: "FedEx"}))

    def test_list_carriers_uses_disambiguated_names(self):
        orig = (ss._carriers, ss.db.get_setting)
        ss._carriers = lambda auth, force=False: [
            {"carrier_id": "se-1", "carrier_code": "ups", "friendly_name": "UPS", "nickname": "Main"},
            {"carrier_id": "se-2", "carrier_code": "ups", "friendly_name": "UPS", "nickname": "Returns"},
            {"carrier_id": "se-3", "carrier_code": "stamps_com", "friendly_name": "Stamps.com", "nickname": "Endicia"}]
        ss.db.get_setting = lambda key, default=None: "key" if key.endswith("_api_key") else default
        try:
            out = ss.ShipStationProvider().list_carriers()
        finally:
            ss._carriers, ss.db.get_setting = orig
        self.assertEqual(out, [{"value": "se-1", "label": "UPS · Main"},
                               {"value": "se-2", "label": "UPS · Returns"},
                               {"value": "se-3", "label": "Stamps.com"}])

    def test_connection_summary_lists_carriers_and_test_mode(self):
        orig = ss.db.get_setting
        ss.db.get_setting = lambda key, default=None: {"shipstation_test_labels": "true"}.get(key, default)
        try:
            summary = ss.ShipStationProvider()._connection_summary(
                [{"friendly_name": "UPS"}, {"carrier_code": "stamps_com"}])
        finally:
            ss.db.get_setting = orig
        self.assertEqual(summary, "2 carrier(s): UPS, stamps_com — test labels ON (no charge)")

    def test_rates_are_tagged_with_the_instance_key(self):
        orig = (ss._request, ss._carriers, ss._origin_address, ss.db.get_setting)
        ss._request = lambda *a, **k: {"shipment_id": "se-s-1",
                                       "rate_response": {"rates": [rate(UPS, "ups_ground", 10.0)]}}
        ss._carriers = lambda auth, force=False: [{"carrier_id": UPS, "carrier_code": "ups",
                                                    "friendly_name": "UPS", "services": []}]
        ss._origin_address = lambda *a, **k: {}
        ss.db.get_setting = lambda key, default=None: "key" if key.endswith("_api_key") else default
        try:
            _, rates, _ = ss.ShipStationProvider("shipstation-5", "East").create_draft_shipments(
                {"address1": "1 Main"}, [{"weight": 1}], [])
        finally:
            ss._request, ss._carriers, ss._origin_address, ss.db.get_setting = orig
        self.assertEqual([r.provider for r in rates], ["shipstation-5"])


if __name__ == "__main__":
    unittest.main()

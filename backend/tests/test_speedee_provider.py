import base64
import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# The provider reads settings through db; stub it so the module imports
# without a live Postgres. Each test swaps in a FakeDb on the module.
sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

from providers import base as provider_base  # noqa: E402
from providers import speedee as sd  # noqa: E402
from providers.base import LabelStatus, ProviderError  # noqa: E402

KEY = "speedee"
ORIGIN = {
    "origin_company": "Acme", "origin_contact": "Bob", "origin_address1": "1 Main St",
    "origin_address2": "", "origin_city": "Saint Cloud", "origin_state": "MN",
    "origin_zip": "56301", "origin_phone": "3205550100", "origin_email": "ship@acme.test",
}
CREDENTIALS = {f"{KEY}_mode": "sandbox", f"{KEY}_sandbox_username": "user", f"{KEY}_sandbox_password": "pw"}
DEST = {"contact": "Jane Roe", "company": "", "address1": "123 2nd Ave S", "address2": "",
        "city": "Minneapolis", "state": "mn", "zip": "55401", "phone": "6125550100", "email": "j@x.test"}
BOX = {"weight": "10", "length": "12", "width": "10", "height": "8"}
ORDER_ID = "SD3041"
COD_AMOUNT = 125.4


class FakeDb:
    """Settings plus the speedee_drafts table, interpreting just the
    statements the provider issues."""

    def __init__(self, settings=None):
        self.settings = {**{f"origin_{k[7:]}": v for k, v in ORIGIN.items()}, **CREDENTIALS, **(settings or {})}
        self.drafts = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def query(self, sql, params=None, one=False):
        if "FROM speedee_drafts" in sql:
            row = self.drafts.get(params[0])
            row = dict(row) if row and row["provider"] == params[1] else None
            return row if one else ([row] if row else [])
        return None if one else []

    def execute(self, sql, params=None, returning=False):
        if sql.startswith("INSERT INTO speedee_drafts"):
            draft_id, provider, body, quote = params
            self.drafts[draft_id] = {"draft_id": draft_id, "provider": provider, "body": json.loads(body),
                                     "quote": json.loads(quote), "order_id": None, "create_started_at": None}
        elif "SET create_started_at = now()" in sql:
            self.drafts[params[0]]["create_started_at"] = datetime.now(timezone.utc)
        elif "SET create_started_at = NULL" in sql:
            self.drafts[params[0]]["create_started_at"] = None
        elif "SET order_id" in sql:
            self.drafts[params[1]]["order_id"] = params[0]
        elif sql.startswith("DELETE FROM speedee_drafts WHERE draft_id"):
            self.drafts.pop(params[0], None)


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload).encode() if payload is not None else text.encode()
        self.text = text
        self.ok = status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def transport(routes):
    """Fake requests.request keyed by (METHOD, path); list values are consumed
    in order so a poll can see the state change. An Exception value is raised."""
    calls = []

    def fake(method, url, **kwargs):
        path = url.split("/integration", 1)[1]
        calls.append((method.upper(), path, kwargs.get("json")))
        entry = routes[(method.upper(), path)]
        if isinstance(entry, list):
            entry = entry.pop(0) if len(entry) > 1 else entry[0]
        if isinstance(entry, Exception):
            raise entry
        return entry

    fake.calls = calls
    return fake


def quote(total_item_price=14.14, das=0.0, errors=None):
    return {
        "validationErrors": errors,
        "pickupWindow": {"start": "2026-11-02T08:00:00-06:00", "end": "2026-11-02T17:00:00-06:00"},
        "dropoffWindow": {"start": "2026-11-02T08:00:00-06:00", "end": "2026-11-03T17:00:00-06:00"},
        "charges": [
            {"type": "Delivery", "price": 0},
            {"type": "Extra", "price": total_item_price, "quantity": 2, "extraFeeTypeId": "ITEMPRICE"},
            {"type": "Extra", "price": das, "quantity": 0, "extraFeeTypeId": "DAS"},
        ],
    }


def items(*barcodes):
    return [{"itemId": f"item-{n}", "itemNumber": n, "barcode": b}
            for n, b in enumerate(barcodes, start=1)]


class SpeedeeCase(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        self.patches = [patch.object(sd, "db", self.db), patch.object(provider_base, "db", self.db),
                        patch.object(sd.time, "sleep", lambda s: None)]
        for p in self.patches:
            p.start()
        self.provider = sd.SpeedeeProvider()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def http(self, routes):
        fake = transport(routes)
        p = patch.object(sd.requests, "request", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def draft(self, boxes=2, **row):
        """A saved draft with its per-box ids."""
        body = sd._order_body(ORIGIN, DEST, [BOX] * boxes, {}, "tok123")
        draft_id = "spd_abc"
        self.db.drafts[draft_id] = {"draft_id": draft_id, "provider": KEY, "body": body,
                                    "quote": {"total": 30.0, "boxes": boxes}, "order_id": None,
                                    "create_started_at": None, **row}
        return [sd._box_id(draft_id, i, boxes) for i in range(boxes)]


class OrderBodyTests(unittest.TestCase):
    def test_body_carries_addresses_items_and_references(self):
        small = {"weight": "3", "length": "6", "width": "4", "height": "2.5"}
        body = sd._order_body(ORIGIN, DEST, [BOX, small], {"reference": "#1001"}, "tok")
        self.assertEqual(body["dropoffAddress"], {
            "addressLine1": "123 2nd Ave S", "addressLine2": None, "company": "Jane Roe",
            "city": "Minneapolis", "stateProvince": "MN", "postalCode": "55401", "country": "US"})
        self.assertEqual(body["pickupContactInfo"],
                         {"name": "Bob", "phoneNumber": "3205550100", "email": "ship@acme.test"})
        self.assertEqual(body["items"], [
            {"parcelTypeId": "BOX", "weight": 10.0, "length": 12.0, "width": 10.0, "height": 8.0,
             "userFields": []},
            {"parcelTypeId": "BOX", "weight": 3.0, "length": 6.0, "width": 4.0, "height": 2.5,
             "userFields": []},
        ])
        self.assertEqual((body["serviceLevelId"], body["vehicleTypeId"], body["referenceNumber1"],
                          body["referenceNumber3"]), ("PACKAGE_PARCEL", "STEPVAN", "#1001", "tok"))

    def test_boxes_without_dimensions_or_weight_are_refused_by_number(self):
        boxes = [BOX, {"weight": "3"}, {**BOX, "weight": "0"}, {**BOX, "height": "0"}]
        with self.assertRaises(ProviderError) as ctx:
            sd._order_body(ORIGIN, DEST, boxes, {}, "tok")
        self.assertEqual(str(ctx.exception),
                         "Spee-Dee needs the weight and L × W × H of every box — missing on box 2, 3, 4")

    def test_reference_number_2_is_never_sent(self):
        body = sd._order_body(ORIGIN, DEST, [BOX], {"reference": "R"}, "tok")
        self.assertNotIn("referenceNumber2", body)

    def test_signature_options_map_to_extra_fees(self):
        cases = {"none": None, "signature": [{"extraFeeId": "AOD", "quantity": 1}],
                 "adult": [{"extraFeeId": "AODSIGNATURE", "quantity": 1}]}
        for option, expected in cases.items():
            with self.subTest(option=option):
                body = sd._order_body(ORIGIN, DEST, [BOX], {"signature": option}, "tok")
                self.assertEqual(body.get("extraFees"), expected)

    def test_cod_adds_its_fee_and_the_amount_to_collect(self):
        body = sd._order_body(ORIGIN, DEST, [BOX], {"cod": {"amount": COD_AMOUNT, "allow_partial": True}}, "tok")
        self.assertEqual((body["extraFees"], body["collectOnDelivery"], body["allowPartialCollectOnDelivery"]),
                         ([{"extraFeeId": "COD", "quantity": 1}], COD_AMOUNT, True))

    def test_cod_with_an_adult_signature_carries_both_fees(self):
        body = sd._order_body(ORIGIN, DEST, [BOX], {"signature": "adult", "cod": {"amount": COD_AMOUNT}}, "tok")
        self.assertEqual((body["extraFees"], body["allowPartialCollectOnDelivery"]), (
            [{"extraFeeId": "AODSIGNATURE", "quantity": 1}, {"extraFeeId": "COD", "quantity": 1}], False))

    def test_without_cod_no_collection_is_sent(self):
        body = sd._order_body(ORIGIN, DEST, [BOX], {"signature": "none", "cod": None}, "tok")
        self.assertEqual({"extraFees", "collectOnDelivery", "allowPartialCollectOnDelivery"} & body.keys(), set())

    def test_incomplete_origin_names_missing_fields(self):
        with self.assertRaisesRegex(ProviderError, "Phone"):
            sd._order_body({**ORIGIN, "origin_phone": ""}, DEST, [BOX], {}, "tok")


class BoxIdTests(unittest.TestCase):
    def test_box_id_round_trips(self):
        for count in (1, 2, 5):
            for index in range(count):
                base, idx = sd._split_box_id(sd._box_id("SD9", index, count))
                self.assertEqual((base, idx), ("SD9", None if count == 1 else index))

    def test_temporary_barcode_is_the_item_number(self):
        cases = [("01", 1, True), ("2", 2, True), ("", 1, True), ("SP55555503SD2862001", 1, False),
                 ("03", 1, False)]
        for barcode, number, expected in cases:
            with self.subTest(barcode=barcode):
                self.assertEqual(sd._is_temporary_barcode({"barcode": barcode, "itemNumber": number}), expected)


class ErrorTests(unittest.TestCase):
    def test_model_state_errors_are_flattened(self):
        resp = FakeResponse(400, {"DropoffAddress.PostalCode": {"errors": [{"errorMessage": "Out of area"}]},
                                  "error": ["Bad request"]})
        self.assertEqual(sd._extract_error(resp),
                         "Spee-Dee error (400): DropoffAddress.PostalCode: Out of area; Bad request")

    def test_plain_text_error(self):
        self.assertEqual(sd._extract_error(FakeResponse(400, text="Order cannot be cancelled")),
                         "Spee-Dee error (400): Order cannot be cancelled")


class RatingTests(SpeedeeCase):
    def test_quote_becomes_one_rate_and_per_box_drafts(self):
        self.http({("POST", "/api/v1/orders/quote"): FakeResponse(200, quote(14.14, das=3.5))})
        drafts, rates, warnings = self.provider.create_draft_shipments(DEST, [BOX, BOX], [], {"reference": "#7"})
        base = drafts[0].provider_shipment_id.partition("#")[0]
        self.assertEqual([d.provider_shipment_id for d in drafts], [f"{base}#1", f"{base}#2"])
        self.assertEqual([r.to_ui() for r in rates], [{
            "provider": KEY, "courier_service_id": "PACKAGE_PARCEL", "courier_name": "Spee-Dee Parcel",
            "umbrella_name": "Spee-Dee", "total_charge": 17.64, "currency": "USD",
            "min_delivery_time": 1, "max_delivery_time": 1, "value_for_money_rank": 1}])
        self.assertEqual(warnings, [])
        self.assertEqual(self.db.drafts[base]["quote"], {"total": 17.64, "boxes": 2})

    def test_validation_errors_refuse_the_quote(self):
        self.http({("POST", "/api/v1/orders/quote"): FakeResponse(200, quote(errors=["ZIP not serviced"]))})
        with self.assertRaisesRegex(ProviderError, "ZIP not serviced"):
            self.provider.create_draft_shipments(DEST, [BOX], [], {})
        self.assertEqual(self.db.drafts, {})

    def test_missing_credentials(self):
        self.db.settings.pop(f"{KEY}_sandbox_password")
        with self.assertRaisesRegex(ProviderError, "sandbox username/password"):
            self.provider.create_draft_shipments(DEST, [BOX], [], {})


class BuyTests(SpeedeeCase):
    def test_one_order_for_every_box_ready_once_barcodes_are_real(self):
        ids = self.draft(boxes=2)
        fake = self.http({
            ("POST", "/api/v1/orders"): FakeResponse(200, {"orderId": ORDER_ID}),
            ("GET", f"/api/v1/orders/{ORDER_ID}/items"): FakeResponse(200, items("SPX1", "SPX2")),
        })
        result = self.provider.buy_labels(ids, "PACKAGE_PARCEL")
        self.assertEqual([c[:2] for c in fake.calls].count(("POST", "/api/v1/orders")), 1)
        self.assertEqual({bid: (s.label_status, s.provider_shipment_id, s.tracking_numbers, s.cost)
                          for bid, s in result.items()}, {
            ids[0]: (LabelStatus.READY, f"{ORDER_ID}#1", ["SPX1"], 15.0),
            ids[1]: (LabelStatus.READY, f"{ORDER_ID}#2", ["SPX2"], 15.0),
        })
        self.assertEqual(self.db.drafts["spd_abc"]["order_id"], ORDER_ID)

    def test_existing_order_is_never_created_again(self):
        ids = self.draft(boxes=1, order_id=ORDER_ID)
        fake = self.http({("GET", f"/api/v1/orders/{ORDER_ID}/items"): FakeResponse(200, items("SPX1"))})
        result = self.provider.buy_labels(ids, "PACKAGE_PARCEL")
        self.assertEqual([c[:2] for c in fake.calls], [("GET", f"/api/v1/orders/{ORDER_ID}/items")])
        self.assertEqual(result[ids[0]].provider_shipment_id, ORDER_ID)

    def test_lost_create_response_is_found_by_search_not_recreated(self):
        ids = self.draft(boxes=1, create_started_at=datetime.now(timezone.utc) - timedelta(seconds=10))
        fake = self.http({
            ("POST", "/api/v1/orders/search"): FakeResponse(200, {"items": [
                {"orderId": "SD1", "status": 91, "referenceNumber3": "tok123"},
                {"orderId": ORDER_ID, "status": 0, "referenceNumber3": "tok123"}]}),
            ("GET", f"/api/v1/orders/{ORDER_ID}/items"): FakeResponse(200, items("01")),
            ("GET", f"/api/v1/orders/{ORDER_ID}/status"): FakeResponse(200, {"status": "Received"}),
        })
        result = self.provider.buy_labels(ids, "PACKAGE_PARCEL")
        self.assertNotIn(("POST", "/api/v1/orders"), [c[:2] for c in fake.calls])
        self.assertEqual(fake.calls[0][2], {"query": "tok123", "searchFields": ["ReferenceNumber3"],
                                            "operator": 2, "maximumResults": 5})
        self.assertEqual(result[ids[0]].label_status, LabelStatus.PENDING)
        self.assertEqual(self.db.drafts["spd_abc"]["order_id"], ORDER_ID)

    def test_recent_lost_create_with_no_match_waits(self):
        ids = self.draft(boxes=1, create_started_at=datetime.now(timezone.utc) - timedelta(seconds=10))
        fake = self.http({("POST", "/api/v1/orders/search"): FakeResponse(200, {"items": []})})
        result = self.provider.buy_labels(ids, "PACKAGE_PARCEL")
        self.assertEqual([c[:2] for c in fake.calls], [("POST", "/api/v1/orders/search")])
        self.assertEqual(result[ids[0]].label_status, LabelStatus.PENDING)

    def test_old_lost_create_with_no_match_creates(self):
        started = datetime.now(timezone.utc) - timedelta(seconds=sd.LOST_CREATE_GRACE_S + 5)
        ids = self.draft(boxes=1, create_started_at=started)
        fake = self.http({
            ("POST", "/api/v1/orders/search"): FakeResponse(200, {"items": []}),
            ("POST", "/api/v1/orders"): FakeResponse(200, {"orderId": ORDER_ID}),
            ("GET", f"/api/v1/orders/{ORDER_ID}/items"): FakeResponse(200, items("SPX1")),
        })
        result = self.provider.buy_labels(ids, "PACKAGE_PARCEL")
        self.assertEqual([c[:2] for c in fake.calls].count(("POST", "/api/v1/orders")), 1)
        self.assertEqual(result[ids[0]].label_status, LabelStatus.READY)

    def test_failed_search_never_creates(self):
        ids = self.draft(boxes=1, create_started_at=datetime.now(timezone.utc) - timedelta(hours=1))
        fake = self.http({("POST", "/api/v1/orders/search"): FakeResponse(400, {"error": ["bad field"]})})
        result = self.provider.buy_labels(ids, "PACKAGE_PARCEL")
        self.assertIsInstance(result[ids[0]], ProviderError)
        self.assertNotIn(("POST", "/api/v1/orders"), [c[:2] for c in fake.calls])

    def test_create_timeout_is_recoverable_and_marks_the_attempt(self):
        ids = self.draft(boxes=1)
        self.http({("POST", "/api/v1/orders"): sd.requests.Timeout("read timed out")})
        result = self.provider.buy_labels(ids, "PACKAGE_PARCEL")
        self.assertTrue(result[ids[0]].recoverable)
        self.assertIsNotNone(self.db.drafts["spd_abc"]["create_started_at"])

    def test_no_order_id_is_a_failed_label(self):
        ids = self.draft(boxes=2)
        self.http({("POST", "/api/v1/orders"): FakeResponse(200, {"validationErrors": ["Weight required"]})})
        result = self.provider.buy_labels(ids, "PACKAGE_PARCEL")
        self.assertEqual({bid: (s.label_status, s.error_message) for bid, s in result.items()},
                         {bid: (LabelStatus.FAILED, "Weight required") for bid in ids})
        self.assertIsNone(self.db.drafts["spd_abc"]["create_started_at"])

    def test_rejected_create_is_a_failed_label(self):
        ids = self.draft(boxes=1)
        self.http({("POST", "/api/v1/orders"): FakeResponse(
            400, {"PostalCode": {"errors": [{"errorMessage": "Out of area"}]}})})
        result = self.provider.buy_labels(ids, "PACKAGE_PARCEL")
        self.assertEqual((result[ids[0]].label_status, result[ids[0]].error_message),
                         (LabelStatus.FAILED, "Spee-Dee error (400): PostalCode: Out of area"))

    def test_missing_draft(self):
        result = self.provider.buy_labels(["spd_gone"], "PACKAGE_PARCEL")
        self.assertRegex(str(result["spd_gone"]), "get rates again")


class PollTests(SpeedeeCase):
    def test_temporary_barcodes_stay_pending(self):
        ids = self.draft(boxes=2, order_id=ORDER_ID)
        self.http({
            ("GET", f"/api/v1/orders/{ORDER_ID}/items"): FakeResponse(200, items("SPX1", "02")),
            ("GET", f"/api/v1/orders/{ORDER_ID}/status"): FakeResponse(200, {"status": "Received"}),
        })
        result = self.provider.poll_shipments(ids)
        self.assertEqual([result[b].label_status for b in ids], [LabelStatus.READY, LabelStatus.PENDING])

    def test_cancelled_order_fails(self):
        ids = self.draft(boxes=1, order_id=ORDER_ID)
        self.http({
            ("GET", f"/api/v1/orders/{ORDER_ID}/items"): FakeResponse(200, items("01")),
            ("GET", f"/api/v1/orders/{ORDER_ID}/status"): FakeResponse(200, {"status": 91}),
        })
        result = self.provider.poll_shipments(ids)
        self.assertEqual(result[ids[0]].label_status, LabelStatus.FAILED)

    def test_uncreated_draft_reports_not_created(self):
        ids = self.draft(boxes=2)
        result = self.provider.poll_shipments(ids)
        self.assertEqual([result[b].label_status for b in ids], [LabelStatus.NOT_CREATED] * 2)


class LabelTests(SpeedeeCase):
    def test_label_is_the_matching_items_zpl(self):
        zpl = {n: f"^XA^FDbox {n}^FS^XZ".encode() for n in (1, 2)}
        self.http({("GET", f"/api/v1/orders/{ORDER_ID}/items/label/STANDARD/0"): FakeResponse(200, {"labels": [
            {"itemId": f"item-{n}", "itemBarcode": f"SPX{n}", "encodedLabel": base64.b64encode(zpl[n]).decode()}
            for n in (1, 2)]})})
        state = sd.ShipmentState(provider_shipment_id=f"{ORDER_ID}#2", label_status=LabelStatus.READY,
                                 tracking_numbers=["SPX2"],
                                 raw={"order_id": ORDER_ID, "item_id": "item-2", "item_number": 2})
        self.assertEqual(self.provider.fetch_labels(state), [(zpl[2], "zpl")])

    def test_custom_label_template(self):
        self.db.settings[f"{KEY}_label_template"] = "CUSTOM4X6"
        fake = self.http({("GET", f"/api/v1/orders/{ORDER_ID}/items/label/CUSTOM4X6/0"):
                          FakeResponse(200, {"labels": []})})
        state = sd.ShipmentState(provider_shipment_id=ORDER_ID, label_status=LabelStatus.READY,
                                 raw={"order_id": ORDER_ID, "item_id": "item-1"})
        self.assertEqual(self.provider.fetch_labels(state), [])
        self.assertEqual(len(fake.calls), 1)


class CancelTests(SpeedeeCase):
    def test_boxes_of_one_order_cancel_once(self):
        fake = self.http({("DELETE", f"/api/v1/orders/{ORDER_ID}"): FakeResponse(200)})
        self.assertEqual(self.provider.cancel_all([f"{ORDER_ID}#1", f"{ORDER_ID}#2"]), [])
        self.assertEqual([c[:2] for c in fake.calls], [("DELETE", f"/api/v1/orders/{ORDER_ID}")])

    def test_uncreated_draft_is_dropped_without_a_call(self):
        ids = self.draft(boxes=2)
        fake = self.http({})
        self.assertEqual(self.provider.cancel_all(ids), [])
        self.assertEqual((fake.calls, self.db.drafts), ([], {}))

    def test_draft_with_an_order_cancels_the_order(self):
        ids = self.draft(boxes=1, order_id=ORDER_ID)
        fake = self.http({("DELETE", f"/api/v1/orders/{ORDER_ID}"): FakeResponse(200)})
        self.assertEqual(self.provider.cancel_all(ids), [])
        self.assertEqual([c[:2] for c in fake.calls], [("DELETE", f"/api/v1/orders/{ORDER_ID}")])

    def test_not_found_is_already_gone(self):
        self.http({("DELETE", f"/api/v1/orders/{ORDER_ID}"): FakeResponse(404, text="")})
        self.assertEqual(self.provider.cancel_all([ORDER_ID]), [])

    def test_picked_up_order_cannot_be_cancelled(self):
        self.http({("DELETE", f"/api/v1/orders/{ORDER_ID}"): FakeResponse(
            400, {"error": ["Order cannot be cancelled"]})})
        self.assertEqual(self.provider.cancel_all([ORDER_ID]),
                         [f"{ORDER_ID}: Spee-Dee error (400): Order cannot be cancelled"])


class SettingsTests(SpeedeeCase):
    def test_connection_uses_typed_username_and_stored_password_for_mask(self):
        calls = []

        def fake_get(url, auth=None, **kwargs):
            calls.append((url, auth))
            return FakeResponse(200, {"items": []})

        with patch.object(sd.requests, "get", fake_get):
            result = self.provider.test_connection(mode="sandbox", token=sd.MASK, username="typed")
        self.assertEqual(result, {"ok": True, "account": "typed connected (test)"})
        self.assertEqual(calls, [(f"{sd.BASE_URLS['sandbox']}/api/v1/account/workflows", ("typed", "pw"))])

    def test_rejected_credentials(self):
        with patch.object(sd.requests, "get", lambda *a, **k: FakeResponse(401, text="")):
            with self.assertRaisesRegex(ProviderError, "rejected \\(401\\)"):
                self.provider.test_connection(mode="production", token="x", username="u")

    def test_descriptor_has_both_modes_and_no_secrets(self):
        d = self.provider.descriptor()
        self.assertEqual([(f["key"], f["type"], f.get("mode")) for f in d["fields"]], [
            ("speedee_sandbox_username", "text", "sandbox"),
            ("speedee_sandbox_password", "secret", "sandbox"),
            ("speedee_production_username", "text", "production"),
            ("speedee_production_password", "secret", "production"),
            ("speedee_label_template", "text", None),
        ])
        self.assertNotIn("pw", json.dumps(d))
        self.assertEqual((d["mode_key"], d["mode"]), ("speedee_mode", "sandbox"))

    def test_instance_keys_its_settings(self):
        provider = sd.SpeedeeProvider("speedee-7", "North")
        self.db.settings.update({"speedee-7_mode": "production", "speedee-7_production_username": "n",
                                 "speedee-7_production_password": "p"})
        self.assertEqual(sd._auth(provider.name), (sd.BASE_URLS["production"], "n", "p"))
        self.assertFalse(provider.is_test_mode())

    def test_tracking_url(self):
        self.assertEqual(self.provider.tracking_url("SPX1"),
                         "https://speedeedelivery.com/track-a-shipment/?barcodes=SPX1")


if __name__ == "__main__":
    unittest.main()

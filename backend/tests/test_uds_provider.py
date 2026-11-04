import base64
import json
import sys
import time
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

# The provider reads settings through db; stub it so the module imports
# without a live Postgres. Each test swaps in a FakeDb on the module.
sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

from providers import base as provider_base  # noqa: E402
from providers import uds  # noqa: E402
from providers.base import LabelStatus, ProviderError  # noqa: E402

KEY = "uds"
VENDOR = "4321"
ORIGIN = {
    "origin_company": "Acme", "origin_contact": "Bob", "origin_address1": "1111 N Ridge Rd",
    "origin_address2": "", "origin_city": "Lombard", "origin_state": "il",
    "origin_zip": "60148", "origin_phone": "6305550100", "origin_email": "ship@acme.test",
}
CREDENTIALS = {f"{KEY}_client_id": "cid", f"{KEY}_client_secret": "secret", f"{KEY}_vendor_id": VENDOR}
LIVE_TOKEN = {f"{KEY}_access_token": "tok-live", f"{KEY}_token_client": "cid",
              f"{KEY}_token_expires_at": str(time.time() + 3000)}
DEST = {"contact": "Jane Roe", "company": "", "address1": "123 Main St", "address2": "Suite 101",
        "city": "Chicago", "state": "il", "zip": "60601-1234", "phone": "+1 (630) 930-5201",
        "email": "jane@x.test"}
BOX = {"weight": "12.5", "length": "12", "width": "10", "height": "8"}
FLAT_BOX = {"weight": "3", "length": "", "width": "", "height": ""}
ORDER_ID = 29814004
ZPL_LABEL = "^XA^FDJANE ROE^FS^XZ"
PDF_LABEL = base64.b64encode(b"%PDF-1.4 label").decode()
QUOTE = {"success": True, "Price": {"total": 14.25, "components": [
    {"Name": "Base Rate", "Value": 12.0, "Description": "Zone 2", "calculated": True},
    {"Name": "Fuel Surcharge", "Value": 2.25, "Description": "Percent of base", "calculated": True},
    {"Name": "Zone", "Value": 2, "Description": "Delivery zone", "calculated": False},
]}}
BREAKDOWN = [
    {"name": "Base Rate", "amount": 12.0, "description": "Zone 2", "included": True},
    {"name": "Fuel Surcharge", "amount": 2.25, "description": "Percent of base", "included": True},
    {"name": "Zone", "amount": 2.0, "description": "Delivery zone", "included": False},
]


class FakeDb:
    """Settings plus the uds_drafts table, interpreting just the statements
    the provider issues."""

    def __init__(self, settings=None):
        self.settings = {**{f"origin_{k[7:]}": v for k, v in ORIGIN.items()}, **CREDENTIALS,
                         **LIVE_TOKEN, **(settings or {})}
        self.drafts = {}
        self.locks = []

    def get_setting(self, key, default=None):
        value = self.settings.get(key)
        return default if value is None else value

    def set_setting(self, key, value):
        self.settings[key] = value

    def query(self, sql, params=None, one=False):
        if "advisory" in sql:
            self.locks.append(sql)
            return None
        if "FROM uds_drafts WHERE draft_id" in sql:
            row = self.drafts.get(params[0])
            return dict(row) if row and row["provider"] == params[1] else None
        if "FROM uds_drafts WHERE order_id" in sql:
            row = next((d for d in self.drafts.values() if d["order_id"] == params[0]), None)
            return dict(row) if row else None
        return None if one else []

    def execute(self, sql, params=None, returning=False):
        if sql.startswith("INSERT INTO uds_drafts"):
            draft_id, provider, body, quote, ref = params
            self.drafts[draft_id] = {"draft_id": draft_id, "provider": provider, "body": json.loads(body),
                                     "quote": json.loads(quote), "order_ref": ref, "order_id": None,
                                     "packages": None, "create_started_at": None}
        elif "SET create_started_at = now()" in sql:
            self.drafts[params[0]]["create_started_at"] = datetime.now(timezone.utc)
        elif "SET create_started_at = NULL" in sql:
            self.drafts[params[0]]["create_started_at"] = None
        elif "SET order_id" in sql:
            order_id, packages, draft_id = params
            self.drafts[draft_id].update(order_id=order_id, packages=json.loads(packages))
        elif sql.startswith("DELETE FROM uds_drafts WHERE draft_id"):
            draft = self.drafts.get(params[0])
            if draft and draft["order_id"] is None and draft["create_started_at"] is None:
                del self.drafts[params[0]]
        return None


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.payload = payload
        self.content = b"" if payload is None else json.dumps(payload).encode()
        self.text = self.content.decode()

    def json(self):
        if self.payload is None:
            raise ValueError("no body")
        return self.payload


def transport(routes):
    """A fake requests.request keyed by (METHOD, path after /v3). A list
    value is consumed one response per call; an Exception value is raised."""
    calls = []

    def request(method, url, json=None, timeout=None, headers=None, data=None):
        path = url.split("/v3", 1)[1]
        calls.append((method.upper(), path, json, (headers or {}).get("x-api-key"), data))
        key = (method.upper(), path)
        if key not in routes:
            raise AssertionError(f"unexpected call {key}")
        value = routes[key]
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        status, payload = value
        return FakeResponse(status, payload)

    def post(url, **kw):
        return request("POST", url, **kw)

    request.calls = calls
    request.post = post
    return request


def created(packages):
    return {"success": True, "message": "Vendor: 4321 Import containing 1 order(s).",
            "orders": [{"Order": {"OrderId": ORDER_ID, "OrderRef": "x"}, "Packages": packages}]}


class UdsCase(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        for target in (patch.object(uds, "db", self.db), patch.object(provider_base, "db", self.db),
                       patch.object(uds.time, "sleep", lambda s: None)):
            target.start()
            self.addCleanup(target.stop)
        self.provider = uds.UdsProvider()

    def http(self, routes):
        fake = transport(routes)
        for target in (patch.object(uds.requests, "request", fake),
                       patch.object(uds.requests, "post", fake.post)):
            target.start()
            self.addCleanup(target.stop)
        return fake

    def draft(self, parcels=(BOX,), **extra):
        body = uds.build_order(self.provider.origin(), DEST, list(parcels), {"reference": "#1001"}, "#1001-abc", uds.DEFAULT_LABEL_FORMAT)
        self.db.drafts["uds_d1"] = {"draft_id": "uds_d1", "provider": KEY, "body": body,
                                    "quote": {"total": 14.25, "boxes": len(parcels)}, "order_ref": "#1001-abc",
                                    "order_id": None, "packages": None, "create_started_at": None, **extra}
        return "uds_d1"


class PureHelpersTest(unittest.TestCase):
    def test_zip_value_keeps_five_digits_as_int_and_a_leading_zero_as_text(self):
        self.assertEqual([uds.zip_value(z) for z in ("60601-1234", "02134", " 60148 ", "601")],
                         [60601, "02134", 60148, "601"])

    def test_phone_value_is_ten_digits_without_country_code_or_none(self):
        self.assertEqual([uds.phone_value(p) for p in ("+1 (630) 930-5201", "630.930.5201", "930-5201", "", "0630930520")],
                         [6309305201, 6309305201, None, None, None])

    def test_every_documented_event_code_maps_to_a_known_status(self):
        codes = {"A": "info_received", "R": "in_transit", "S": "exception", "L": "out_for_delivery",
                 "D": "delivered", "EX": "exception", "AV": "will_call"}
        exceptions = ("EA3", "EB1", "EC2", "EN1", "EN2", "EN3", "EN4", "EN5", "EW2", "ED2", "EA2", "EC1", "EL1", "EW1")
        returns = ("ER1", "ER2", "ER3", "ER4", "ER5", "ER6", "ER7")
        expected = {**codes, **{c: "exception" for c in exceptions}, **{c: "returning" for c in returns}}
        self.assertEqual({c: uds.status_for_code(c.lower()) for c in expected}, expected)

    def test_business_days_skip_the_weekend(self):
        friday = date(2026, 11, 6)
        self.assertEqual([uds.business_days(friday, friday + timedelta(days=n)) for n in (0, 1, 3, 4)],
                         [1, 1, 1, 2])

    def test_order_ref_is_the_reference_plus_a_unique_token(self):
        self.assertEqual([uds.order_ref("#1001", "tok"), uds.order_ref("", "tok"), len(uds.order_ref("x" * 80, "t" * 12))],
                         ["#1001-tok", "tok", 50])


class CleanOptionsTest(unittest.TestCase):
    TODAY = date(2026, 11, 3)

    def test_valid_panel_is_trimmed_and_the_drop_location_normalized(self):
        raw = {"delivery_date": "2026-11-05", "instructions": "  Use   side door ", "access_code": "1234",
               "door_code": "", "parking": "Rear lot", "drop_location": "front door", "notes": "Call first",
               "description": "Case of cups", "ignored": "x"}
        self.assertEqual(uds.clean_options(raw, today=self.TODAY), {
            "delivery_date": "2026-11-05", "instructions": "Use side door", "access_code": "1234",
            "parking": "Rear lot", "drop_location": "Front Door", "notes": "Call first",
            "description": "Case of cups"})

    def test_invalid_values_are_refused_with_a_reason(self):
        cases = {
            "past": {"delivery_date": "2026-11-02"},
            "not a date": {"delivery_date": "11/05/2026"},
            "drop": {"drop_location": "Roof"},
            "long": {"instructions": "x" * 101},
        }
        messages = {}
        for name, raw in cases.items():
            with self.assertRaises(ValueError) as ctx:
                uds.clean_options(raw, today=self.TODAY)
            messages[name] = str(ctx.exception)
        self.assertEqual(messages, {
            "past": "UDS delivery date cannot be in the past",
            "not a date": "UDS delivery date must be a date (YYYY-MM-DD)",
            "drop": "Unknown UDS drop location 'Roof'",
            "long": "UDS instructions is limited to 100 characters",
        })

    def test_nothing_entered_is_no_options(self):
        self.assertEqual(uds.clean_options(None), {})


class BuildOrderTest(unittest.TestCase):
    ORIGIN = {**ORIGIN}

    def test_two_box_order_carries_every_field_uds_takes(self):
        options = {"signature": "adult", "reference": "#1001", "provider": {
            "delivery_date": "2026-11-05", "instructions": "Side door", "access_code": "1234",
            "drop_location": "Porch", "description": "Cups"}}
        dest = {**DEST, "company": "A very long company name that exceeds the limit"}
        body = uds.build_order(self.ORIGIN, dest, [BOX, FLAT_BOX], options, "#1001-tok", "LabelZplStrUDS_4x6")
        self.assertEqual(body, {
            "Order": {
                "OrderRef": "#1001-tok", "DeliveryDate": "2026-11-05",
                "LocationName": "A very long company name that excee", "Address1": "123 Main St",
                "Address2": "Suite 101", "City": "Chicago", "State": "IL", "ZipCode": 60601,
                "FromLocationName": "Acme", "FromAddress1": "1111 N Ridge Rd", "FromCity": "Lombard",
                "FromState": "IL", "FromZipCode": 60148, "ContactName": "Jane Roe", "Email": "jane@x.test",
                "Phone": 6309305201, "Instructions": "Side door", "SignatureRequired": True,
                "AdultSignatureRequired": True, "Pieces": 2, "Weight": 15.5,
                "PrintLabelFormat": "LabelZplStrUDS_4x6", "UseGroupByOrderRef": False,
                "AddressNotes": [{"Name": "AccessCode", "Value": "1234"},
                                 {"Name": "PreferredDropLocation", "Value": "Porch"}],
            },
            "Packages": [
                {"Barcode": "", "Description": "Cups", "Weight": 12.5, "PieceNumber": "#1001-tok-1",
                 "Length": 12.0, "Width": 10.0, "Height": 8.0},
                {"Barcode": "", "Description": "Cups", "Weight": 3.0, "PieceNumber": "#1001-tok-2"},
            ],
        })

    def test_plain_order_defaults_instructions_to_the_order_number_and_no_signature(self):
        body = uds.build_order(self.ORIGIN, {**DEST, "phone": "", "email": ""}, [FLAT_BOX],
                               {"reference": "#1001"}, "r", "zebra10000300")
        order = body["Order"]
        self.assertEqual({k: order.get(k) for k in ("Instructions", "SignatureRequired", "AdultSignatureRequired",
                                                     "Phone", "Email", "AddressNotes", "DeliveryDate")},
                         {"Instructions": "Order #1001", "SignatureRequired": False, "AdultSignatureRequired": False,
                          "Phone": None, "Email": None, "AddressNotes": None, "DeliveryDate": None})

    def test_incomplete_origin_is_refused(self):
        with self.assertRaises(ProviderError) as ctx:
            uds.build_order({**self.ORIGIN, "origin_zip": ""}, DEST, [BOX], {}, "r", "x")
        self.assertEqual(str(ctx.exception), "Origin address is incomplete — fill in on the Settings page: ZIP")


class TokenTest(UdsCase):
    def test_cached_token_is_sent_as_x_api_key_without_signing_in(self):
        fake = self.http({("POST", f"/order/quote/{VENDOR}"): (200, QUOTE)})
        self.provider.create_draft_shipments(DEST, [BOX], [], {})
        self.assertEqual([(c[0], c[1], c[3]) for c in fake.calls], [("POST", f"/order/quote/{VENDOR}", "tok-live")])

    def test_expired_token_is_replaced_under_the_lock(self):
        self.db.settings[f"{KEY}_token_expires_at"] = str(time.time() + 10)
        fake = self.http({("POST", "/token"): (200, {"access_token": "tok-new", "expires_in": 3600}),
                          ("POST", f"/order/quote/{VENDOR}"): (200, QUOTE)})
        self.provider.create_draft_shipments(DEST, [BOX], [], {})
        self.assertEqual((fake.calls[0][4], fake.calls[1][3], self.db.settings[f"{KEY}_access_token"], len(self.db.locks)),
                         ({"grant_type": "client_credentials", "client_id": "cid", "client_secret": "secret"},
                          "tok-new", "tok-new", 2))

    def test_a_403_signs_in_again_once(self):
        fake = self.http({("POST", "/token"): (200, {"access_token": "tok-new", "expires_in": 3600}),
                          ("POST", f"/order/quote/{VENDOR}"): [(403, {"message": "Forbidden"}), (200, QUOTE)]})
        self.provider.create_draft_shipments(DEST, [BOX], [], {})
        self.assertEqual([(c[1], c[3]) for c in fake.calls],
                         [(f"/order/quote/{VENDOR}", "tok-live"), ("/token", None), (f"/order/quote/{VENDOR}", "tok-new")])

    def test_rejected_credentials_say_so(self):
        self.db.settings[f"{KEY}_access_token"] = ""
        self.http({("POST", "/token"): (403, {"message": "Forbidden"})})
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_draft_shipments(DEST, [BOX], [], {})
        self.assertEqual(str(ctx.exception), "UDS rejected the Client ID / secret (403)")


class RatingTest(UdsCase):
    def test_quote_becomes_one_rate_with_its_breakdown_and_a_draft_per_box(self):
        fake = self.http({("POST", f"/order/quote/{VENDOR}"): (200, QUOTE)})
        drafts, rates, warnings = self.provider.create_draft_shipments(DEST, [BOX, FLAT_BOX], [], {"reference": "#1001"})
        draft_id = drafts[0].provider_shipment_id.split("#")[0]
        sent = fake.calls[0][2]
        self.assertEqual(
            ([d.provider_shipment_id for d in drafts], rates[0].to_ui(), warnings, isinstance(sent, dict),
             self.db.drafts[draft_id]["quote"], self.db.drafts[draft_id]["order_ref"] == sent["Order"]["OrderRef"]),
            ([f"{draft_id}#1", f"{draft_id}#2"],
             {"provider": "uds", "courier_service_id": "UDS", "courier_name": "UDS Delivery", "umbrella_name": "UDS",
              "total_charge": 14.25, "currency": "USD", "min_delivery_time": 1, "max_delivery_time": 1,
              "value_for_money_rank": 1, "breakdown": BREAKDOWN},
             [], True, {"total": 14.25, "boxes": 2, "breakdown": BREAKDOWN}, True))

    def test_a_quote_without_a_price_is_an_error(self):
        self.http({("POST", f"/order/quote/{VENDOR}"): (200, {"success": True, "Price": {"total": 0}})})
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_draft_shipments(DEST, [BOX], [], {})
        self.assertEqual(str(ctx.exception), "UDS returned no price for this shipment")

    def test_a_refused_quote_carries_the_uds_message(self):
        self.http({("POST", f"/order/quote/{VENDOR}"): (400, {"success": False, "message": "Zip not serviced"})})
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_draft_shipments(DEST, [BOX], [], {})
        self.assertEqual((str(ctx.exception), ctx.exception.status), ("UDS error (400): Zip not serviced", 400))

    def test_without_a_vendor_id_nothing_is_sent(self):
        self.db.settings[f"{KEY}_vendor_id"] = ""
        fake = self.http({})
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_draft_shipments(DEST, [BOX], [], {})
        self.assertEqual((str(ctx.exception), fake.calls),
                         ("No UDS Vendor ID configured — set it in Settings", []))


class BuyTest(UdsCase):
    def test_create_stores_barcodes_and_labels_and_every_box_is_ready(self):
        draft_id = self.draft(parcels=(BOX, FLAT_BOX))
        fake = self.http({("POST", f"/order/create/{VENDOR}"): (200, created([
            {"Barcode": "UDS1", "PieceNumber": "#1001-abc-1", "Label": ZPL_LABEL},
            {"Barcode": "UDS2", "PieceNumber": "#1001-abc-2", "Label": ZPL_LABEL + "2"},
        ]))})
        states = self.provider.buy_labels([f"{draft_id}#1", f"{draft_id}#2"], "UDS")
        self.assertEqual(
            ({k: (s.label_status, s.provider_shipment_id, s.tracking_numbers, s.cost) for k, s in states.items()},
             isinstance(fake.calls[0][2], list), self.db.drafts[draft_id]["packages"]),
            ({f"{draft_id}#1": (LabelStatus.READY, f"{ORDER_ID}#1", ["UDS1"], 7.12),
              f"{draft_id}#2": (LabelStatus.READY, f"{ORDER_ID}#2", ["UDS2"], 7.12)},
             True, [{"barcode": "UDS1", "label": ZPL_LABEL}, {"barcode": "UDS2", "label": ZPL_LABEL + "2"}]))

    def test_pdf_label_formats_are_base64_decoded(self):
        draft_id = self.draft()
        self.db.drafts[draft_id]["body"]["Order"]["PrintLabelFormat"] = "zebra10000300"
        self.http({("POST", f"/order/create/{VENDOR}"): (200, created(
            [{"Barcode": "UDS1", "PieceNumber": "#1001-abc-1", "Label": PDF_LABEL}]))})
        state = self.provider.buy_labels([draft_id], "UDS")[draft_id]
        self.assertEqual(self.provider.fetch_labels(state), [(b"%PDF-1.4 label", "pdf")])

    def test_zpl_label_formats_are_returned_as_text(self):
        draft_id = self.draft()
        self.http({("POST", f"/order/create/{VENDOR}"): (200, created(
            [{"Barcode": "UDS1", "PieceNumber": "#1001-abc-1", "Label": ZPL_LABEL}]))})
        state = self.provider.buy_labels([draft_id], "UDS")[draft_id]
        self.assertEqual(self.provider.fetch_labels(state), [(ZPL_LABEL.encode(), "zpl")])

    def test_a_refused_create_fails_the_boxes_and_can_be_bought_again(self):
        draft_id = self.draft()
        self.http({("POST", f"/order/create/{VENDOR}"): (400, {"success": False, "message": "Address1 too long"})})
        state = self.provider.buy_labels([draft_id], "UDS")[draft_id]
        self.assertEqual((state.label_status, state.error_message, self.db.drafts[draft_id]["create_started_at"]),
                         (LabelStatus.FAILED, "UDS error (400): Address1 too long", None))

    def test_a_gateway_error_keeps_the_create_marked_for_a_resend(self):
        draft_id = self.draft()
        self.http({("POST", f"/order/create/{VENDOR}"): (502, None)})
        result = self.provider.buy_labels([draft_id], "UDS")[draft_id]
        self.assertEqual((type(result), result.status, self.db.drafts[draft_id]["create_started_at"] is not None),
                         (ProviderError, 502, True))

    def test_a_lost_create_is_resent_with_the_same_order_ref(self):
        draft_id = self.draft(create_started_at=datetime.now(timezone.utc) - timedelta(seconds=30))
        fake = self.http({("POST", f"/order/create/{VENDOR}"): (200, created(
            [{"Barcode": "UDS1", "PieceNumber": "#1001-abc-1", "Label": ZPL_LABEL}]))})
        state = self.provider.buy_labels([draft_id], "UDS")[draft_id]
        self.assertEqual((state.label_status, fake.calls[0][2][0]["Order"]["OrderRef"]),
                         (LabelStatus.READY, "#1001-abc"))

    def test_a_resend_refused_as_duplicate_reports_the_order_uds_already_has(self):
        draft_id = self.draft(create_started_at=datetime.now(timezone.utc) - timedelta(seconds=30))
        self.http({
            ("POST", f"/order/create/{VENDOR}"): (400, {"success": False, "message": "Duplicate OrderRef"}),
            ("POST", "/tracking/orderRef"): (200, [{"PackageTrackingInfo": {"TrackingNumber": "UDS1"}}]),
        })
        state = self.provider.buy_labels([draft_id], "UDS")[draft_id]
        self.assertEqual((state.label_status, state.error_message), (
            LabelStatus.FAILED,
            "UDS created order #1001-abc (tracking UDS1) but its label response was lost — "
            "print or void it in the UDS portal, then undo this shipment"))

    def test_a_create_lost_over_twelve_hours_ago_is_never_resent(self):
        draft_id = self.draft(create_started_at=datetime.now(timezone.utc) - timedelta(hours=13))
        fake = self.http({("POST", "/tracking/orderRef"): (404, {"success": False, "message": "Not found"})})
        state = self.provider.buy_labels([draft_id], "UDS")[draft_id]
        self.assertEqual((state.label_status, [c[1] for c in fake.calls]), (LabelStatus.FAILED, ["/tracking/orderRef"]))

    def test_an_unknown_draft_asks_for_new_rates(self):
        self.http({})
        result = self.provider.buy_labels(["uds_missing"], "UDS")["uds_missing"]
        self.assertEqual(str(result), "UDS quote not found — get rates again")


class PollTest(UdsCase):
    def test_uncreated_draft_is_not_created_and_a_created_one_is_ready(self):
        pending = self.draft()
        self.db.drafts["uds_d2"] = {**self.db.drafts[pending], "draft_id": "uds_d2", "order_id": "77",
                                    "packages": [{"barcode": "UDS9", "label": ZPL_LABEL}]}
        states = self.provider.poll_shipments([pending, "77"])
        self.assertEqual({k: (s.label_status, s.tracking_numbers) for k, s in states.items()},
                         {pending: (LabelStatus.NOT_CREATED, []), "77": (LabelStatus.READY, ["UDS9"])})


class CancelTest(UdsCase):
    def test_bought_order_is_voided_once_and_unbought_draft_is_dropped(self):
        self.draft()
        self.db.drafts["uds_d2"] = {**self.db.drafts["uds_d1"], "draft_id": "uds_d2", "order_id": "77"}
        fake = self.http({("DELETE", "/order/id/77"): (200, {"success": True})})
        errors = self.provider.cancel_all(["77#1", "77#2", "uds_d1"])
        self.assertEqual((errors, [c[1] for c in fake.calls], sorted(self.db.drafts)),
                         ([], ["/order/id/77"], ["uds_d2"]))

    def test_already_gone_is_fine_and_a_received_order_is_an_error(self):
        self.http({("DELETE", "/order/id/77"): (404, {"success": False, "message": "Not found"}),
                   ("DELETE", "/order/id/78"): (400, {"success": False, "message": "Packages already received"})})
        self.assertEqual(self.provider.cancel_all(["77", "78"]),
                         ["UDS order 78: UDS error (400): Packages already received"])


ORDER_PAYLOAD = {"success": True, "orders": {"Order": {
    "OrderId": ORDER_ID, "OrderRef": "#1001-abc", "Status": "Assigned", "PriceStatus": "Billed",
    "PricingDefinitionName": "ZONE 2", "Price": QUOTE["Price"], "InvoiceId": 991, "DeliveryBy": "20:00",
    "CreatedTimestamp": "2026-11-03T10:00:00", "Instruction": "Side door", "SignatureRequired": True,
    "AdultSignatureRequired": False, "LocationName": "Jane Roe", "Address1": "123 Main St", "City": "Chicago",
    "State": "IL", "ZipCode": 60601, "ContactName": "Jane Roe", "Phone": 6309305201,
    "PODs": [{"Name": "J ROE", "Timestamp": "2026-11-04T14:00:00", "Type": "Delivery", "Latitude": 41.8,
              "Longitude": -87.6, "Images": [{"Type": "URL", "Value": "https://img"}]}]},
    "Packages": [{"Barcode": "UDS1", "Weight": 12.5, "DimWeight": 19.5, "PieceNumber": "#1001-abc-1"}]}}


class OrderTest(UdsCase):
    def test_order_details_are_normalized_for_the_tracking_view(self):
        self.http({("GET", f"/order/id/{ORDER_ID}"): (200, ORDER_PAYLOAD)})
        self.assertEqual(self.provider.order_details(f"{ORDER_ID}#1"), {
            "order_id": ORDER_ID, "order_ref": "#1001-abc", "status": "Assigned", "price_status": "Billed",
            "pricing_name": "ZONE 2", "price_total": 14.25, "price_breakdown": BREAKDOWN, "invoice_id": 991,
            "delivery_by": "20:00", "created_at": "2026-11-03T10:00:00", "instructions": "Side door",
            "signature": "signature",
            "address": {"company": "Jane Roe", "address1": "123 Main St", "address2": "", "city": "Chicago",
                        "state": "IL", "zip": "60601", "contact": "Jane Roe", "phone": "6309305201", "email": ""},
            "pods": [{"name": "J ROE", "at": "2026-11-04T14:00:00", "type": "Delivery", "latitude": 41.8,
                      "longitude": -87.6, "images": [{"type": "URL", "value": "https://img"}]}],
            "packages": [{"barcode": "UDS1", "weight": 12.5, "dim_weight": 19.5, "piece_number": "#1001-abc-1"}],
        })

    def test_update_sends_only_the_changed_fields_in_uds_shapes(self):
        fake = self.http({("PATCH", f"/order/id/{ORDER_ID}"): (200, ORDER_PAYLOAD)})
        self.provider.update_order(str(ORDER_ID), {"address1": "9 Elm St", "zip": "60602", "phone": "(630) 555-0199",
                                                    "contact": "Jim Roe", "instructions": "Ring bell"})
        self.assertEqual(fake.calls[0][2], {"Order": {
            "Address1": "9 Elm St", "ZipCode": 60602, "ContactName": "Jim Roe", "Phone": 6305550199,
            "Instructions": "Ring bell", "LocationName": "Jim Roe"}})

    def test_an_order_past_editing_says_so(self):
        self.http({("PATCH", f"/order/id/{ORDER_ID}"): (409, {"success": False, "message": "Order is Completed"})})
        with self.assertRaises(ProviderError) as ctx:
            self.provider.update_order(str(ORDER_ID), {"city": "Oak Park"})
        self.assertEqual((str(ctx.exception), ctx.exception.status), (
            f"UDS no longer allows changes to order {ORDER_ID}: UDS error (409): Order is Completed", 409))

    def test_a_bad_phone_is_refused_before_sending(self):
        fake = self.http({})
        with self.assertRaises(ProviderError):
            self.provider.update_order(str(ORDER_ID), {"phone": "555-0199"})
        self.assertEqual(fake.calls, [])


RECEIVED = {"PackageTrackingInfo": {
    "TrackingNumber": "UDS1", "OrderRef": "#1001-abc", "DeliveryDate": "2026-11-04T00:00:00-06:00",
    "TrackingEventHistory": [
        {"TrackingId": 11, "EventStatus": "R", "EventDescription": "Received",
         "EventDetail": "Package has been received by UDS.", "EventDateTime": "2026-11-03T13:24:14-06:00",
         "EventScanLocation": "LOMBARD, IL", "TrackingEventDetail": "",
         "EventScanLocationDetail": {"City": "LOMBARD", "StateProvinceCode": "IL"}},
        {"TrackingId": 12, "EventStatus": "D", "EventDescription": "Delivered", "EventDetail": "Delivered.",
         "EventDateTime": "2026-11-04T11:02:00-06:00", "SignedForByName": "J ROE",
         "AdditionalLocationInfo": "FRONT_DOOR", "VPODURL": "https://vpod", "SignatureImage": "iVBOR",
         "EventScanLocationDetail": {}},
    ],
    "Photos": [{"url": "https://photo", "shortDescription": "Front Door Photo", "timestamp": "2026-11-04T11:02:00-06:00"}],
}}


class TrackingTest(UdsCase):
    def test_tracking_response_becomes_flat_events(self):
        self.assertEqual(uds.normalize_tracking([RECEIVED]), [
            {"tracking_number": "UDS1", "event_id": 11, "status_code": "R", "status": "in_transit",
             "description": "Received", "detail": "Package has been received by UDS.", "exception_detail": "",
             "event_at": "2026-11-03T13:24:14-06:00", "location": "LOMBARD, IL", "signed_by": "", "left_at": "",
             "vpod_url": "", "signature_png": "", "photos": [], "rescheduled_for": None,
             "order_ref": "#1001-abc", "delivery_date": "2026-11-04T00:00:00-06:00"},
            {"tracking_number": "UDS1", "event_id": 12, "status_code": "D", "status": "delivered",
             "description": "Delivered", "detail": "Delivered.", "exception_detail": "",
             "event_at": "2026-11-04T11:02:00-06:00", "location": "", "signed_by": "J ROE", "left_at": "FRONT_DOOR",
             "vpod_url": "https://vpod", "signature_png": "iVBOR",
             "photos": [{"url": "https://photo", "label": "Front Door Photo", "at": "2026-11-04T11:02:00-06:00"}],
             "rescheduled_for": None, "order_ref": "#1001-abc", "delivery_date": "2026-11-04T00:00:00-06:00"},
        ])

    def test_an_event_without_an_id_gets_a_stable_one(self):
        ev = {"EventStatus": "A", "EventDateTime": "2026-11-03T08:00:00-06:00"}
        pkg = {"PackageTrackingInfo": {"TrackingNumber": "UDS1", "TrackingEventHistory": [ev]}}
        first, second = uds.normalize_tracking([pkg])[0]["event_id"], uds.normalize_tracking([pkg])[0]["event_id"]
        self.assertEqual((first == second, isinstance(first, int)), (True, True))

    def test_track_reads_each_number_and_reports_failures_separately(self):
        fake = self.http({("POST", "/tracking"): [(200, RECEIVED), (404, {"success": False, "message": "Unknown"})]})
        events, errors = self.provider.track(["UDS1", "UDS1", "UDS2"])
        self.assertEqual(([e["event_id"] for e in events], errors, [c[2] for c in fake.calls]),
                         ([11, 12], ["UDS2: UDS error (404): Unknown"],
                          [{"TrackingNumber": "UDS1"}, {"TrackingNumber": "UDS2"}]))

    def test_pending_pull_returns_every_new_event(self):
        self.http({("GET", "/tracking/pending"): (200, [RECEIVED])})
        self.assertEqual([e["event_id"] for e in self.provider.pull_tracking()], [11, 12])


class SettingsTest(UdsCase):
    def test_platform_is_manual_only_tracks_and_edits(self):
        self.assertEqual((self.provider.auto_mode, self.provider.supports_tracking(), self.provider.edits_orders,
                          self.provider.supports_cod, self.provider.supports_manifests(), self.provider.is_test_mode()),
                         (False, True, True, False, False, False))

    def test_label_format_and_poll_interval_fall_back_to_their_defaults(self):
        values = []
        for fmt, minutes in (("", ""), ("bogus", "x"), ("zebra10000300", "off"), ("LabelZplStr2", "5")):
            self.db.settings.update({f"{KEY}_label_format": fmt, f"{KEY}_tracking_poll_minutes": minutes})
            values.append((self.provider.label_format(), self.provider.poll_minutes()))
        self.assertEqual(values, [("LabelZplStrUDS_4x6", 15), ("LabelZplStrUDS_4x6", 15),
                                  ("zebra10000300", 0), ("LabelZplStr2", 5)])

    def test_descriptor_lists_credentials_label_format_and_tracking(self):
        self.assertEqual([f["key"] for f in self.provider.descriptor()["fields"]],
                         ["uds_client_id", "uds_client_secret", "uds_vendor_id", "uds_label_format",
                          "uds_tracking_poll_minutes"])

    def test_connection_test_signs_in_and_quotes_a_sample(self):
        fake = self.http({("POST", "/token"): (200, {"access_token": "tok-t", "expires_in": 3600}),
                          ("POST", f"/order/quote/{VENDOR}"): (200, QUOTE)})
        result = self.provider.test_connection(token="typed-secret", username="typed-id")
        self.assertEqual((result, fake.calls[0][4]["client_id"], fake.calls[1][3]), (
            {"ok": True, "account": "Vendor 4321 connected · 1 lb to your own address quotes $14.25"},
            "typed-id", "tok-t"))

    def test_connection_test_names_a_refused_vendor(self):
        self.http({("POST", "/token"): (200, {"access_token": "tok-t", "expires_in": 3600}),
                   ("POST", f"/order/quote/{VENDOR}"): (403, {"message": "Forbidden"})})
        with self.assertRaises(ProviderError) as ctx:
            self.provider.test_connection()
        self.assertEqual(str(ctx.exception), "Signed in, but UDS refused Vendor ID 4321 (403)")

    def test_connection_test_without_vendor_only_signs_in(self):
        self.db.settings[f"{KEY}_vendor_id"] = ""
        self.http({("POST", "/token"): (200, {"access_token": "tok-t", "expires_in": 3600})})
        self.assertEqual(self.provider.test_connection()["account"],
                         "Signed in — save the Vendor ID, then test again to check it")

    def test_tracking_link(self):
        self.assertEqual(self.provider.tracking_url("UDS1"),
                         "https://www.uniteddeliveryservice.com/track/barcode/UDS1")


if __name__ == "__main__":
    unittest.main()

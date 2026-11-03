import base64
import json
import sys
import types
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

# The provider reads settings through db; stub it so the module imports
# without a live Postgres. Each test swaps in a FakeDb on the module.
sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

from providers import base as provider_base  # noqa: E402
from providers import stamps as st  # noqa: E402
from providers.base import LabelStatus, ProviderError  # noqa: E402

KEY = "stamps"
ORIGIN = {
    "origin_company": "Acme", "origin_contact": "Bob", "origin_address1": "1 Main St",
    "origin_address2": "", "origin_city": "Anaheim", "origin_state": "CA",
    "origin_zip": "92802", "origin_phone": "7145550100", "origin_email": "ship@acme.test",
}
DEST = {"contact": "Jane Roe", "company": "", "address1": "351 S Studio Dr", "address2": "Apt 2",
        "city": "Lake Buena Vista", "state": "fl", "zip": "32830", "phone": "4075550100", "email": "j@x.test"}
BOX = {"weight": "2.5", "length": "12", "width": "10", "height": "8"}
FLAT = {"weight": "1", "length": "0", "width": "0", "height": "0"}
ACCESS_TOKEN = "access-1"
REFRESH_TOKEN = "refresh-1"
CLIENT_ID = "client-abc"
LABEL_ID = "13bab305-a5eb-4ad6-a1d2-e5b545e764fb"
TRACKING = "9405511899560772485630"
PDF = b"%PDF-1.4 label"


def connected(mode="sandbox", expires_in=3600, **extra):
    return {
        f"{KEY}_mode": mode,
        f"{KEY}_{mode}_client_id": CLIENT_ID,
        f"{KEY}_{mode}_access_token": ACCESS_TOKEN,
        f"{KEY}_{mode}_refresh_token": REFRESH_TOKEN,
        f"{KEY}_{mode}_token_expires_at": str(datetime.now(timezone.utc).timestamp() + expires_in),
        f"{KEY}_{mode}_account": "acme-ship",
        **extra,
    }


class FakeDb:
    """Settings plus the stamps_drafts table, interpreting just the statements
    the provider issues."""

    def __init__(self, settings=None):
        self.settings = {**{f"origin_{k[7:]}": v for k, v in ORIGIN.items()}, **(settings or {})}
        self.drafts = {}
        self.locks = []

    def get_setting(self, key, default=None):
        value = self.settings.get(key)
        return default if value in (None, "") else value

    def set_setting(self, key, value):
        self.settings[key] = value

    def query(self, sql, params=None, one=False):
        if "pg_advisory" in sql:
            self.locks.append(sql.split("(")[0].split()[-1])
            return {} if one else [{}]
        if "FROM stamps_drafts" in sql:
            column = "label_id" if "WHERE label_id" in sql else "draft_id"
            rows = [dict(r) for r in self.drafts.values() if r[column] == params[0] and r["provider"] == params[1]]
            return (rows[0] if rows else None) if one else rows
        return None if one else []

    def execute(self, sql, params=None, returning=False):
        if sql.startswith("INSERT INTO stamps_drafts"):
            draft_id, provider, body = params
            self.drafts[draft_id] = {"draft_id": draft_id, "provider": provider, "body": json.loads(body),
                                     "service_id": None, "buy_key": None, "buy_body": None,
                                     "buy_started_at": None, "label_id": None, "label": None}
        elif "SET buy_key = %s" in sql:
            key, body, service_id, draft_id = params
            row = self.drafts[draft_id]
            if row["buy_key"] is not None:
                return None
            row.update(buy_key=key, buy_body=json.loads(body), service_id=service_id,
                       buy_started_at=datetime.now(timezone.utc))
            return {"draft_id": draft_id}
        elif "SET buy_key = NULL" in sql:
            self.drafts[params[0]].update(buy_key=None, buy_body=None, service_id=None, buy_started_at=None)
        elif "SET label_id" in sql:
            label_id, label, draft_id = params
            self.drafts[draft_id].update(label_id=label_id, label=json.loads(label))
        elif sql.startswith("DELETE FROM stamps_drafts WHERE draft_id"):
            row = self.drafts.get(params[0])
            if row and row["label_id"] is None and row["buy_key"] is None:
                del self.drafts[params[0]]
        return None


class FakeResponse:
    def __init__(self, status_code, payload=None, text="", content=None):
        self.status_code = status_code
        self._payload = payload
        if content is not None:
            self.content = content
        else:
            self.content = json.dumps(payload).encode() if payload is not None else text.encode()
        self.text = text
        self.ok = status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def transport(routes):
    """Fake requests.request keyed by (METHOD, path); list values are consumed
    in order. An Exception value is raised."""
    calls = []

    def fake(method, url, **kwargs):
        path = url.split("/sera", 1)[1] if "/sera" in url else url
        calls.append({"method": method.upper(), "path": path, "json": kwargs.get("json"),
                      "params": kwargs.get("params"), "headers": kwargs.get("headers") or {}})
        entry = routes[(method.upper(), path)]
        if isinstance(entry, list):
            entry = entry.pop(0) if len(entry) > 1 else entry[0]
        if isinstance(entry, Exception):
            raise entry
        return entry

    fake.calls = calls
    return fake


def rate(service, total, packaging="package", days="2"):
    return {"carrier": "usps", "service_type": service, "packaging_type": packaging,
            "estimated_delivery_days": days,
            "shipment_cost": {"total_amount": total, "currency": "usd", "cost_details": []}}


def label(label_id=LABEL_ID, tracking=TRACKING, total=8.8, data=PDF):
    return {
        "label_id": label_id, "tracking_number": tracking, "carrier": "usps",
        "service_type": "usps_priority_mail", "packaging_type": "package",
        "shipment_cost": {"total_amount": total, "currency": "usd"},
        "labels": [{"href": None, "label_data": base64.b64encode(data).decode()}],
        "forms": None,
    }


class StampsCase(unittest.TestCase):
    settings = None

    def setUp(self):
        self.db = FakeDb(self.settings if self.settings is not None else connected())
        self.patches = [patch.object(st, "db", self.db), patch.object(provider_base, "db", self.db),
                        patch.object(st.time, "sleep", lambda s: None)]
        for p in self.patches:
            p.start()
        self.provider = st.StampsProvider()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def http(self, routes):
        fake = transport(routes)
        p = patch.object(st.requests, "request", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def token_endpoint(self, *responses):
        calls = []
        queue = list(responses)

        def fake(url, **kwargs):
            calls.append({"url": url, "json": kwargs.get("json")})
            entry = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(entry, Exception):
                raise entry
            return entry

        p = patch.object(st.requests, "post", fake)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def draft(self, **row):
        draft_id = st.DRAFT_PREFIX + uuid.uuid4().hex[:12]
        body = st._draft_body(st._origin_address(ORIGIN), st._dest_address(DEST), BOX, {"reference": "#1001"})
        self.db.drafts[draft_id] = {"draft_id": draft_id, "provider": KEY, "body": body, "service_id": None,
                                    "buy_key": None, "buy_body": None, "buy_started_at": None,
                                    "label_id": None, "label": None, **row}
        return draft_id


class RequestBodyTests(unittest.TestCase):
    def test_addresses_use_sera_field_names(self):
        self.assertEqual(st._dest_address(DEST), {
            "name": "Jane Roe", "address_line1": "351 S Studio Dr", "address_line2": "Apt 2",
            "city": "Lake Buena Vista", "state_province": "FL", "postal_code": "32830",
            "country_code": "US", "phone": "4075550100", "email": "j@x.test"})
        self.assertEqual(st._origin_address(ORIGIN), {
            "name": "Bob", "company_name": "Acme", "address_line1": "1 Main St", "city": "Anaheim",
            "state_province": "CA", "postal_code": "92802", "country_code": "US",
            "phone": "7145550100", "email": "ship@acme.test"})

    def test_incomplete_origin_names_missing_fields(self):
        with self.assertRaisesRegex(ProviderError, "ZIP"):
            st._origin_address({**ORIGIN, "origin_zip": ""})

    def test_package_is_pounds_and_inches_with_dimensions_only_when_complete(self):
        self.assertEqual(st._package(BOX), {"packaging_type": "package", "weight": 2.5, "weight_unit": "pound",
                                            "length": 12.0, "width": 10.0, "height": 8.0,
                                            "dimension_unit": "inch"})
        self.assertEqual(st._package(FLAT), {"packaging_type": "package", "weight": 1.0, "weight_unit": "pound"})

    def test_signature_options_map_to_delivery_confirmation(self):
        cases = {"none": None, "signature": "signature", "adult": "adult_signature"}
        for option, expected in cases.items():
            with self.subTest(option=option):
                body = st._rate_body({}, {}, BOX, {"signature": option})
                self.assertEqual(body.get("delivery_confirmation_type"), expected)

    def test_draft_body_adds_the_order_reference_to_the_rate_body(self):
        body = st._draft_body({}, {}, BOX, {"reference": "#1001"})
        self.assertEqual(body["references"], {"reference1": "#1001"})
        self.assertNotIn("references", st._rate_body({}, {}, BOX, {"reference": "#1001"}))

    def test_label_body_asks_for_a_4x6_base64_label(self):
        body = st._label_body({"package": {}}, "usps_priority_mail", "zpl", True)
        self.assertEqual((body["service_type"], body["label_options"], body["is_test_label"]), (
            "usps_priority_mail",
            {"label_size": "4x6", "label_format": "zpl", "label_output_type": "base64"},
            True))


class RateCombineTests(unittest.TestCase):
    def test_delivery_days_parse_single_values_and_ranges(self):
        cases = {"2": (2, 2), "1-2": (1, 2), "": (None, None), None: (None, None), "soon": (None, None)}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(st._delivery_days(raw), expected)

    def test_only_services_every_box_got_are_offered_at_the_summed_price(self):
        box1 = [rate("usps_priority_mail", 9.1), rate("usps_ground_advantage", 6.2),
                rate("usps_priority_mail", 8.0, packaging="usps_flat_rate_envelope")]
        box2 = [rate("usps_priority_mail", 11.4, days="1-3")]
        quotes = st._combine_per_box([box1, box2], KEY)
        self.assertEqual([(q.provider_service_id, q.total_charge, q.min_delivery_time, q.max_delivery_time,
                           q.courier_name, q.umbrella_name, q.currency) for q in quotes],
                         [("usps_priority_mail", 20.5, 2, 3, "USPS Priority Mail", "USPS", "USD")])

    def test_cheapest_is_ranked_best_value(self):
        quotes = st._combine_per_box([[rate("usps_priority_mail", 9.1), rate("usps_ground_advantage", 6.2)]], KEY)
        self.assertEqual([(q.provider_service_id, q.value_for_money_rank) for q in quotes],
                         [("usps_ground_advantage", 1), ("usps_priority_mail", None)])

    def test_unknown_service_gets_a_readable_name(self):
        self.assertEqual(st._service_name("usps_some_new_service"), "USPS Some New Service")


class TokenTests(StampsCase):
    def test_a_fresh_access_token_is_used_without_refreshing(self):
        calls = self.token_endpoint(FakeResponse(500))
        self.assertEqual(st._auth(KEY), (st.BASE_URLS["sandbox"], ACCESS_TOKEN))
        self.assertEqual(calls, [])

    def test_an_expiring_token_is_refreshed_under_a_lock_and_the_rotated_refresh_token_kept(self):
        self.db.settings.update(connected(expires_in=30))
        calls = self.token_endpoint(FakeResponse(200, {
            "access_token": "access-2", "refresh_token": "refresh-2", "expires_in": "900", "token_type": "Bearer"}))
        self.assertEqual(st._auth(KEY), (st.BASE_URLS["sandbox"], "access-2"))
        self.assertEqual(calls[0]["json"], {"grant_type": "refresh_token", "client_id": CLIENT_ID,
                                            "refresh_token": REFRESH_TOKEN})
        self.assertEqual((self.db.settings[f"{KEY}_sandbox_refresh_token"], self.db.locks),
                         ("refresh-2", ["pg_advisory_lock", "pg_advisory_unlock"]))

    def test_a_refused_refresh_token_signs_the_account_out(self):
        self.db.settings.update(connected(expires_in=0))
        self.token_endpoint(FakeResponse(403, {"error": "invalid_grant"}))
        with self.assertRaisesRegex(ProviderError, "Connect"):
            st._auth(KEY)
        self.assertEqual(self.db.get_setting(f"{KEY}_sandbox_refresh_token"), None)

    def test_an_unreachable_token_endpoint_keeps_the_sign_in(self):
        self.db.settings.update(connected(expires_in=0))
        self.token_endpoint(st.requests.ConnectionError("down"))
        with self.assertRaises(ProviderError):
            st._auth(KEY)
        self.assertEqual(self.db.get_setting(f"{KEY}_sandbox_refresh_token"), REFRESH_TOKEN)

    def test_without_a_sign_in_the_error_says_how_to_connect(self):
        self.db.settings = {f"{KEY}_mode": "production"}
        with self.assertRaisesRegex(ProviderError, "Connect"):
            st._auth(KEY)


class SignInTests(StampsCase):
    settings = {f"{KEY}_mode": "sandbox", f"{KEY}_sandbox_client_id": CLIENT_ID,
                f"{KEY}_sandbox_client_secret": "shh"}

    def test_authorize_url_requests_offline_access_for_the_registered_callback(self):
        url = self.provider.authorize_url("sandbox", "https://192.168.89.14/api/oauth/stamps/callback", "st8")
        parsed = urlparse(url)
        self.assertEqual((f"{parsed.scheme}://{parsed.netloc}{parsed.path}", parse_qs(parsed.query)), (
            "https://signin.testing.stampsendicia.com/authorize",
            {"response_type": ["code"], "client_id": [CLIENT_ID], "scope": ["offline_access"], "state": ["st8"],
             "redirect_uri": ["https://192.168.89.14/api/oauth/stamps/callback"]}))

    def test_authorize_url_needs_the_client_id(self):
        with self.assertRaisesRegex(ProviderError, "client ID"):
            self.provider.authorize_url("production", "https://x/cb", "s")

    def test_completing_sign_in_stores_tokens_and_the_account_name(self):
        calls = self.token_endpoint(FakeResponse(200, {
            "access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN, "expires_in": 3600}))
        self.http({("GET", "/v1/account"): FakeResponse(200, {
            "account": {"customer_details": {"username": "acme-ship"}}})})
        account = self.provider.complete_sign_in("sandbox", "code-9", "https://x/cb")
        self.assertEqual(calls[0]["json"], {"grant_type": "authorization_code", "client_id": CLIENT_ID,
                                            "client_secret": "shh", "code": "code-9", "redirect_uri": "https://x/cb"})
        self.assertEqual((account, self.provider.signed_in_accounts()), ("acme-ship", {"sandbox": "acme-ship",
                                                                                        "production": ""}))

    def test_a_token_response_without_a_refresh_token_is_refused(self):
        self.token_endpoint(FakeResponse(200, {"access_token": ACCESS_TOKEN, "expires_in": 3600}))
        with self.assertRaisesRegex(ProviderError, "offline_access"):
            self.provider.complete_sign_in("sandbox", "code-9", "https://x/cb")

    def test_disconnect_forgets_every_token(self):
        self.db.settings.update(connected())
        self.provider.disconnect("sandbox")
        self.assertEqual(self.provider.signed_in_accounts(), {"sandbox": "", "production": ""})


class RatingTests(StampsCase):
    def test_each_box_is_rated_and_one_draft_saved_per_box(self):
        fake = self.http({("POST", "/v1/rates"): [
            FakeResponse(200, [rate("usps_priority_mail", 9.1), rate("usps_ground_advantage", 6.2)]),
            FakeResponse(200, [rate("usps_priority_mail", 9.1)]),
        ]})
        drafts, quotes, warnings = self.provider.create_draft_shipments(DEST, [BOX, BOX], [], {"signature": "none"})
        self.assertEqual(([q.provider_service_id for q in quotes], len(drafts), len(self.db.drafts), warnings),
                         (["usps_priority_mail"], 2, 2, []))
        keys = {c["headers"]["Idempotency-Key"] for c in fake.calls}
        self.assertEqual((len(keys), fake.calls[0]["params"], fake.calls[0]["headers"]["Authorization"]),
                         (2, {"carriers": "usps"}, f"Bearer {ACCESS_TOKEN}"))

    def test_a_failed_box_fails_the_whole_rating(self):
        self.http({("POST", "/v1/rates"): FakeResponse(400, {"errors": [
            {"error_code": "800000", "error_message": "to_address.postal_code is invalid"}]})})
        with self.assertRaisesRegex(ProviderError, "postal_code is invalid"):
            self.provider.create_draft_shipments(DEST, [BOX], [], {})
        self.assertEqual(self.db.drafts, {})

    def test_no_common_service_is_an_error(self):
        self.http({("POST", "/v1/rates"): FakeResponse(200, [])})
        with self.assertRaisesRegex(ProviderError, "no USPS rates"):
            self.provider.create_draft_shipments(DEST, [BOX], [], {})


class BuyTests(StampsCase):
    def test_buy_sends_the_label_request_with_a_saved_idempotency_key(self):
        draft_id = self.draft()
        fake = self.http({("POST", "/v1/labels"): FakeResponse(200, label())})
        out = self.provider.buy_labels([draft_id], "usps_priority_mail")
        state = out[draft_id]
        sent = fake.calls[0]
        self.assertEqual((state.label_status, state.provider_shipment_id, state.tracking_numbers, state.cost,
                          state.courier_name, state.courier_umbrella_name),
                         (LabelStatus.READY, LABEL_ID, [TRACKING], 8.8, "USPS Priority Mail", "USPS"))
        row = self.db.drafts[draft_id]
        self.assertEqual((sent["headers"]["Idempotency-Key"], sent["json"], row["label_id"]),
                         (row["buy_key"], row["buy_body"], LABEL_ID))
        self.assertEqual(sent["json"]["references"], {"reference1": "#1001"})
        self.assertNotIn("label_data", json.dumps(row["label"]))

    def test_a_lost_response_is_resent_with_the_same_key(self):
        draft_id = self.draft()
        fake = self.http({("POST", "/v1/labels"): [st.requests.Timeout("lost"), st.requests.Timeout("lost"),
                                                   st.requests.Timeout("lost"), st.requests.Timeout("lost"),
                                                   FakeResponse(200, label())]})
        first = self.provider.buy_labels([draft_id], "usps_priority_mail")[draft_id]
        self.assertTrue(isinstance(first, ProviderError) and first.recoverable)
        polled = self.provider.poll_shipments([draft_id])[draft_id]
        second = self.provider.buy_labels([draft_id], "usps_priority_mail")[draft_id]
        keys = {c["headers"]["Idempotency-Key"] for c in fake.calls}
        self.assertEqual((polled.label_status, second.label_status, len(keys)),
                         (LabelStatus.NOT_CREATED, LabelStatus.READY, 1))

    def test_a_rejected_purchase_fails_and_releases_its_key(self):
        draft_id = self.draft()
        self.http({("POST", "/v1/labels"): FakeResponse(400, {"errors": [
            {"error_code": "800000", "error_message": "Insufficient postage balance"}]})})
        state = self.provider.buy_labels([draft_id], "usps_priority_mail")[draft_id]
        self.assertEqual((state.label_status, self.db.drafts[draft_id]["buy_key"]), (LabelStatus.FAILED, None))
        self.assertIn("Insufficient postage balance", state.error_message)

    def test_a_box_already_bought_is_never_bought_again(self):
        stored = {k: v for k, v in label().items() if k != "labels"}
        draft_id = self.draft(label_id=LABEL_ID, label=stored, buy_key=str(uuid.uuid4()))
        fake = self.http({})
        state = self.provider.buy_labels([draft_id], "usps_priority_mail")[draft_id]
        self.assertEqual((state.label_status, state.provider_shipment_id, fake.calls),
                         (LabelStatus.READY, LABEL_ID, []))

    def test_a_purchase_in_flight_cannot_switch_service(self):
        draft_id = self.draft(buy_key=str(uuid.uuid4()), service_id="usps_ground_advantage",
                              buy_body={}, buy_started_at=datetime.now(timezone.utc))
        self.http({})
        result = self.provider.buy_labels([draft_id], "usps_priority_mail")[draft_id]
        self.assertIsInstance(result, ProviderError)
        self.assertIn("USPS Ground Advantage", str(result))

    def test_an_unconfirmed_purchase_older_than_the_replay_window_is_not_resent(self):
        started = datetime.now(timezone.utc) - timedelta(hours=st.REPLAY_WINDOW_HOURS, minutes=1)
        draft_id = self.draft(buy_key=str(uuid.uuid4()), service_id="usps_priority_mail",
                              buy_body={}, buy_started_at=started)
        fake = self.http({})
        state = self.provider.buy_labels([draft_id], "usps_priority_mail")[draft_id]
        self.assertEqual((state.label_status, fake.calls), (LabelStatus.FAILED, []))
        self.assertIn("Stamps.com", state.error_message)

    def test_unknown_draft_asks_for_new_rates(self):
        self.http({})
        result = self.provider.buy_labels(["sed_missing"], "usps_priority_mail")["sed_missing"]
        self.assertIsInstance(result, ProviderError)
        self.assertIn("get rates again", str(result))

    def test_test_label_setting_is_sent(self):
        self.db.settings[f"{KEY}_test_labels"] = "true"
        draft_id = self.draft()
        fake = self.http({("POST", "/v1/labels"): FakeResponse(200, label())})
        self.provider.buy_labels([draft_id], "usps_priority_mail")
        self.assertEqual(fake.calls[0]["json"]["is_test_label"], True)


class LabelDocumentTests(StampsCase):
    def test_label_data_from_the_purchase_is_decoded(self):
        state = st._ready_state(label())
        self.assertEqual(self.provider.fetch_labels(state), [(PDF, "pdf")])

    def test_a_stored_label_is_reprinted_as_4x6(self):
        stored = {k: v for k, v in label().items() if k != "labels"}
        fake = self.http({("GET", f"/v1/labels/{LABEL_ID}"): FakeResponse(200, {
            "labels": [{"label_data": base64.b64encode(PDF).decode()}]})})
        docs = self.provider.fetch_labels(st._ready_state(stored))
        self.assertEqual((docs, fake.calls[0]["params"]), ([(PDF, "pdf")], {
            "label_size": "4x6", "label_format": "pdf", "label_output_type": "base64"}))


class CancelTests(StampsCase):
    def test_a_bought_label_is_voided_and_an_unbought_draft_deleted(self):
        bought = self.draft(label_id=LABEL_ID, label={}, buy_key="k")
        unbought = self.draft()
        fake = self.http({("PUT", f"/v1/labels/{LABEL_ID}/void"): FakeResponse(200)})
        errors = self.provider.cancel_all([LABEL_ID, unbought])
        self.assertEqual((errors, [c["path"] for c in fake.calls], sorted(self.db.drafts)),
                         ([], [f"/v1/labels/{LABEL_ID}/void"], [bought]))

    def test_an_unconfirmed_purchase_is_reported_not_forgotten(self):
        draft_id = self.draft(buy_key="k", buy_started_at=datetime.now(timezone.utc))
        self.http({})
        errors = self.provider.cancel_all([draft_id])
        self.assertEqual((len(errors), draft_id in self.db.drafts), (1, True))

    def test_an_already_voided_label_is_not_an_error(self):
        self.http({("PUT", f"/v1/labels/{LABEL_ID}/void"): FakeResponse(400, {"errors": [
            {"error_code": "800010", "error_message": "Label has already been voided"}]})})
        self.assertEqual(self.provider.cancel_all([LABEL_ID]), [])


class ManifestTests(StampsCase):
    def test_scan_form_covers_the_label_ids_and_downloads_the_document(self):
        draft_id = self.draft(label_id=LABEL_ID, label={}, buy_key="k")
        fake = self.http({
            ("POST", "/v1/manifests"): FakeResponse(200, {
                "manifest_id": "m-1", "carrier": "usps", "number_of_items_in_manifest": 1,
                "labels": [{"href": "https://files.test/m-1.pdf"}]}),
            ("GET", "https://files.test/m-1.pdf"): FakeResponse(200, content=PDF),
        })
        [result] = self.provider.create_manifest([LABEL_ID, draft_id])
        self.assertEqual(fake.calls[0]["json"], {"label_ids": [LABEL_ID], "label_options": {"label_format": "pdf"}})
        self.assertTrue(fake.calls[0]["headers"].get("Idempotency-Key"))
        self.assertEqual((result.provider_manifest_id, result.shipment_count, result.provider_shipment_ids,
                          result.document), ("m-1", 1, [LABEL_ID, draft_id], (PDF, "pdf")))


class SettingsSurfaceTests(StampsCase):
    def test_services_come_from_the_usps_catalog(self):
        self.http({("GET", "/v1/carrier-services"): FakeResponse(200, {"shipping_options": [
            {"carrier": "usps", "services": [{"service_type": "usps_priority_mail", "service_name": "Priority Mail"}]},
            {"carrier": "ups", "services": [{"service_type": "ups_ground", "service_name": "UPS Ground"}]},
        ]})})
        self.assertEqual(self.provider.list_courier_services(), [
            {"id": "usps_priority_mail", "umbrella_name": "USPS", "name": "Priority Mail"}])

    def test_connection_test_names_the_account_and_balance(self):
        self.http({("GET", "/v1/account"): FakeResponse(200, {"account": {"customer_details": {"username": "acme"}}}),
                   ("GET", "/v1/balance"): FakeResponse(200, {"amount_available": 390.65, "currency": "usd"})})
        self.assertEqual(self.provider.test_connection(mode="sandbox"),
                         {"ok": True, "account": "Signed in as acme · balance $390.65"})

    def test_descriptor_never_exposes_tokens(self):
        text = json.dumps(self.provider.descriptor())
        self.assertEqual((ACCESS_TOKEN in text, REFRESH_TOKEN in text), (False, False))

    def test_sandbox_mode_or_test_labels_show_the_test_badge(self):
        cases = [("sandbox", "", True), ("production", "", False), ("production", "true", True)]
        for mode, test_labels, expected in cases:
            with self.subTest(mode=mode, test_labels=test_labels):
                self.db.settings.update({f"{KEY}_mode": mode, f"{KEY}_test_labels": test_labels})
                self.assertEqual(self.provider.is_test_mode(), expected)


if __name__ == "__main__":
    unittest.main()

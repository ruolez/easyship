import os
import sys
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")

# The providers read settings through db; stub it so the modules import
# without a live Postgres. Tests patch get_setting on this namespace.
sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

import easyship_client as ec  # noqa: E402
from providers import easypost as ep  # noqa: E402
from providers import easyship as es  # noqa: E402
from providers import shippo as sp  # noqa: E402
from providers import shipstation as ss  # noqa: E402
from providers.base import ProviderError  # noqa: E402

DB = sys.modules["db"]

ORIGIN = {
    "origin_company": "Acme", "origin_contact": "Bob", "origin_address1": "1 Main St",
    "origin_address2": "", "origin_city": "Austin", "origin_state": "TX",
    "origin_zip": "78701", "origin_phone": "5125550100", "origin_email": "ship@acme.test",
}


def settings_stub(extra):
    values = {**ORIGIN, **extra}
    return lambda key, default=None: values.get(key, default)


class FakeResponse:
    def __init__(self, status_code, payload=None, content=b"", text=""):
        self.status_code = status_code
        self._payload = payload
        self.content = content or (b"{}" if payload is not None else b"")
        self.text = text
        self.headers = {}
        self.ok = status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def transport(routes):
    """Fake requests.request keyed by (METHOD, path); list values are consumed
    in order so a poll can see the state change."""
    calls = []

    def fake(method, url, **kwargs):
        path = url.split(".com", 1)[1]
        calls.append((method.upper(), path, kwargs.get("json")))
        entry = routes[(method.upper(), path)]
        payload = entry.pop(0) if isinstance(entry, list) else entry
        return payload if isinstance(payload, FakeResponse) else FakeResponse(200, payload)

    fake.calls = calls
    return fake


def pdf_download(_url, **_kwargs):
    return FakeResponse(200, content=b"%PDF-manifest")


class ShipStationManifestTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(DB, "get_setting", settings_stub({"shipstation_api_key": "key"}))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = ss.ShipStationProvider()

    def manifest(self, **extra):
        return {"manifest_id": "se-m-1", "submission_id": "SUB123", "shipments": 2,
                "label_ids": ["se-l-1", "se-l-2"],
                "manifest_download": {"href": "https://api.shipstation.com/v2/downloads/m.pdf"},
                **extra}

    def test_dedupes_shared_label_ids_and_returns_manifest(self):
        fake = transport({("POST", "/v2/manifests"): {"manifests": [self.manifest()]}})
        with patch.object(ss.requests, "request", fake), \
             patch.object(ss.requests, "get", pdf_download):
            results = self.provider.create_manifest(["se-l-1", "se-l-1", "se-l-2"])
        self.assertEqual(fake.calls, [("POST", "/v2/manifests",
                                       {"label_ids": ["se-l-1", "se-l-2"]})])
        self.assertEqual(
            (results[0].provider_manifest_id, results[0].ref_number,
             results[0].shipment_count, results[0].provider_shipment_ids, results[0].document),
            ("se-m-1", "SUB123", 2, ["se-l-1", "se-l-2"], (b"%PDF-manifest", "pdf")))

    def test_pending_request_is_polled_until_the_manifest_appears(self):
        fake = transport({
            ("POST", "/v2/manifests"): {"manifests": [],
                                        "manifest_requests": [{"manifest_request_id": "se-r-1",
                                                               "status": "in_progress"}]},
            ("GET", "/v2/manifests/se-r-1"): [FakeResponse(404, {"errors": []}),
                                              self.manifest()],
        })
        with patch.object(ss.requests, "request", fake), \
             patch.object(ss.requests, "get", pdf_download), \
             patch.object(ss.time, "sleep", lambda *_: None):
            results = self.provider.create_manifest(["se-l-1"])
        self.assertEqual(results[0].provider_manifest_id, "se-m-1")

    def test_carrier_rejection_surfaces_verbatim(self):
        fake = transport({("POST", "/v2/manifests"): FakeResponse(400, {
            "errors": [{"message": "label se-l-1 is already on a manifest"}]})})
        with patch.object(ss.requests, "request", fake):
            with self.assertRaisesRegex(ProviderError, "already on a manifest"):
                self.provider.create_manifest(["se-l-1"])


class ShippoManifestTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(DB, "get_setting", settings_stub({"shippo_token": "shippo_test_x"}))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = sp.ShippoProvider()

    def txn(self, account):
        return {"rate": {"object_id": "r1", "carrier_account": account}}

    def test_groups_transactions_by_carrier_account(self):
        fake = transport({
            ("GET", "/transactions/t1/"): self.txn("ca-1"),
            ("GET", "/transactions/t2/"): self.txn("ca-1"),
            ("GET", "/transactions/t3/"): self.txn("ca-2"),
            ("POST", "/manifests/"): [
                {"object_id": "mf-1", "status": "SUCCESS", "documents": ["https://x.com/1.pdf"]},
                {"object_id": "mf-2", "status": "SUCCESS", "documents": ["https://x.com/2.pdf"]},
            ],
        })
        with patch.object(sp.requests, "request", fake), \
             patch.object(sp.requests, "get", pdf_download):
            results = self.provider.create_manifest(["t1", "t2", "t3"])
        posted = [c for c in fake.calls if c[0] == "POST"]
        self.assertEqual(sorted(c[2]["transactions"] for c in posted), [["t1", "t2"], ["t3"]])
        self.assertEqual(sorted(c[2]["carrier_account"] for c in posted), ["ca-1", "ca-2"])
        self.assertEqual({r.provider_manifest_id for r in results}, {"mf-1", "mf-2"})

    def test_queued_manifest_is_polled_to_success(self):
        fake = transport({
            ("GET", "/transactions/t1/"): self.txn("ca-1"),
            ("POST", "/manifests/"): {"object_id": "mf-1", "status": "QUEUED"},
            ("GET", "/manifests/mf-1/"): [
                {"object_id": "mf-1", "status": "QUEUED"},
                {"object_id": "mf-1", "status": "SUCCESS", "documents": ["https://x.com/1.pdf"]},
            ],
        })
        with patch.object(sp.requests, "request", fake), \
             patch.object(sp.requests, "get", pdf_download), \
             patch.object(sp.time, "sleep", lambda *_: None):
            results = self.provider.create_manifest(["t1"])
        self.assertEqual(
            (results[0].provider_manifest_id, results[0].document),
            ("mf-1", (b"%PDF-manifest", "pdf")))

    def test_error_status_surfaces_shippo_messages(self):
        fake = transport({
            ("GET", "/transactions/t1/"): self.txn("ca-1"),
            ("POST", "/manifests/"): {"object_id": "mf-1", "status": "ERROR",
                                      "messages": [{"text": "shipment_date is in the past"}]},
        })
        with patch.object(sp.requests, "request", fake):
            with self.assertRaisesRegex(ProviderError, "shipment_date is in the past"):
                self.provider.create_manifest(["t1"])


class EasyPostManifestTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(DB, "get_setting", settings_stub({"easypost_token": "EZTKx"}))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = ep.EasyPostProvider()

    def test_scan_form_is_polled_until_created(self):
        fake = transport({
            ("POST", "/v2/scan_forms"): {"id": "sf-1", "status": "creating"},
            ("GET", "/v2/scan_forms/sf-1"): {"id": "sf-1", "status": "created",
                                             "form_url": "https://x.com/sf.pdf",
                                             "tracking_codes": ["9400", "9401"]},
        })
        with patch.object(ep.requests, "request", fake), \
             patch.object(ep.requests, "get", pdf_download), \
             patch.object(ep.time, "sleep", lambda *_: None):
            results = self.provider.create_manifest(["shp_1", "shp_2", "shp_1"])
        self.assertEqual(fake.calls[0], ("POST", "/v2/scan_forms",
                                         {"scan_form": {"shipments": [{"id": "shp_1"}, {"id": "shp_2"}]}}))
        self.assertEqual(
            (results[0].provider_manifest_id, results[0].shipment_count, results[0].document),
            ("sf-1", 2, (b"%PDF-manifest", "pdf")))

    def test_failed_scan_form_raises_with_reason(self):
        fake = transport({("POST", "/v2/scan_forms"): {
            "id": "sf-1", "status": "failed", "message": "shipment already manifested"}})
        with patch.object(ep.requests, "request", fake):
            with self.assertRaisesRegex(ProviderError, "already manifested"):
                self.provider.create_manifest(["shp_1"])


class EasyshipCourierResolutionTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(DB, "get_setting", settings_stub({
            "easyship_mode": "production", "easyship_production_token": "tok"}))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = es.EasyshipProvider()

    def test_shipment_courier_id_is_read_from_known_spots(self):
        self.assertEqual(es._shipment_courier_id({"courier": {"id": "c-1"}}), "c-1")
        self.assertEqual(es._shipment_courier_id({"courier_service": {"courier_id": "c-2"}}), "c-2")
        self.assertEqual(es._shipment_courier_id({"courier_service": {"id": "svc"}}), None)

    def test_shipment_courier_id_wins_over_the_account_list(self):
        with patch.object(ec, "get_shipment", lambda *a, **k: {"courier": {"id": "c-9"}}), \
             patch.object(ec, "list_couriers", lambda key: self.fail("should not be called")):
            self.assertEqual(self.provider._usps_courier_id("ESSG1"), "c-9")

    def test_single_usps_courier_settles_it(self):
        couriers = [{"id": "c-usps", "umbrella_name": "USPS", "name": "USPS"},
                    {"id": "c-ups", "umbrella_name": "UPS", "name": "UPS"}]
        with patch.object(ec, "get_shipment", lambda *a, **k: {}), \
             patch.object(ec, "list_couriers", lambda key: couriers):
            self.assertEqual(self.provider._usps_courier_id("ESSG1"), "c-usps")

    def test_no_usps_courier_raises(self):
        with patch.object(ec, "get_shipment", lambda *a, **k: {}), \
             patch.object(ec, "list_couriers", lambda key: [
                 {"id": "c-ups", "umbrella_name": "UPS", "name": "UPS"}]):
            with self.assertRaisesRegex(ProviderError, "No USPS courier"):
                self.provider._usps_courier_id("ESSG1")

    def test_ambiguous_usps_couriers_raise(self):
        couriers = [{"id": "c-1", "umbrella_name": "USPS", "name": "USPS East"},
                    {"id": "c-2", "umbrella_name": "USPS", "name": "USPS West"}]
        with patch.object(ec, "get_shipment", lambda *a, **k: {}), \
             patch.object(ec, "list_couriers", lambda key: couriers):
            with self.assertRaisesRegex(ProviderError, "USPS East, USPS West"):
                self.provider._usps_courier_id("ESSG1")

    def test_create_manifest_downloads_the_document(self):
        manifest = {"id": "m-1", "ref_number": "REF9", "shipments_count": 2,
                    "document": {"format": "url", "url": "https://x.com/m.pdf"}}
        with patch.object(ec, "get_shipment", lambda *a, **k: {"courier": {"id": "c-usps"}}), \
             patch.object(ec, "create_manifest",
                          lambda cid, ids, key: {**manifest, "_courier": cid, "_ids": list(ids)}), \
             patch.object(es.requests, "get", pdf_download):
            results = self.provider.create_manifest(["ESSG1", "ESSG2", "ESSG1"])
        self.assertEqual(
            (results[0].provider_manifest_id, results[0].ref_number, results[0].shipment_count,
             results[0].provider_shipment_ids, results[0].document,
             results[0].raw["_courier"], results[0].raw["_ids"]),
            ("m-1", "REF9", 2, ["ESSG1", "ESSG2"], (b"%PDF-manifest", "pdf"),
             "c-usps", ["ESSG1", "ESSG2"]))


if __name__ == "__main__":
    unittest.main()

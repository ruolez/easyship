import json
import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp",
    MANIFESTS_DIR="/tmp"))

from flask import Flask  # noqa: E402

import po_box  # noqa: E402
import providers  # noqa: E402
import shipments_api  # noqa: E402
from providers.base import DraftShipment, ProviderError, Rate  # noqa: E402

STREET = {"address1": "123 Main St", "city": "Austin", "state": "TX", "zip": "78701"}
PO_BOX = {**STREET, "address1": "PO Box 123"}


class IsPoBoxTest(unittest.TestCase):
    def test_recognises_the_common_spellings_on_either_line(self):
        spellings = ["PO Box 123", "P.O. Box 123", "P O BOX 123", "POBox 12",
                     "Post Office Box 9", "POB 44", "po box 7"]
        self.assertEqual(
            [po_box.is_po_box({"address1": s}) for s in spellings]
            + [po_box.is_po_box({"address1": "c/o Jane", "address2": "P.O. Box 55"})],
            [True] * (len(spellings) + 1))

    def test_street_addresses_that_merely_contain_box_are_not_po_boxes(self):
        streets = ["123 Main St", "45 Box Elder Rd", "10 Boxwood Ln", "8 Apo Box Rd",
                   "Suite 200", ""]
        self.assertEqual([po_box.is_po_box({"address1": s}) for s in streets], [False] * len(streets))

    def test_empty_destination_is_not_a_po_box(self):
        self.assertEqual(po_box.is_po_box({}), False)


def ui_rate(service_id, courier_name, umbrella_name):
    return {"courier_service_id": service_id, "courier_name": courier_name,
            "umbrella_name": umbrella_name, "total_charge": 1.0}


class SplitRatesTest(unittest.TestCase):
    def test_keeps_usps_and_usps_last_mile_services_from_every_provider_shape(self):
        kept = [
            ui_rate("es-1", "USPS - Priority Mail", "USPS"),            # Easyship
            ui_rate("usps_priority", "Priority Mail", "USPS"),          # Shippo
            ui_rate("USPS:Priority", "USPS Priority", "USPS"),          # EasyPost
            ui_rate("se-1:usps_priority_mail", "USPS Priority Mail", "Stamps.com"),  # ShipStation
            ui_rate("se-2:usps_first_class_mail", "USPS First Class Mail", "Endicia (via ShipStation)"),
            ui_rate("se-3:ups_surepost", "UPS SurePost", "UPS"),
            ui_rate("se-3:ups_ground_saver", "UPS® Ground Saver", "UPS"),
            ui_rate("se-4:fedex_ground_economy", "FedEx Ground Economy", "FedEx"),
            ui_rate("se-5:dhl_ecommerce", "DHL eCommerce Parcel Expedited", "DHL eCommerce"),
        ]
        hidden = [
            ui_rate("se-3:ups_ground", "UPS® Ground", "UPS"),
            ui_rate("se-4:fedex_2day", "FedEx 2Day", "FedEx"),
            ui_rate("dhl_express", "DHL Express Worldwide", "DHL Express"),
            ui_rate("se-4:fedex_home", "FedEx Home Delivery", "FedEx"),
        ]
        self.assertEqual(po_box.split_rates(hidden[:2] + kept + hidden[2:]),
                         (kept, ["DHL Express", "FedEx", "UPS"]))

    def test_nothing_hidden_reports_no_carriers(self):
        rates = [ui_rate("es-1", "USPS - Priority Mail", "USPS")]
        self.assertEqual(po_box.split_rates(rates), (rates, []))


def rate(service_id, courier_name, umbrella_name, provider="fake", charge=5.0):
    return Rate(provider=provider, provider_service_id=service_id, courier_name=courier_name,
                umbrella_name=umbrella_name, total_charge=charge, currency="USD",
                min_delivery_time=None, max_delivery_time=None, value_for_money_rank=None)


class FakeProvider:
    def __init__(self, name="fake", rates=None, error=None):
        self.name = name
        self.label = name.capitalize()
        self._rates = rates or []
        self._error = error

    def create_draft_shipments(self, destination, parcels, items, options=None):
        if self._error:
            raise ProviderError(self._error)
        return [DraftShipment(f"{self.name}-draft")], list(self._rates), []

    def get_excluded_service_ids(self):
        return set()


class RatesRouteTest(unittest.TestCase):
    """PO Box destinations only offer services that deliver to a PO Box, and a
    provider that fails while another quotes is reported rather than dropped."""

    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(shipments_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "user"
        self.providers = []
        db = shipments_api.db
        self._orig = (db.execute, db.get_setting, providers.enabled_for_user)
        db.execute = lambda sql, params=None, returning=False, **kw: {"id": 1} if returning else None
        db.get_setting = lambda key, default=None: default
        providers.enabled_for_user = lambda user_id, role: self.providers

    def tearDown(self):
        db = shipments_api.db
        db.execute, db.get_setting, providers.enabled_for_user = self._orig

    def _rates(self, destination):
        res = self.client.post("/api/shipments/rates", json={
            "destination": destination, "parcels": [{"weight": 2}], "items": []})
        return res.status_code, json.loads(res.get_data())

    def _mixed_rates(self):
        return [rate("ups_ground", "UPS® Ground", "UPS", charge=9.0),
                rate("usps_priority", "USPS Priority Mail", "Stamps.com", charge=7.0),
                rate("ups_surepost", "UPS SurePost", "UPS", charge=6.0),
                rate("fedex_2day", "FedEx 2Day", "FedEx", charge=20.0)]

    def test_po_box_destination_hides_non_po_box_services_and_says_so(self):
        self.providers = [FakeProvider(rates=self._mixed_rates())]
        status, body = self._rates(PO_BOX)
        self.assertEqual(
            (status, [r["courier_service_id"] for r in body["rates"]], body["warnings"], body["po_box"]),
            (200, ["ups_surepost", "usps_priority"],
             ["PO Box destination — only USPS-deliverable services are shown (hidden: FedEx, UPS)."],
             True))

    def test_street_destination_offers_every_service(self):
        self.providers = [FakeProvider(rates=self._mixed_rates())]
        status, body = self._rates(STREET)
        self.assertEqual(
            (status, [r["courier_service_id"] for r in body["rates"]], body["warnings"], body["po_box"]),
            (200, ["ups_surepost", "usps_priority", "ups_ground", "fedex_2day"], [], False))

    def test_po_box_with_no_deliverable_service_is_refused_with_the_reason(self):
        self.providers = [FakeProvider(rates=[rate("ups_ground", "UPS® Ground", "UPS"),
                                              rate("fedex_2day", "FedEx 2Day", "FedEx")])]
        status, body = self._rates(PO_BOX)
        self.assertEqual((status, body["error"]), (422, (
            "This address is a PO Box — none of the quoted services deliver to PO Boxes "
            "(quoted: FedEx, UPS). Use a USPS service or ship to a street address.")))

    def test_a_failing_provider_is_reported_as_a_warning_when_another_quotes(self):
        self.providers = [FakeProvider("broken", error="UPS: cannot ship to a P.O. Box"),
                          FakeProvider("fine", rates=[rate("usps_priority", "USPS Priority Mail", "USPS",
                                                           provider="fine")])]
        status, body = self._rates(STREET)
        self.assertEqual(
            (status, [r["courier_service_id"] for r in body["rates"]], body["warnings"]),
            (200, ["usps_priority"], ["Broken: UPS: cannot ship to a P.O. Box"]))


if __name__ == "__main__":
    unittest.main()

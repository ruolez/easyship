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

import auth  # noqa: E402
import providers  # noqa: E402
import shipments_api  # noqa: E402
from providers.base import DraftShipment, Rate  # noqa: E402

STREET = {"address1": "123 Main St", "city": "Austin", "state": "TX", "zip": "78701"}
COD = {"enabled": True, "amount": "125.40", "allow_partial": False}
STORED_COD = {"amount": 125.4, "allow_partial": False}


class FakeProvider:
    def __init__(self, name, supports_cod):
        self.name = name
        self.label = name.title()
        self.supports_cod = supports_cod
        self.rated_with = []

    def create_draft_shipments(self, destination, parcels, items, options=None):
        self.rated_with.append(options)
        return [DraftShipment(f"{self.name}-draft")], [Rate(
            provider=self.name, provider_service_id=f"{self.name}-svc", courier_name=f"{self.label} Ground",
            umbrella_name=self.label, total_charge=5.0, currency="USD", min_delivery_time=None,
            max_delivery_time=None, value_for_money_rank=None)], []

    def get_excluded_service_ids(self):
        return set()


class RatesCodTest(unittest.TestCase):
    """With COD on, only providers that collect on delivery are rated, so no
    label can go out without collecting the money."""

    def setUp(self):
        self.addCleanup(setattr, auth, "user_ships", auth.user_ships)
        auth.user_ships = lambda user_id, role: True
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(shipments_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "user"
        self.speedee = FakeProvider("speedee", True)
        self.shipstation = FakeProvider("shipstation", False)
        self.enabled = [self.shipstation, self.speedee]
        self.stored_options = []
        db = shipments_api.db
        self._orig = (db.query, db.execute, db.get_setting, providers.enabled_for_user, shipments_api.user_sections)

        def execute(sql, params=None, returning=False, **kw):
            if sql.lstrip().startswith("INSERT INTO shipments"):
                self.stored_options.append(json.loads(params[13]))
            return {"id": 1} if returning else None
        db.query = lambda sql, params=None, **kw: []
        db.execute = execute
        db.get_setting = lambda key, default=None: default
        providers.enabled_for_user = lambda user_id, role: self.enabled
        shipments_api.user_sections = lambda user_id, role: set()

    def tearDown(self):
        db = shipments_api.db
        (db.query, db.execute, db.get_setting, providers.enabled_for_user, shipments_api.user_sections) = self._orig

    def _rates(self, options):
        res = self.client.post("/api/shipments/rates", json={
            "source": "manual", "destination": STREET, "parcels": [{"weight": 2}], "items": [],
            "options": options})
        return res.status_code, json.loads(res.get_data())

    def test_cod_rates_only_the_providers_that_collect_and_says_why(self):
        status, body = self._rates({"signature": "none", "cod": COD})
        self.assertEqual((status, [r["provider"] for r in body["rates"]], body["warnings"], self.shipstation.rated_with),
                         (200, ["speedee"], ["COD is on — Shipstation was not rated (only Spee-Dee collects on delivery)"], []))

    def test_cod_reaches_the_provider_and_the_stored_shipment(self):
        self._rates({"signature": "adult", "cod": COD})
        expected = {"signature": "adult", "cod": STORED_COD}
        self.assertEqual((self.stored_options, self.speedee.rated_with),
                         ([expected], [{**expected, "reference": ""}]))

    def test_cod_with_no_provider_that_collects_is_refused(self):
        self.enabled = [self.shipstation]
        status, body = self._rates({"cod": COD})
        self.assertEqual((status, body["error"], self.shipstation.rated_with), (
            400, "COD is only available with Spee-Dee — choose Spee-Dee in 'Shipping with'", []))

    def test_cod_without_an_amount_is_refused_before_anything_is_saved(self):
        status, body = self._rates({"cod": {"enabled": True, "amount": "0"}})
        self.assertEqual((status, body["error"], self.stored_options),
                         (400, "Enter the amount to collect on delivery", []))

    def test_without_cod_every_provider_is_rated_and_nothing_is_collected(self):
        status, body = self._rates({"signature": "none", "cod": {"enabled": False, "amount": "50"}})
        self.assertEqual((status, sorted(r["provider"] for r in body["rates"]), self.stored_options),
                         (200, ["shipstation", "speedee"], [{"signature": "none"}]))


if __name__ == "__main__":
    unittest.main()

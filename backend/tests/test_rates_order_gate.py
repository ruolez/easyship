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

import profit  # noqa: E402
import providers  # noqa: E402
import shipments_api  # noqa: E402
import shopify_client  # noqa: E402
from providers.base import DraftShipment, Rate  # noqa: E402

STREET = {"address1": "123 Main St", "city": "Austin", "state": "TX", "zip": "78701"}
GID = "gid://shopify/Order/1"
SHIPPABLE = {"shippable": True, "label": None, "reasons": [], "on_hold": False, "fully_paid": True,
             "financial_status": "PAID", "holds": [], "outstanding": 0.0, "currency": "USD"}
HELD = dict(SHIPPABLE, shippable=False, label="On hold", on_hold=True,
            reasons=["Fulfillment is on hold in Shopify: Awaiting payment"])
ECON = profit.compute_economics(
    [{"description": "Widget", "sku": "W", "quantity": 1, "unit_price": 20.0, "unit_cost": 5.0}],
    20.0, 0, "USD")


class FakeProvider:
    name = "fake"
    label = "Fake"

    def create_draft_shipments(self, destination, parcels, items, options=None):
        return [DraftShipment("fake-draft")], [Rate(
            provider="fake", provider_service_id="svc-1", courier_name="Fake Ground",
            umbrella_name="Fake", total_charge=5.0, currency="USD", min_delivery_time=None,
            max_delivery_time=None, value_for_money_rank=None)], []

    def get_excluded_service_ids(self):
        return set()


class RatesOrderGateTest(unittest.TestCase):
    """Rating a Shopify order reads it once from the server side, snapshots
    the gate on box 1 and still returns rates when the order cannot ship."""

    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(shipments_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "user"
        self.settings = {}
        self.executed = []
        self.fetched = []
        self.order = {"gate": SHIPPABLE, "economics": ECON}
        self.error = None
        db = shipments_api.db
        self._orig = (db.execute, db.get_setting, providers.enabled_for_user, shopify_client.get_order)

        def execute(sql, params=None, returning=False, **kw):
            self.executed.append((sql, params))
            return {"id": len([s for s, _ in self.executed if "INSERT" in s])} if returning else None

        def get_order(store_id, gid):
            self.fetched.append((store_id, gid))
            if self.error:
                raise self.error
            return self.order
        db.execute = execute
        db.get_setting = lambda key, default=None: self.settings.get(key, default)
        providers.enabled_for_user = lambda user_id, role: [FakeProvider()]
        shopify_client.get_order = get_order

    def tearDown(self):
        db = shipments_api.db
        db.execute, db.get_setting, providers.enabled_for_user, shopify_client.get_order = self._orig

    def _rates(self, **body):
        payload = {"source": "shopify", "store_id": 3, "order_id": GID, "order_name": "#1001",
                   "destination": STREET, "parcels": [{"weight": 2}, {"weight": 3}], "items": []}
        payload.update(body)
        res = self.client.post("/api/shipments/rates", json=payload)
        return res.status_code, json.loads(res.get_data())

    def _snapshots(self):
        return [(params[1], json.loads(params[0])) for sql, params in self.executed if "order_gate" in sql]

    def test_order_is_read_once_and_the_gate_snapshotted_on_box_one(self):
        self.settings[profit.SETTING_MIN_AMOUNT] = "1"
        status, body = self._rates()
        self.assertEqual((status, self.fetched, self._snapshots(), body["order_gate"], body["warnings"]),
                         (200, [(3, GID)], [(1, SHIPPABLE)], SHIPPABLE, []))
        self.assertEqual((body["economics"]["revenue"], body["rates"][0]["profit"]["profit"]), (20.0, 10.0))

    def test_blocked_order_still_gets_rates_with_the_warning(self):
        self.order = {"gate": HELD, "economics": ECON}
        status, body = self._rates()
        self.assertEqual((status, len(body["rates"]), body["order_gate"], body["warnings"]), (200, 1, HELD, [
            "On hold — rates only, a label cannot be bought: Fulfillment is on hold in Shopify: Awaiting payment"]))

    def test_unreachable_shopify_leaves_no_snapshot_and_unavailable_economics(self):
        self.settings[profit.SETTING_MIN_AMOUNT] = "1"
        self.error = shopify_client.ShopifyUnavailable("Shopify unavailable after 3 attempts")
        status, body = self._rates()
        self.assertEqual((status, self._snapshots(), body["order_gate"], body["warnings"],
                          body["economics"]["available"], body["economics"]["reason"]),
                         (200, [], None, [], False, "Shopify unavailable after 3 attempts"))

    def test_order_never_loaded_is_not_read(self):
        status, body = self._rates(order_id=None)
        self.assertEqual((status, self.fetched, body["order_gate"]), (200, [], None))

    def test_manual_and_backoffice_never_read_shopify(self):
        for source in ("manual", "backoffice"):
            with self.subTest(source=source):
                status, body = self._rates(source=source, order_id=None, invoice_id=7, db_id=1)
                self.assertEqual((status, self.fetched, body["order_gate"]), (200, [], None))


if __name__ == "__main__":
    unittest.main()

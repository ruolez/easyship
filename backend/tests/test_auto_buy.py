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
LABEL_COST = 5.0
# revenue 20, items 5 → profit 10 with the 5.00 label; 20 − 18 − 5 is a loss
PROFITABLE = profit.compute_economics(
    [{"description": "Widget", "sku": "W", "quantity": 1, "unit_price": 20.0, "unit_cost": 5.0}],
    20.0, 0, "USD")
LOSING = profit.compute_economics(
    [{"description": "Widget", "sku": "W", "quantity": 1, "unit_price": 20.0, "unit_cost": 18.0}],
    20.0, 0, "USD")
UNVERIFIED = {"verified": False, "message": "This order has not been verified", "unverified": []}


class FakeProvider:
    name = "fake"
    label = "Fake"

    def create_draft_shipments(self, destination, parcels, items, options=None):
        return [DraftShipment("fake-draft")], [Rate(
            provider="fake", provider_service_id="svc-1", courier_name="Fake Ground",
            umbrella_name="Fake", total_charge=LABEL_COST, currency="USD", min_delivery_time=None,
            max_delivery_time=None, value_for_money_rank=None)], []

    def get_excluded_service_ids(self):
        return set()


def rated_row():
    return {"id": 1, "group_id": "g1", "box_number": 1, "box_total": 1, "source": "shopify",
            "shopify_store_id": 3, "shopify_order_id": GID, "order_gate": SHIPPABLE,
            "shopify_order_name": "#1001", "backoffice_invoice_number": None, "status": "rated",
            "courier_service_id": None, "rate": None, "provider": "fake",
            "provider_drafts": {"fake": "fake-draft"}, "easyship_shipment_id": "fake-draft",
            "tracking_number": None, "progress": None, "updated_at": None, "verify_gate": None,
            "profit_check": None}


class AutoBuyTest(unittest.TestCase):
    """Auto Mode's rates request buys the preferred rate itself when no gate
    needs the packer, reusing the order and verification it just read."""

    def setUp(self):
        self.addCleanup(setattr, auth, "user_ships", auth.user_ships)
        auth.user_ships = lambda user_id, role: True  # access itself: test_user_access
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(shipments_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "user"
        self.settings = {}
        self.order = {"gate": SHIPPABLE, "economics": PROFITABLE}
        self.verify = {"verified": True}
        self.verify_calls = []
        self.gate_reads = []
        self.threads = []
        db = shipments_api.db
        self._orig = (db.query, db.execute, db.get_setting, providers.enabled_for_user,
                      shopify_client.get_order, shopify_client.get_order_gate, shipments_api.threading,
                      shipments_api.audit, shipments_api.verify_gate.check, shipments_api.user_sections)

        def execute(sql, params=None, returning=False, **kw):
            return {"id": 1} if returning else None

        def verify_check(shipment):
            self.verify_calls.append(shipment)
            return self.verify
        db.query = lambda sql, params=None, **kw: [rated_row()]
        db.execute = execute
        db.get_setting = lambda key, default=None: self.settings.get(key, default)
        providers.enabled_for_user = lambda user_id, role: [FakeProvider()]
        shopify_client.get_order = lambda store_id, gid: self.order
        shopify_client.get_order_gate = lambda store_id, gid: self.gate_reads.append(gid) or SHIPPABLE
        shipments_api.threading = types.SimpleNamespace(
            Thread=lambda **kw: self.threads.append(kw["args"]) or types.SimpleNamespace(start=lambda: None))
        shipments_api.audit = lambda action, detail=None, user_id=None: None
        shipments_api.verify_gate.check = verify_check
        shipments_api.user_sections = lambda user_id, role: set()

    def tearDown(self):
        db = shipments_api.db
        (db.query, db.execute, db.get_setting, providers.enabled_for_user,
         shopify_client.get_order, shopify_client.get_order_gate, shipments_api.threading,
         shipments_api.audit, shipments_api.verify_gate.check, shipments_api.user_sections) = self._orig

    def _rates(self, **body):
        payload = {"source": "shopify", "store_id": 3, "order_id": GID, "order_name": "#1001",
                   "destination": STREET, "parcels": [{"weight": 2}], "items": [],
                   "preferred_service_id": "svc-1", "auto_buy": True}
        payload.update(body)
        res = self.client.post("/api/shipments/rates", json=payload)
        return res.status_code, json.loads(res.get_data())

    def test_preferred_rate_with_every_gate_passing_is_bought_in_the_same_request(self):
        status, body = self._rates(print={"mode": "network", "host": "10.0.0.9", "port": "9105", "dpi": "300"})
        worker_args = [args[1:] for args in self.threads]  # args[0] is the Flask app
        self.assertEqual((status, body["auto_buy"]["started"], worker_args), (200, True, [(
            body["group_id"], "fake", "svc-1", {
                "provider": "fake", "courier_service_id": "svc-1", "courier_name": "Fake Ground",
                "umbrella_name": "Fake", "total_charge": LABEL_COST, "currency": "USD",
                "min_delivery_time": None, "max_delivery_time": None, "value_for_money_rank": None,
                "preferred": True, "preferred_by": "preset"}, 1,
            {"mode": "network", "host": "10.0.0.9", "port": 9105, "dpi": 300})]))

    def test_the_order_gate_just_read_replaces_the_live_buy_time_read(self):
        self._rates()
        self.assertEqual((self.gate_reads, len(self.threads)), ([], 1))

    def test_verification_is_checked_once_with_the_order_from_the_request(self):
        self._rates()
        self.assertEqual(self.verify_calls, [{
            "source": "shopify", "shopify_store_id": 3, "shopify_order_id": GID,
            "shopify_order_name": "#1001", "backoffice_db_id": None, "backoffice_invoice_id": None,
            "backoffice_invoice_number": None}])

    def test_no_preferred_rate_leaves_the_choice_to_the_packer(self):
        status, body = self._rates(preferred_service_id="other-svc")
        self.assertEqual((status, body["auto_buy"], self.threads),
                         (200, {"started": False, "code": "no_preferred"}, []))

    def test_preferred_rate_below_the_profit_threshold_waits_for_the_bypass_password(self):
        self.settings[profit.SETTING_MIN_AMOUNT] = "1"
        self.order = {"gate": SHIPPABLE, "economics": LOSING}
        status, body = self._rates()
        self.assertEqual((status, body["auto_buy"], self.threads),
                         (200, {"started": False, "code": "profit_gate"}, []))

    def test_order_on_hold_is_not_bought_and_not_read_again(self):
        self.order = {"gate": HELD, "economics": PROFITABLE}
        status, body = self._rates()
        self.assertEqual((status, body["auto_buy"], self.gate_reads, self.threads),
                         (200, {"started": False, "code": "order_gate"}, [], []))

    def test_unverified_order_returns_the_verify_refusal_for_the_override_prompt(self):
        self.verify = UNVERIFIED
        status, body = self._rates()
        self.assertEqual((status, body["auto_buy"], self.threads), (200, {
            "started": False, "error": UNVERIFIED["message"], "code": "verify_gate",
            "verify": UNVERIFIED, "can_override": False}, []))

    def test_unreachable_shopify_falls_back_to_the_live_gate_read_at_buy(self):
        def down(store_id, gid):
            raise shopify_client.ShopifyUnavailable("Shopify unavailable after 3 attempts")
        shopify_client.get_order = down
        status, body = self._rates()
        self.assertEqual((status, body["auto_buy"]["started"], self.gate_reads), (200, True, [GID]))

    def test_without_auto_buy_nothing_is_bought_or_verified(self):
        status, body = self._rates(auto_buy=False)
        self.assertEqual((status, "auto_buy" in body, self.threads, self.verify_calls),
                         (200, False, [], []))


if __name__ == "__main__":
    unittest.main()

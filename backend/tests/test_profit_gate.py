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
from werkzeug.security import generate_password_hash  # noqa: E402

import profit  # noqa: E402
import providers  # noqa: E402
import shipments_api  # noqa: E402
import shopify_client  # noqa: E402

PASSWORD = "open-sesame"
RATE = {"provider": "fake", "courier_service_id": "svc-1", "courier_name": "Fake Ground",
        "total_charge": 12.35, "currency": "USD"}
# revenue 20, cost 15 → profit −7.35 with the 12.35 label
ECON = profit.compute_economics(
    [{"description": "Widget", "sku": "W", "quantity": 1, "unit_price": 20.0, "unit_cost": 15.0}],
    20.0, 0, "USD")


class FakeProvider:
    name = "fake"
    label = "Fake"


def row(**over):
    base = {"id": 7, "group_id": "g1", "box_number": 1, "box_total": 1, "source": "shopify",
            "shopify_store_id": 3, "shopify_order_id": None, "order_gate": None,
            "shopify_order_name": "#1001", "backoffice_invoice_number": None, "status": "rated",
            "courier_service_id": None, "rate": None, "provider": "fake",
            "provider_drafts": {"fake": "draft-1"}, "easyship_shipment_id": None,
            "tracking_number": None, "progress": None, "updated_at": None,
            "profit_check": profit.snapshot(ECON, [RATE])}
    base.update(over)
    return base


class BuyRouteTest(unittest.TestCase):
    """The buy route with the database, providers, the worker thread and the
    audit log faked; subclasses add the cases."""

    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(shipments_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "user"
        self.settings = {profit.SETTING_MIN_AMOUNT: "10", profit.SETTING_MIN_PCT: "",
                         profit.SETTING_PASSWORD: generate_password_hash(PASSWORD)}
        self.rows = [row()]
        self.executed = []
        self.audits = []
        db = shipments_api.db
        self._orig = (db.query, db.execute, db.get_setting, providers.enabled_for_user,
                      shipments_api.threading, shipments_api.audit)
        db.query = lambda sql, params=None, **kw: list(self.rows)
        db.execute = lambda sql, params=None, **kw: self.executed.append((sql, params))
        db.get_setting = lambda key, default=None: self.settings.get(key, default)
        providers.enabled_for_user = lambda user_id, role: [FakeProvider()]
        shipments_api.threading = types.SimpleNamespace(
            Thread=lambda **kw: types.SimpleNamespace(start=lambda: None))
        shipments_api.audit = lambda action, detail=None, user_id=None: self.audits.append((action, detail))

    def tearDown(self):
        db = shipments_api.db
        (db.query, db.execute, db.get_setting, providers.enabled_for_user,
         shipments_api.threading, shipments_api.audit) = self._orig

    def _buy(self, **body):
        payload = {"provider": "fake", "courier_service_id": "svc-1", "rate": RATE}
        payload.update(body)
        res = self.client.post("/api/shipments/group/g1/buy", json=payload)
        return res.status_code, json.loads(res.get_data())

    def _cleared_marker(self):
        for sql, params in self.executed:
            if "profit_check" in sql:
                return json.loads(params[0])["cleared"]
        return None


class ProfitGateTest(BuyRouteTest):
    """A buy below the profit threshold is refused unless the bypass password
    is supplied; the label cost comes from the server's own rate snapshot."""

    def test_below_threshold_without_password_is_refused_with_the_breakdown(self):
        status, body = self._buy()
        self.assertEqual(status, 403)
        self.assertEqual((body["code"], body["bypass"]), ("profit_gate", "required"))
        self.assertEqual((body["profit"]["profit"], body["profit"]["label_cost"], body["profit"]["below_threshold"]),
                         (-7.35, 12.35, True))
        self.assertEqual((self.audits, self._cleared_marker()), ([], None))

    def test_wrong_password_is_refused_and_audited(self):
        status, body = self._buy(bypass_password="nope")
        self.assertEqual((status, body["bypass"]), (403, "wrong_password"))
        self.assertEqual(self.audits, [("profit.bypass_denied", {
            "group_id": "g1", "provider": "fake", "courier_service_id": "svc-1"})])

    def test_no_password_on_file_cannot_be_bypassed(self):
        self.settings[profit.SETTING_PASSWORD] = ""
        status, body = self._buy(bypass_password=PASSWORD)
        self.assertEqual((status, body["bypass"]), (403, "wrong_password"))

    def test_correct_password_buys_audits_and_clears_the_gate(self):
        status, body = self._buy(bypass_password=PASSWORD)
        self.assertEqual((status, body), (200, {"started": True, "box_count": 1}))
        self.assertEqual(self.audits, [("profit.bypass", {
            "group_id": "g1", "source": "shopify", "order": "#1001", "provider": "fake",
            "courier_service_id": "svc-1", "courier_name": "Fake Ground",
            "revenue": 20.0, "items_cost": 15.0, "label_cost": 12.35,
            "profit": -7.35, "margin_pct": -36.8,
            "reasons": ["Profit -$7.35 is below the $10.00 minimum"],
            "thresholds": {"min_amount": 10.0, "min_margin_pct": None}})])
        self.assertEqual(self._cleared_marker(),
                         {"provider": "fake", "courier_service_id": "svc-1", "bypassed": True})

    def test_a_cheaper_rate_sent_by_the_browser_does_not_dodge_the_gate(self):
        status, body = self._buy(rate=dict(RATE, total_charge=0.01))
        self.assertEqual((status, body["profit"]["label_cost"]), (403, 12.35))

    def test_a_rate_the_server_never_offered_is_gated_as_unknown(self):
        status, body = self._buy(courier_service_id="svc-9")
        self.assertEqual((status, body["profit"]["available"], body["profit"]["profit"]), (403, True, None))
        self.assertIn("not among the rates offered", body["profit"]["reasons"][0])

    def test_draft_rated_before_the_check_existed_is_gated_as_unknown(self):
        self.rows = [row(profit_check=None)]
        status, body = self._buy()
        self.assertEqual((status, body["profit"]["available"]), (403, False))

    def test_above_threshold_buys_and_records_a_clean_pass(self):
        self.settings[profit.SETTING_MIN_AMOUNT] = "-20"
        status, body = self._buy()
        self.assertEqual((status, body["started"], self.audits), (200, True, []))
        self.assertEqual(self._cleared_marker(),
                         {"provider": "fake", "courier_service_id": "svc-1", "bypassed": False})

    def test_manual_shipments_are_never_gated(self):
        self.rows = [row(source="manual", profit_check=None)]
        status, body = self._buy()
        self.assertEqual((status, body["started"], self._cleared_marker()), (200, True, None))

    def test_no_thresholds_means_no_gate(self):
        self.settings[profit.SETTING_MIN_AMOUNT] = ""
        status, body = self._buy()
        self.assertEqual((status, body["started"], self._cleared_marker()), (200, True, None))

    def test_a_resume_of_a_cleared_buy_is_not_asked_again(self):
        check = dict(self.rows[0]["profit_check"],
                     cleared={"provider": "fake", "courier_service_id": "svc-1", "bypassed": True})
        self.rows = [row(status="error", profit_check=check)]
        status, body = self._buy()
        self.assertEqual((status, body["started"], self.audits), (200, True, []))


GID = "gid://shopify/Order/1"
SHIPPABLE = {"shippable": True, "label": None, "reasons": [], "on_hold": False, "fully_paid": True,
             "financial_status": "PAID", "holds": [], "outstanding": 0.0, "currency": "USD"}
HELD = dict(SHIPPABLE, shippable=False, label="On hold", on_hold=True,
            reasons=["Fulfillment is on hold in Shopify: Awaiting payment"])
UNPAID = dict(SHIPPABLE, shippable=False, label="Partially paid", fully_paid=False,
              financial_status="PARTIALLY_PAID", outstanding=12.0,
              reasons=["The order is only partially paid — $12.00 outstanding"])


class OrderGateBuyTest(BuyRouteTest):
    """A Shopify order on hold or not fully paid is refused outright, from
    Shopify's live answer or the rate-time snapshot, before any profit prompt."""

    def setUp(self):
        super().setUp()
        self.settings[profit.SETTING_MIN_AMOUNT] = "-20"   # profit gate passes
        self.rows = [row(shopify_order_id=GID, order_gate=SHIPPABLE)]
        self.lookups = []
        self.live = SHIPPABLE
        self.live_error = None
        self._orig_gate = shopify_client.get_order_gate

        def get_order_gate(store_id, gid):
            self.lookups.append((store_id, gid))
            if self.live_error:
                raise self.live_error
            return self.live
        shopify_client.get_order_gate = get_order_gate

    def tearDown(self):
        shopify_client.get_order_gate = self._orig_gate
        super().tearDown()

    def test_held_order_is_refused_live_before_the_profit_gate(self):
        self.settings[profit.SETTING_MIN_AMOUNT] = "10"   # would also ask for the password
        self.live = HELD
        status, body = self._buy()
        self.assertEqual((status, body, self.lookups), (403, {
            "error": "A label cannot be bought for this order — Fulfillment is on hold in Shopify: Awaiting payment",
            "code": "order_gate", "gate": HELD, "live": True}, [(3, GID)]))
        self.assertEqual((self.audits, self._cleared_marker()), ([("order_gate.refused", {
            "group_id": "g1", "order": "#1001", "provider": "fake", "courier_service_id": "svc-1",
            "live": True, "label": "On hold", "reasons": HELD["reasons"],
            "financial_status": "PAID", "on_hold": True})], None))

    def test_partially_paid_order_is_refused_with_the_payment_reason(self):
        self.live = UNPAID
        status, body = self._buy()
        self.assertEqual((status, body["code"], body["gate"]["label"], body["error"]), (
            403, "order_gate", "Partially paid",
            "A label cannot be bought for this order — The order is only partially paid — $12.00 outstanding"))

    def test_live_shippable_answer_overrides_a_stale_blocked_snapshot(self):
        self.rows = [row(shopify_order_id=GID, order_gate=HELD)]
        status, body = self._buy()
        self.assertEqual((status, body["started"], self.audits), (200, True, []))

    def test_unreachable_shopify_falls_back_to_the_snapshot(self):
        self.rows = [row(shopify_order_id=GID, order_gate=HELD)]
        self.live_error = shopify_client.ShopifyUnavailable("down")
        status, body = self._buy()
        self.assertEqual((status, body["code"], body["live"], self.audits[0][1]["live"]),
                         (403, "order_gate", False, False))

    def test_unreachable_shopify_with_no_snapshot_allows_the_buy(self):
        self.rows = [row(shopify_order_id=GID, order_gate=None)]
        self.live_error = shopify_client.ShopifyUnavailable("down")
        status, body = self._buy()
        self.assertEqual((status, body["started"]), (200, True))

    def test_draft_that_never_loaded_its_order_is_not_checked(self):
        self.rows = [row(shopify_order_id=None, order_gate=None)]
        status, body = self._buy()
        self.assertEqual((status, body["started"], self.lookups), (200, True, []))

    def test_manual_and_backoffice_rows_are_not_checked(self):
        self.live = HELD
        for source in ("manual", "backoffice"):
            with self.subTest(source=source):
                self.rows = [row(source=source, shopify_order_id=None, order_gate=HELD)]
                status, body = self._buy()
                self.assertEqual((status, body["started"], self.lookups), (200, True, []))

    def test_resume_of_an_errored_row_is_checked_again(self):
        self.rows = [row(shopify_order_id=GID, order_gate=SHIPPABLE, status="error")]
        self.live = HELD
        status, body = self._buy()
        self.assertEqual((status, body["code"]), (403, "order_gate"))

    def test_shippable_order_still_meets_the_profit_gate(self):
        self.settings[profit.SETTING_MIN_AMOUNT] = "10"
        status, body = self._buy()
        self.assertEqual((status, body["code"], body["bypass"]), (403, "profit_gate", "required"))


if __name__ == "__main__":
    unittest.main()

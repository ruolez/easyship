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
from providers.base import DraftShipment, ProviderError, Rate  # noqa: E402

STREET = {"address1": "123 Main St", "city": "Chicago", "state": "IL", "zip": "60601"}
UDS_PANEL = {"delivery_date": "2099-01-05", "instructions": "Side door"}


class FakeProvider:
    supports_cod = False
    edits_orders = False

    def __init__(self, name, auto_mode=True, own_options=False):
        self.name = name
        self.label = name.upper()
        self.auto_mode = auto_mode
        self.own_options = own_options
        self.rated_with = []

    def clean_ship_options(self, raw):
        if not self.own_options:
            return {}
        if (raw or {}).get("drop_location") == "Roof":
            raise ValueError("Unknown UDS drop location 'Roof'")
        return dict(raw or {})

    def create_draft_shipments(self, destination, parcels, items, options=None):
        self.rated_with.append(options)
        return [DraftShipment(f"{self.name}-draft")], [Rate(
            provider=self.name, provider_service_id=f"{self.name}-svc", courier_name=f"{self.label} Ground",
            umbrella_name=self.label, total_charge=5.0 if self.name == "uds" else 9.0, currency="USD",
            min_delivery_time=1, max_delivery_time=1, value_for_money_rank=None)], []

    def get_excluded_service_ids(self):
        return set()


class ApiCase(unittest.TestCase):
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
        db = shipments_api.db
        originals = (db.query, db.execute, db.get_setting, providers.enabled_for_user, providers.get_provider,
                     shipments_api.user_sections, shipments_api._auto_buy, shipments_api.audit)
        self.addCleanup(self.restore, originals)
        self.executed = []
        self.audits = []
        db.get_setting = lambda key, default=None: default
        shipments_api.user_sections = lambda user_id, role: set()
        shipments_api.audit = lambda action, detail=None, user_id=None: self.audits.append((action, detail))

    def restore(self, originals):
        db = shipments_api.db
        (db.query, db.execute, db.get_setting, providers.enabled_for_user, providers.get_provider,
         shipments_api.user_sections, shipments_api._auto_buy, shipments_api.audit) = originals


class RatesManualOnlyTest(ApiCase):
    def setUp(self):
        super().setUp()
        self.uds = FakeProvider("uds", auto_mode=False, own_options=True)
        self.ss = FakeProvider("shipstation")
        self.enabled = [self.uds]
        self.auto_bought_with = []
        db = shipments_api.db

        def execute(sql, params=None, returning=False, **kw):
            self.executed.append((sql.strip(), params))
            return {"id": 1} if returning else None
        db.query = lambda sql, params=None, **kw: []
        db.execute = execute
        providers.enabled_for_user = lambda user_id, role: self.enabled
        shipments_api._auto_buy = lambda gid, active, rates, *a: (
            self.auto_bought_with.append([r["provider"] for r in rates]) or {"started": True})

    def rates(self, **extra):
        res = self.client.post("/api/shipments/rates", json={
            "source": "manual", "destination": STREET, "parcels": [{"weight": 2}], "items": [], **extra})
        return res.status_code, json.loads(res.get_data())

    def stored_options(self):
        return [json.loads(p[0]) for sql, p in self.executed if sql.startswith("UPDATE shipments SET options")]

    def test_auto_mode_rates_a_manual_only_platform_but_never_buys(self):
        status, body = self.rates(auto_buy=True, options={"provider": UDS_PANEL})
        self.assertEqual((status, [r["provider"] for r in body["rates"]], body["auto_buy"], self.auto_bought_with),
                         (200, ["uds"], {"started": False, "code": "manual_only"}, []))

    def test_auto_mode_only_considers_the_platforms_that_allow_it(self):
        self.enabled = [self.uds, self.ss]
        status, body = self.rates(auto_buy=True)
        self.assertEqual((status, body["auto_buy"], self.auto_bought_with),
                         (200, {"started": True}, [["shipstation"]]))

    def test_platform_options_are_cleaned_passed_on_and_stored(self):
        self.enabled = [self.uds, self.ss]
        status, body = self.rates(options={"signature": "adult", "provider": UDS_PANEL})
        self.assertEqual((status, self.uds.rated_with, self.ss.rated_with, self.stored_options()), (
            200,
            [{"signature": "adult", "reference": "", "provider": UDS_PANEL}],
            [{"signature": "adult", "reference": ""}],
            [{"signature": "adult", "provider": UDS_PANEL}],
        ))

    def test_invalid_platform_options_refuse_the_rates(self):
        status, body = self.rates(options={"provider": {"drop_location": "Roof"}})
        self.assertEqual((status, body["error"], self.uds.rated_with),
                         (400, "Unknown UDS drop location 'Roof'", []))


SHIPMENT = {
    "id": 5, "group_id": "g1", "box_number": 1, "provider": "uds", "provider_label": "UDS",
    "status": "label_created", "easyship_shipment_id": "777#1", "tracking_number": "UDS1",
    "tracking_numbers": ["UDS1"], "shipping_cost": 7.12, "rate": {"total_charge": 14.25},
    "options": {"signature": "none", "provider": {"instructions": "Side door"}},
    "destination": {**STREET, "contact": "Jane Roe"},
    "tracking_status": "in_transit", "tracking_status_at": None, "delivered_at": None,
}
SECOND_BOX = {**SHIPMENT, "id": 6, "box_number": 2, "easyship_shipment_id": "777#2",
              "tracking_number": "UDS2", "tracking_numbers": ["UDS2"]}
STORED_EVENT = {"tracking_number": "UDS1", "event_id": 11, "status": "in_transit"}


class FakeCarrier:
    name = "uds"
    label = "UDS"
    edits_orders = True

    def __init__(self, order_error=None, update_error=None):
        self.order_error = order_error
        self.update_error = update_error
        self.updates = []
        self.tracked = []

    def supports_tracking(self):
        return True

    def tracking_url(self, number):
        return f"https://track/{number}"

    def order_details(self, psid):
        if self.order_error:
            raise ProviderError(self.order_error)
        return {"order_id": 777, "status": "Assigned"}

    def track(self, numbers):
        self.tracked.append(numbers)
        return [{"tracking_number": "UDS1", "event_id": 12, "status": "delivered"}], ["UDS2: not found"]

    def update_order(self, psid, changes):
        if self.update_error:
            raise self.update_error
        self.updates.append((psid, changes))
        return {"order_id": 777, "status": "Assigned"}


class CarrierRoutesTest(ApiCase):
    def setUp(self):
        super().setUp()
        self.carrier = FakeCarrier()
        self.saved = []
        db = shipments_api.db

        def query(sql, params=None, one=False, **kw):
            if "FROM shipments WHERE id" in sql:
                return dict(SHIPMENT)
            if "FROM shipments WHERE group_id" in sql:
                return [dict(SHIPMENT), dict(SECOND_BOX)]
            if "FROM carrier_tracking_events" in sql:
                return [dict(STORED_EVENT)]
            return None if one else []

        def execute(sql, params=None, returning=False, **kw):
            self.executed.append((sql.strip(), params))
            return None
        db.query = query
        db.execute = execute
        providers.get_provider = lambda key: self.carrier if key == "uds" else None
        orig_save = shipments_api.tracking_store.save
        self.addCleanup(setattr, shipments_api.tracking_store, "save", orig_save)
        shipments_api.tracking_store.save = lambda provider, events: self.saved.append((provider, events))

    def test_tracking_view_has_every_box_the_stored_events_and_the_live_order(self):
        body = json.loads(self.client.get("/api/shipments/5/tracking").get_data())
        self.assertEqual(
            {k: body[k] for k in ("boxes", "events", "order", "order_error", "quoted", "cost", "can_edit_delivery")},
            {"boxes": [{"shipment_id": 5, "box_number": 1, "tracking_number": "UDS1", "url": "https://track/UDS1"},
                       {"shipment_id": 6, "box_number": 2, "tracking_number": "UDS2", "url": "https://track/UDS2"}],
             "events": [{**STORED_EVENT, "event_at": None}], "order": {"order_id": 777, "status": "Assigned"},
             "order_error": None, "quoted": 14.25, "cost": 14.24, "can_edit_delivery": True})

    def test_order_lookup_failure_still_shows_the_events(self):
        self.carrier.order_error = "UDS error (500): down"
        body = json.loads(self.client.get("/api/shipments/5/tracking").get_data())
        self.assertEqual((body["order"], body["order_error"], len(body["events"])),
                         (None, "UDS error (500): down", 1))

    def test_refresh_reads_every_number_stores_the_events_and_reports_misses(self):
        body = json.loads(self.client.post("/api/shipments/5/tracking/refresh").get_data())
        self.assertEqual((self.carrier.tracked, self.saved, body["refresh_errors"]), (
            [["UDS1", "UDS2"]], [("uds", [{"tracking_number": "UDS1", "event_id": 12, "status": "delivered"}])],
            ["UDS2: not found"]))

    def test_delivery_edit_changes_the_carrier_order_then_every_box_snapshot(self):
        res = self.client.patch("/api/shipments/5/delivery",
                                json={"address1": " 9 Elm St ", "instructions": "Ring bell", "ignored": "x"})
        destination = {**SHIPMENT["destination"], "address1": "9 Elm St"}
        options = {"signature": "none", "provider": {"instructions": "Ring bell"}}
        updates = [p for sql, p in self.executed if sql.startswith("UPDATE shipments SET destination")]
        self.assertEqual((res.status_code, self.carrier.updates, updates, self.audits[0][0]), (
            200, [("777#1", {"address1": "9 Elm St", "instructions": "Ring bell"})],
            [(json.dumps(destination), json.dumps(options), 5), (json.dumps(destination), json.dumps(options), 6)],
            "shipment.delivery_updated"))

    def test_a_refused_edit_changes_nothing_here(self):
        self.carrier.update_error = ProviderError("UDS no longer allows changes to order 777", status=409)
        res = self.client.patch("/api/shipments/5/delivery", json={"city": "Oak Park"})
        self.assertEqual((res.status_code, json.loads(res.get_data())["error"], self.executed, self.audits),
                         (409, "UDS no longer allows changes to order 777", [], []))

    def test_blanking_a_required_field_is_refused(self):
        res = self.client.patch("/api/shipments/5/delivery", json={"zip": " "})
        self.assertEqual((res.status_code, json.loads(res.get_data())["error"], self.carrier.updates),
                         (400, "Destination zip is required", []))


if __name__ == "__main__":
    unittest.main()

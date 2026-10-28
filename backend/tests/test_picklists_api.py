import json
import sys
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp", MANIFESTS_DIR="/tmp"))

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import backoffice  # noqa: E402
import picklist_cache  # noqa: E402
import picklists  # noqa: E402
import picklists_api  # noqa: E402
import shipping_estimate  # noqa: E402
from shopify_client import ShopifyError  # noqa: E402

STORE = 1
DB_ID = 4
SETTINGS = {"picklist_backoffice_db_id": str(DB_ID), "picklist_quote_status": "2",
            "picklist_title_prefix": "PL", "picklist_number_tag": "19"}
SYNCED_AT = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)
REP = {"id": 7, "name": "Maria Lopez"}
ITEM = {"ProductID": 1010, "ProductSKU": "AP", "ProductDescription": "APPLE", "ProductUPC": "111"}


def node(gid, name, barcode="111", qty=1, financial="PAID", tags=()):
    return {
        "id": gid, "name": name, "createdAt": "2026-10-20T10:00:00Z", "cancelledAt": None,
        "displayFinancialStatus": financial, "displayFulfillmentStatus": "UNFULFILLED",
        "fulfillmentOrders": {"nodes": [{"status": "OPEN"}]}, "tags": list(tags),
        "customer": {"displayName": "Ann", "tags": []}, "shippingAddress": {"provinceCode": "TX"},
        "totalPriceSet": {"shopMoney": {"amount": "10.00"}}, "risk": {},
        "lineItems": {"nodes": [{"title": "Apple", "sku": "A", "quantity": qty, "unfulfilledQuantity": qty,
                                 "requiresShipping": True, "variant": {"barcode": barcode}}]},
    }


FRAUD_NODE = {
    "id": "g1", "name": "#1", "createdAt": "2026-10-20T10:00:00Z", "tags": [], "currencyCode": "USD",
    "currentSubtotalPriceSet": {"shopMoney": {"amount": "40.00"}},
    "currentShippingPriceSet": {"shopMoney": {"amount": "5.00"}},
    "customer": {"id": "gid://shopify/Customer/5", "displayName": "Ann", "tags": []},
    "shippingAddress": {"provinceCode": "CT"},
    "lineItems": {"pageInfo": {"hasNextPage": False}, "nodes": [{
        "title": "Wrap", "sku": "W", "quantity": 2, "currentQuantity": 2, "unfulfilledQuantity": 2,
        "requiresShipping": True,
        "originalUnitPriceSet": {"shopMoney": {"amount": "20.00"}},
        "discountedUnitPriceAfterAllDiscountsSet": {"shopMoney": {"amount": "20.00"}},
        "variant": {"barcode": "111", "inventoryItem": {
            "unitCost": {"amount": "12.00"}, "measurement": {"weight": {"value": 8, "unit": "OUNCES"}}}}}]},
}


class PicklistsApiTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(picklists_api.bp)
        app.register_blueprint(auth.bp)
        self.client = app.test_client()
        self.calls = []
        self.sections = ["picklists"]
        self.reserved = set()
        self.shopify = {STORE: [node("g1", "#1"), node("g2", "#2", barcode="999"), node("g3", "#3", financial="PENDING")]}

        patches = [
            (picklists_api.db, "get_setting", lambda key, default=None: SETTINGS.get(key, default)),
            (picklists_api.db, "query", self.fake_query),
            (picklists_api.db, "execute", lambda *a, **k: None),
            (picklist_cache, "sync_store", self.fake_sync),
            (picklist_cache, "cached_orders", lambda store_ids: [
                (sid, n) for sid in store_ids for n in self.cache.get(sid, [])]),
            (picklist_cache, "synced_at", lambda store_ids: {
                sid: self.synced[sid] for sid in store_ids if sid in self.synced}),
            (picklists_api, "get_picklist_orders", lambda sid, gids: [n for n in self.shopify[sid] if n["id"] in gids]),
            (picklists, "active_order_keys", lambda store_ids: set(self.reserved)),
            (picklists, "reserve", self.fake_reserve),
            (picklists, "release", lambda pid: self.calls.append(("release", pid))),
            (picklists, "activate", lambda *a: self.calls.append(("activate",) + a)),
            (picklists, "cancel", lambda pid, uid: self.calls.append(("cancel", pid))),
            (picklists, "get_picklist", lambda pid: self.picklist),
            (backoffice, "list_sales_reps", lambda db_id: [REP]),
            (backoffice, "find_items_by_upc", lambda db_id, upcs: {u: ITEM for u in upcs if u == "111"}),
            (backoffice, "known_upcs", lambda db_id, upcs: {u for u in upcs if u == "111"}),
            (backoffice, "create_quotation", self.fake_create_quotation),
            (backoffice, "delete_quotation", self.fake_delete_quotation),
            (picklists_api, "get_order_fraud", lambda sid, gid: self.fraud_node),
            (picklists_api, "list_shop_tags", self.fake_shop_tags),
            (picklists_api, "change_tags", self.fake_change_tags),
            (picklists_api, "hold_order", self.fake_hold_order),
            (picklists_api, "release_holds", self.fake_release_holds),
            (picklist_cache, "put_orders", lambda sid, nodes: self.calls.append(
                ("put_orders", sid, [(n["id"], n["tags"]) for n in nodes]))),
            (picklists, "start_tagging", self.fake_start_tagging),
            (picklists, "tag_rows", lambda pid: [dict(r) for r in self.tag_db.values()]),
            (picklists, "record_tag_results", self.fake_record_tag_results),
            (shipping_estimate, "estimate_shipping", lambda sid, state, weight: {
                "cost": 7.5, "samples": 3, "tolerance_lb": 0.1, "state": state, "weight_lb": weight}),
        ]
        self.fetch = self.fake_list
        self.cache, self.synced, self.syncs = {}, {}, []
        self.fraud_node = FRAUD_NODE
        self.tag_error = None
        self.hold_error = None
        self.tag_fail = {}  # order gid -> Shopify error for that order
        self.tag_db = {}  # picklist_orders.id -> row
        picklists_api._tag_cache.clear()
        self._orig = [(obj, name, getattr(obj, name)) for obj, name, _ in patches]
        for obj, name, value in patches:
            setattr(obj, name, value)
        self.quotation_error = None
        self.delete_error = None
        self.picklist = None
        self.login("user")

    def tearDown(self):
        for obj, name, value in self._orig:
            setattr(obj, name, value)

    def login(self, role):
        with self.client.session_transaction() as sess:
            sess.update({"user_id": 9, "username": "pat", "role": role})

    def fake_query(self, sql, params=None, one=False):
        if "allowed_sections" in sql:
            return {"allowed_sections": self.sections}
        if "FROM shopify_stores" in sql:
            return [{"id": STORE, "name": "Main"}, {"id": 2, "name": "Wholesale"}]
        return [] if not one else None

    def fake_sync(self, store_id, full=False):
        """Reads the store like a whole read would: replaces its cache, or
        raises and leaves the last read in place."""
        self.syncs.append((store_id, full))
        self.cache[store_id] = self.fetch(store_id)
        self.synced[store_id] = SYNCED_AT
        return "full"

    def fake_list(self, store_id):
        if store_id == 2:
            raise ShopifyError("Shopify down")
        return self.shopify[store_id]

    def fake_reserve(self, user_id, db_id, customer, rep, orders, lines):
        taken = [o["name"] for o in orders if (o["store_id"], o["id"]) in self.reserved]
        if taken:
            raise picklists.ReservationConflict(taken)
        self.calls.append(("reserve", [o["name"] for o in orders], [(g["barcode"], g["qty"]) for g in lines]))
        self.reserved_orders = orders
        return 42

    def fake_start_tagging(self, picklist_id, tag):
        self.tag_db = {i: {"id": i, "store_id": o["store_id"], "store_name": "Main", "order_gid": o["id"],
                           "order_name": o["name"], "tag_state": "pending", "tag_error": None}
                       for i, o in enumerate(self.reserved_orders, 1)}

    def fake_record_tag_results(self, results):
        for row_id, state, error in results:
            self.tag_db[row_id].update(tag_state=state, tag_error=error)

    def tag_states(self):
        return [(r["order_name"], r["tag_state"], r["tag_error"]) for r in self.tag_db.values()]

    def fake_create_quotation(self, db_id, **kw):
        if self.quotation_error:
            raise backoffice.BackofficeError(self.quotation_error)
        self.calls.append(("quotation", kw["title"], kw["status"], kw["number_prefix"][-2:],
                           [(item["ProductID"], qty) for item, qty in kw["lines"]]))
        return 555, "1022202619" + "1", "HOUSE ACCOUNT"

    def fake_delete_quotation(self, db_id, qid, number):
        if self.delete_error:
            raise backoffice.BackofficeError(self.delete_error)
        self.calls.append(("delete_quotation", db_id, qid, number))
        return True

    def fake_shop_tags(self, store_id, kind):
        self.calls.append(("shop_tags", store_id, kind))
        return {"order": ["Wholesale", "rush"], "customer": ["VIP"]}[kind]

    def fake_change_tags(self, store_id, gid, add=(), remove=()):
        if self.tag_error or gid in self.tag_fail:
            raise ShopifyError(self.tag_error or self.tag_fail[gid])
        self.calls.append(("change_tags", store_id, gid, list(add), list(remove)))
        for n in self.shopify[store_id]:
            owner = n if n["id"] == gid else n.get("customer") if (n.get("customer") or {}).get("id") == gid else None
            if owner is not None:
                owner["tags"] = [t for t in owner["tags"] if t not in remove] + [t for t in add if t not in owner["tags"]]

    def fake_hold_order(self, store_id, gid, reason, notes=None):
        if self.hold_error:
            raise ShopifyError(self.hold_error)
        self.calls.append(("hold", store_id, gid, reason, notes))
        return [{"fulfillment_order_id": "fo1", "hold_id": "h1"}]

    def fake_release_holds(self, store_id, holds):
        if self.hold_error:
            raise ShopifyError(self.hold_error)
        self.calls.append(("release_holds", store_id, holds))

    def body(self, ids, **extra):
        return {"orders": [{"store_id": STORE, "order_id": gid} for gid in ids], **extra}

    # ----- access -----

    def test_user_without_the_section_is_refused_everywhere(self):
        self.sections = None
        statuses = [self.client.get("/api/picklists/orders?store_ids=1").status_code,
                    self.client.post("/api/picklists/preview", json=self.body(["g1"])).status_code,
                    self.client.get("/api/picklists").status_code]
        self.assertEqual(statuses, [403, 403, 403])

    def test_admin_needs_no_grant(self):
        self.sections = None
        self.login("admin")
        self.assertEqual(self.client.get("/api/picklists/orders?store_ids=").status_code, 200)

    def test_me_reports_the_sections(self):
        self.assertEqual(self.client.get("/api/auth/me").get_json()["sections"], ["picklists"])

    # ----- eligible orders -----

    def test_orders_lists_eligible_ones_with_rules_and_reports_failing_stores(self):
        self.reserved = {(STORE, "g2")}
        rules = '[{"id": "tx", "name": "Texas", "action": "keep", "conditions": [{"field": "ship_state", "op": "in", "value": "TX"}]}]'
        SETTINGS["picklist_rules"] = rules
        try:
            res = self.client.get("/api/picklists/orders?store_ids=1,2").get_json()
        finally:
            del SETTINGS["picklist_rules"]
        self.assertEqual(
            ([(o["name"], o["rule_ids"]) for o in res["orders"]], res["errors"], res["rules"]),
            ([("#1", ["tx"])], [{"store_id": 2, "store_name": "Wholesale", "error": "Shopify down", "synced_at": None}],
             [{"id": "tx", "name": "Texas", "action": "keep"}]))

    def test_stores_load_at_the_same_time(self):
        both_running = threading.Barrier(2, timeout=2)

        def slow_list(store_id):
            both_running.wait()  # breaks (and errors) unless the other store runs concurrently
            return [node(f"g{store_id}0", f"#{store_id}0")]

        self.fetch = slow_list
        self.shopify[3] = []
        res = self.client.get("/api/picklists/orders?store_ids=1,3").get_json()
        self.assertEqual((sorted(o["name"] for o in res["orders"]), res["errors"]), (["#10", "#30"], []))

    def test_unexpected_store_failure_is_reported_not_a_server_error(self):
        def broken(store_id):
            if store_id == 2:
                raise KeyError("orders")
            return self.shopify[store_id]

        self.fetch = broken
        res = self.client.get("/api/picklists/orders?store_ids=1,2")
        self.assertEqual((res.status_code, [e["error"] for e in res.get_json()["errors"]]),
                         (200, ["Unexpected error: 'orders'"]))

    def test_a_store_shopify_cannot_answer_for_keeps_its_last_read_orders(self):
        self.cache[2] = [node("w1", "#W1")]
        self.synced[2] = SYNCED_AT
        res = self.client.get("/api/picklists/orders?store_ids=1,2").get_json()
        self.assertEqual(
            ([o["name"] for o in res["orders"]], res["errors"], res["synced"]),
            (["#1", "#2", "#W1"],
             [{"store_id": 2, "store_name": "Wholesale", "error": "Shopify down", "synced_at": SYNCED_AT.isoformat()}],
             {"1": SYNCED_AT.isoformat(), "2": SYNCED_AT.isoformat()}))

    def test_sync_none_answers_from_the_last_read_without_asking_shopify(self):
        self.cache[STORE] = [node("g9", "#9")]
        res = self.client.get("/api/picklists/orders?store_ids=1,3&sync=none").get_json()
        self.assertEqual(([o["name"] for o in res["orders"]], res["synced"], self.syncs),
                         (["#9"], {"1": None, "3": None}, []))

    def test_sync_full_and_auto_reach_every_store(self):
        self.shopify[3] = []
        self.client.get("/api/picklists/orders?store_ids=1,3&sync=full")
        self.client.get("/api/picklists/orders?store_ids=1,3")
        self.assertEqual(sorted(self.syncs), [(1, False), (1, True), (3, False), (3, True)])

    def test_unknown_sync_mode_is_refused(self):
        self.assertEqual(self.client.get("/api/picklists/orders?store_ids=1&sync=later").status_code, 400)

    # ----- preset coverage -----

    COVERAGE_RULES = [
        {"id": "w", "name": "Wholesale", "action": "keep", "conditions": [
            {"field": "order_tag", "op": "contains", "value": "wholesale"}]},
        {"id": "v", "name": "VIP", "action": "keep", "conditions": [
            {"field": "order_tag", "op": "contains", "value": "vip"}]},
    ]
    COVERAGE_PRESETS = [
        {"name": "Alpha", "groups": [{"join": "and", "items": [{"rule": "w", "negate": False}]}]},
        {"name": "", "groups": [{"join": "and", "items": [{"rule": "w", "negate": False}]}]},
        {"name": "Beta", "groups": [{"join": "and", "items": [{"rule": "v", "negate": False}]}]},
    ]

    def coverage(self, **body):
        return self.client.post("/api/picklists/coverage", json={
            "store_ids": [STORE], "rules": self.COVERAGE_RULES, "presets": self.COVERAGE_PRESETS, **body})

    def test_coverage_says_which_unsaved_presets_each_eligible_order_falls_in(self):
        self.login("admin")
        self.shopify[STORE] = [node("g1", "#1", tags=["Wholesale"]), node("g2", "#2", tags=["wholesale", "VIP"]),
                               node("g3", "#3"), node("g4", "#4", financial="PENDING", tags=["vip"])]
        res = self.coverage().get_json()
        self.assertEqual(
            ([(o["name"], o["rule_ids"], o["presets"]) for o in res["orders"]], res["presets"], self.syncs),
            ([("#1", ["w"], [0]), ("#2", ["w", "v"], [0, 1]), ("#3", [], [])],
             [{"name": "Alpha"}, {"name": "Beta"}], [(STORE, False)]))

    def test_coverage_can_answer_from_the_last_read_alone(self):
        self.login("admin")
        self.cache[STORE] = [node("g9", "#9", tags=["vip"])]
        res = self.coverage(sync="none").get_json()
        self.assertEqual(([(o["name"], o["presets"]) for o in res["orders"]], self.syncs), ([("#9", [1])], []))

    def test_coverage_is_for_admins(self):
        self.assertEqual(self.coverage().status_code, 403)

    def test_orders_tagged_as_being_worked_on_are_not_eligible(self):
        self.shopify[STORE] = [node("g1", "#1"), node("g6", "#6", tags=["Checked"]),
                               node("g7", "#7", tags=["picklist 10202026191"])]
        res = self.client.get("/api/picklists/orders?store_ids=1").get_json()
        self.assertEqual([o["name"] for o in res["orders"]], ["#1"])

    def test_orders_carry_what_would_leave_them_out_and_the_hidden_tags(self):
        self.shopify[STORE].append(node("g4", "#4", barcode=""))
        SETTINGS["picklist_hidden_tags"] = "Shop, Login with Shop"
        try:
            res = self.client.get("/api/picklists/orders?store_ids=1").get_json()
        finally:
            del SETTINGS["picklist_hidden_tags"]
        self.assertEqual(
            ([(o["name"], o["issues"]) for o in res["orders"]], res["hidden_tags"], res["backoffice_error"]),
            ([("#1", []),
              ("#2", [{"kind": "unknown_barcode", "text": "Apple — barcode 999 is not in BackOffice"}]),
              ("#4", [{"kind": "no_barcode", "text": "Apple has no barcode in Shopify"}])],
             ["Shop", "Login with Shop"], None))

    def test_orders_still_load_when_backoffice_cannot_check_the_barcodes(self):
        def down(db_id, upcs):
            raise backoffice.BackofficeError("BackOffice is offline")

        backoffice.known_upcs = down
        self.shopify[STORE].append(node("g4", "#4", barcode=""))
        res = self.client.get("/api/picklists/orders?store_ids=1").get_json()
        self.assertEqual(
            ([(o["name"], [i["kind"] for i in o["issues"]]) for o in res["orders"]], res["backoffice_error"]),
            ([("#1", []), ("#2", []), ("#4", ["no_barcode"])], "BackOffice is offline"))

    def test_orders_say_when_no_backoffice_database_is_chosen(self):
        saved = SETTINGS.pop("picklist_backoffice_db_id")
        try:
            res = self.client.get("/api/picklists/orders?store_ids=1").get_json()
        finally:
            SETTINGS["picklist_backoffice_db_id"] = saved
        self.assertEqual((len(res["orders"]), res["backoffice_error"]),
                         (2, "Choose the BackOffice database for pick lists in Settings → Pick lists"))

    # ----- preview -----

    def test_preview_stops_and_names_the_store_shopify_could_not_answer_for(self):
        def fetch(store_id, gids):
            raise ShopifyError("Shopify unavailable after 3 attempts")

        picklists_api.get_picklist_orders = fetch
        res = self.client.post("/api/picklists/preview", json={"orders": [{"store_id": 2, "order_id": "w1"}]})
        self.assertEqual((res.status_code, res.get_json()["error"]),
                         (502, "Wholesale: Shopify unavailable after 3 attempts"))

    def test_preview_groups_lines_and_explains_every_left_out_order(self):
        self.shopify[STORE].append(node("g4", "#4", qty=2))
        res = self.client.post("/api/picklists/preview", json=self.body(["g1", "g2", "g3", "g4", "gX"])).get_json()
        self.assertEqual((
            [o["name"] for o in res["orders"]],
            sorted((e["name"], e["reasons"][0]) for e in res["excluded"]),
            [(g["barcode"], g["qty"], g["orders"]) for g in res["lines"]],
            res["totals"],
        ), (
            ["#1", "#4"],
            [("#2", "Apple — barcode 999 is not in BackOffice"), ("#3", "Not fully paid"),
             ("gX", "Order not found in Shopify")],
            [("111", 3, ["#1", "#4"])],
            {"orders": 2, "units": 3, "lines": 1},
        ))

    def test_preview_needs_the_backoffice_database_configured(self):
        orig = SETTINGS.pop("picklist_backoffice_db_id")
        try:
            res = self.client.post("/api/picklists/preview", json=self.body(["g1"]))
        finally:
            SETTINGS["picklist_backoffice_db_id"] = orig
        self.assertEqual(res.status_code, 400)

    # ----- create -----

    def test_create_reserves_then_writes_the_quotation_then_activates(self):
        res = self.client.post("/api/picklists", json=self.body(["g1", "g2"], customer_id=3, sales_rep_id=7))
        self.assertEqual((res.status_code, res.get_json()["quotation_number"], self.calls), (200, "10222026191", [
            ("reserve", ["#1"], [("111", 1)]),
            ("quotation", "PL 42", 2, "19", [(1010, 1)]),
            ("activate", 42, 555, "10222026191", "HOUSE ACCOUNT"),
            ("change_tags", STORE, "g1", ["picklist 10222026191"], []),
        ]))

    def test_order_put_on_hold_after_the_preview_is_left_off_the_created_list(self):
        self.shopify[STORE] = [node("g1", "#1"), node("g5", "#5")]
        preview = self.client.post("/api/picklists/preview", json=self.body(["g1", "g5"])).get_json()
        self.shopify[STORE][1]["fulfillmentOrders"] = {"nodes": [{"status": "ON_HOLD"}]}
        res = self.client.post("/api/picklists", json=self.body(["g1", "g5"], customer_id=3, sales_rep_id=7))
        data = res.get_json()
        self.assertEqual(
            (preview["totals"]["orders"], res.status_code, data["totals"]["orders"],
             [(e["name"], e["reasons"]) for e in data["excluded"]], self.calls[0]),
            (2, 200, 1, [("#5", ["On hold in Shopify"])], ("reserve", ["#1"], [("111", 1)])))

    def test_create_refuses_when_every_order_went_on_hold(self):
        self.shopify[STORE][0]["fulfillmentOrders"] = {"nodes": [{"status": "ON_HOLD"}]}
        res = self.client.post("/api/picklists", json=self.body(["g1"], customer_id=3, sales_rep_id=7))
        self.assertEqual((res.status_code, [(e["name"], e["reasons"]) for e in res.get_json()["excluded"]], self.calls),
                         (409, [("#1", ["On hold in Shopify"])], []))

    def test_create_refuses_orders_taken_in_the_meantime(self):
        self.reserved = set()
        orig = picklists.active_order_keys
        picklists.active_order_keys = lambda ids: set()  # still free when built…
        self.reserved = {(STORE, "g1")}  # …but taken by the time it is reserved
        try:
            res = self.client.post("/api/picklists", json=self.body(["g1"], customer_id=3, sales_rep_id=7))
        finally:
            picklists.active_order_keys = orig
        self.assertEqual((res.status_code, res.get_json()["orders"], self.calls), (409, ["#1"], []))

    def test_failed_quotation_releases_the_orders(self):
        self.quotation_error = "BackOffice connection failed"
        res = self.client.post("/api/picklists", json=self.body(["g1"], customer_id=3, sales_rep_id=7))
        self.assertEqual((res.status_code, [c[0] for c in self.calls]), (502, ["reserve", "release"]))

    def test_unknown_sales_rep_is_refused(self):
        res = self.client.post("/api/picklists", json=self.body(["g1"], customer_id=3, sales_rep_id=99))
        self.assertEqual((res.status_code, self.calls), (400, []))

    # ----- cancel -----

    def active_picklist(self, **over):
        return {"id": 42, "status": "active", "backoffice_db_id": DB_ID, "quotation_id": 555,
                "quotation_number": "10222026191", "created_at": datetime.now(timezone.utc), **over}

    def test_cancel_deletes_the_quotation_before_freeing_the_orders(self):
        self.picklist = self.active_picklist()
        res = self.client.post("/api/picklists/42/cancel")
        self.assertEqual((res.status_code, self.calls), (200, [
            ("delete_quotation", DB_ID, 555, "10222026191"), ("cancel", 42)]))

    def test_cancel_changes_nothing_when_backoffice_fails(self):
        self.picklist = self.active_picklist()
        self.delete_error = "BackOffice connection failed"
        res = self.client.post("/api/picklists/42/cancel")
        self.assertEqual((res.status_code, self.calls), (502, []))

    def test_pick_list_still_being_created_cannot_be_cancelled_yet(self):
        self.picklist = self.active_picklist(status="creating", quotation_id=None, quotation_number=None)
        fresh = self.client.post("/api/picklists/42/cancel").status_code
        self.picklist["created_at"] -= timedelta(minutes=10)
        stale = self.client.post("/api/picklists/42/cancel").status_code
        self.assertEqual((fresh, stale, self.calls), (409, 200, [("cancel", 42)]))


    # ----- order window -----

    def test_order_window_carries_the_profit_and_the_customer_id(self):
        res = self.client.get("/api/picklists/orders/risk?store_id=1&order_id=g1")
        data = res.get_json()
        self.assertEqual(
            (data["customer"]["id"],
             {k: data["profit"][k] for k in ("revenue", "items_cost", "includes_shipping", "profit", "margin_pct")},
             data["profit"]["shipping_estimate"]),
            ("gid://shopify/Customer/5",
             {"revenue": 45.0, "items_cost": 24.0, "includes_shipping": True, "profit": 13.5, "margin_pct": 30.0},
             {"cost": 7.5, "samples": 3, "tolerance_lb": 0.1, "state": "CT", "weight_lb": 1.0}))

    # ----- tags -----

    def test_tag_suggestions_are_read_once_per_store_and_kind(self):
        got = [self.client.get(f"/api/picklists/tags?store_id=1&kind={kind}").get_json()["tags"]
               for kind in ("order", "order", "customer")]
        self.assertEqual((got, [c for c in self.calls if c[0] == "shop_tags"]),
                         ([["Wholesale", "rush"], ["Wholesale", "rush"], ["VIP"]],
                          [("shop_tags", 1, "order"), ("shop_tags", 1, "customer")]))

    def test_unknown_tag_kind_is_refused(self):
        self.assertEqual(self.client.get("/api/picklists/tags?store_id=1&kind=product").status_code, 400)

    def tag_change(self, **body):
        res = self.client.post("/api/picklists/orders/tags",
                               json={"store_id": STORE, "order_id": "g1", "kind": "order", **body})
        return res.status_code, res.get_json()

    def test_order_tags_change_in_shopify_and_the_order_comes_back_with_its_rules(self):
        self.shopify[STORE][0]["tags"] = ["old", "Keep"]
        status, data = self.tag_change(add=["rush, Gift ", "keep"], remove=["OLD"])
        self.assertEqual(
            (status, data["order"]["tags"], data["order"]["rule_ids"], self.calls[-2]),
            (200, ["Keep", "rush", "Gift"], [], ("change_tags", STORE, "g1", ["rush", "Gift"], ["old"])))

    def test_customer_tags_go_to_the_orders_customer(self):
        self.shopify[STORE][0]["customer"] = {"id": "gid://shopify/Customer/5", "displayName": "Ann", "tags": ["vip"]}
        status, data = self.tag_change(kind="customer", add=["wholesale"])
        self.assertEqual((status, data["order"]["customer_tags"], self.calls[-2]),
                         (200, ["vip", "wholesale"], ("change_tags", STORE, "gid://shopify/Customer/5", ["wholesale"], [])))

    def test_new_tags_join_the_suggestions(self):
        self.client.get("/api/picklists/tags?store_id=1&kind=order")
        self.tag_change(add=["fragile"])
        tags = self.client.get("/api/picklists/tags?store_id=1&kind=order").get_json()["tags"]
        self.assertEqual(tags, ["fragile", "Wholesale", "rush"])

    def test_tag_change_refusals(self):
        self.shopify[STORE][0]["customer"] = None
        cases = [({"add": []}, 400), ({"add": ["x"], "kind": "product"}, 400),
                 ({"add": ["x"], "order_id": "gone"}, 404), ({"add": ["x"], "kind": "customer"}, 400)]
        self.assertEqual([self.tag_change(**body)[0] for body, _ in cases], [code for _, code in cases])
        self.assertEqual([c for c in self.calls if c[0] == "change_tags"], [])

    def test_missing_write_scope_is_explained(self):
        self.tag_error = "Shopify GraphQL error: [{'extensions': {'code': 'ACCESS_DENIED'}}]"
        status, data = self.tag_change(add=["x"])
        self.assertEqual((status, data["error"]),
                         (502, "This store's Shopify token can't change order tags. Add the write_orders and "
                               "write_customers scopes to the store's app, then try again."))


    def test_tag_change_saves_the_order_to_the_cache_as_shopify_now_has_it(self):
        self.tag_change(add=["rush"])
        self.assertEqual(self.calls[-1], ("put_orders", STORE, [("g1", ["rush"])]))

    # ----- holds -----

    def hold(self, path="/api/picklists/orders/hold", **body):
        res = self.client.post(path, json={"store_id": STORE, "order_id": "g1", **body})
        return res.status_code, res.get_json()

    def test_hold_puts_the_order_on_hold_and_caches_it_as_shopify_now_has_it(self):
        status, data = self.hold(reason="INCORRECT_ADDRESS", notes="  Apt missing ")
        self.assertEqual((status, data, self.calls), (
            200, {"holds": [{"fulfillment_order_id": "fo1", "hold_id": "h1"}]},
            [("hold", STORE, "g1", "INCORRECT_ADDRESS", "Apt missing"), ("put_orders", STORE, [("g1", [])])]))

    def test_hold_refusals(self):
        cases = [({"reason": "BORED"}, 400), ({"reason": "OTHER", "order_id": ""}, 400),
                 ({"reason": "OTHER", "store_id": "1"}, 400)]
        self.assertEqual([self.hold(**body)[0] for body, _ in cases], [code for _, code in cases])
        self.assertEqual(self.calls, [])

    def test_missing_fulfillment_scope_is_explained(self):
        self.hold_error = "Shopify GraphQL error: [{'extensions': {'code': 'ACCESS_DENIED'}}]"
        status, data = self.hold(reason="OTHER")
        self.assertEqual((status, data["error"]), (
            502, "This store's Shopify token can't put orders on hold. Add the "
                 "write_merchant_managed_fulfillment_orders scope to the store's app, then try again."))

    def test_a_hold_still_counts_when_the_order_cannot_be_read_back(self):
        picklists_api.get_picklist_orders = lambda sid, gids: (_ for _ in ()).throw(ShopifyError("down"))
        status, data = self.hold(reason="OTHER")
        self.assertEqual((status, [c[0] for c in self.calls]), (200, ["hold"]))

    def test_release_lets_go_of_the_holds_given_and_recaches_the_order(self):
        holds = [{"fulfillment_order_id": "fo1", "hold_id": "h1"}]
        status, _ = self.hold("/api/picklists/orders/hold/release", holds=holds)
        self.assertEqual((status, self.calls),
                         (200, [("release_holds", STORE, holds), ("put_orders", STORE, [("g1", [])])]))

    def test_release_needs_the_holds(self):
        cases = [{}, {"holds": []}, {"holds": [{"hold_id": "h1"}]}]
        self.assertEqual([self.hold("/api/picklists/orders/hold/release", **body)[0] for body in cases], [400] * 3)

    # ----- bulk tags -----

    def bulk_tag(self, ids, **body):
        res = self.client.post("/api/picklists/orders/tags/bulk",
                               json={"orders": [{"store_id": STORE, "order_id": g} for g in ids], **body})
        return res.status_code, res.get_json()

    def test_bulk_tags_every_order_and_sends_them_back_with_their_rules(self):
        status, data = self.bulk_tag(["g1", "g2"], add=["rush, Gift"])
        self.assertEqual(
            (status, sorted(c for c in self.calls if c[0] == "change_tags"),
             [(o["id"], o["rule_ids"]) for o in data["orders"]], data["failed"], data["tags"]),
            (200, [("change_tags", STORE, "g1", ["rush", "Gift"], []), ("change_tags", STORE, "g2", ["rush", "Gift"], [])],
             [("g1", []), ("g2", [])], [], ["rush", "Gift"]))

    def test_an_order_shopify_refuses_is_reported_and_the_rest_still_get_tagged(self):
        self.tag_fail = {"g2": "Order is archived"}
        status, data = self.bulk_tag(["g1", "g2"], add=["rush"])
        self.assertEqual((status, [o["id"] for o in data["orders"]], data["failed"]),
                         (200, ["g1"], [{"store_id": STORE, "order_id": "g2", "error": "Order is archived"}]))

    def test_bulk_tags_are_cached_and_join_the_suggestions(self):
        self.client.get("/api/picklists/tags?store_id=1&kind=order")
        self.bulk_tag(["g1"], add=["fragile"])
        tags = self.client.get("/api/picklists/tags?store_id=1&kind=order").get_json()["tags"]
        self.assertEqual(([c for c in self.calls if c[0] == "put_orders"], tags),
                         ([("put_orders", STORE, [("g1", ["fragile"])])], ["fragile", "Wholesale", "rush"]))

    def test_a_tag_that_marks_the_order_as_worked_on_says_it_can_no_longer_be_picked(self):
        _, data = self.bulk_tag(["g1"], add=["Checked"])
        self.assertEqual([o["ineligible_reason"] for o in data["orders"]], ["Tagged “Checked” — already being worked on"])

    def test_bulk_tag_refusals(self):
        too_many = [f"g{i}" for i in range(picklists_api.BULK_TAG_LIMIT + 1)]
        cases = [(["g1"], {"add": [" , "]}), ([], {"add": ["x"]}), (too_many, {"add": ["x"]})]
        self.assertEqual([self.bulk_tag(ids, **body)[0] for ids, body in cases], [400] * 3)
        self.assertEqual(self.calls, [])

    # ----- Shopify tag "picklist <quotation number>" -----

    def create(self, ids):
        res = self.client.post("/api/picklists", json=self.body(ids, customer_id=3, sales_rep_id=7))
        return res.status_code, res.get_json()

    def test_create_tags_every_order_and_says_so(self):
        self.shopify[STORE].append(node("g4", "#4"))
        status, data = self.create(["g1", "g4"])
        self.assertEqual((status, data["tagging"], self.tag_states()), (200,
            {"tag": "picklist 10222026191", "mode": "add", "done": 2, "failed": [], "error": None},
            [("#1", "tagged", None), ("#4", "tagged", None)]))

    def test_an_order_shopify_refuses_is_reported_and_the_rest_still_get_tagged(self):
        self.shopify[STORE].append(node("g4", "#4"))
        self.tag_fail = {"g1": "Order does not exist"}
        status, data = self.create(["g1", "g4"])
        self.assertEqual((status, data["quotation_number"], data["tagging"]["done"], data["tagging"]["failed"],
                          self.tag_states()), (200, "10222026191", 1,
            [{"order_name": "#1", "store_name": "Main", "error": "Order does not exist"}],
            [("#1", "failed", "Order does not exist"), ("#4", "tagged", None)]))

    def test_missing_write_scope_is_explained_on_every_order(self):
        self.tag_error = "Shopify GraphQL error: [{'extensions': {'code': 'ACCESS_DENIED'}}]"
        _, data = self.create(["g1"])
        self.assertEqual(data["tagging"]["failed"][0]["error"],
                         "This store's Shopify token can't change order tags. Add the write_orders and "
                         "write_customers scopes to the store's app, then try again.")

    def test_tagging_that_breaks_down_never_undoes_the_pick_list(self):
        def boom(picklist_id, tag):
            raise RuntimeError("database went away")
        picklists.start_tagging = boom
        status, data = self.create(["g1"])
        self.assertEqual((status, data["id"], data["tagging"]["error"], [c[0] for c in self.calls]),
                         (200, 42, "Could not tag the orders: database went away", ["reserve", "quotation", "activate"]))

    def test_retry_adds_the_tag_only_where_it_is_missing(self):
        self.shopify[STORE].append(node("g4", "#4"))
        self.tag_fail = {"g1": "Shopify unavailable"}
        self.create(["g1", "g4"])
        self.tag_fail = {}
        self.picklist = self.active_picklist(shopify_tag="picklist 10222026191")
        before = len(self.calls)
        res = self.client.post("/api/picklists/42/tags/retry").get_json()
        self.assertEqual((res["done"], res["failed"], self.calls[before:], self.tag_states()), (1, [],
            [("change_tags", STORE, "g1", ["picklist 10222026191"], [])],
            [("#1", "tagged", None), ("#4", "tagged", None)]))

    def test_cancel_removes_the_tag_after_the_quotation_is_gone(self):
        self.create(["g1"])
        self.picklist = self.active_picklist(shopify_tag="picklist 10222026191")
        before = len(self.calls)
        res = self.client.post("/api/picklists/42/cancel").get_json()
        self.assertEqual((res["tagging"]["done"], self.calls[before:], self.tag_states()), (1, [
            ("delete_quotation", DB_ID, 555, "10222026191"), ("cancel", 42),
            ("change_tags", STORE, "g1", [], ["picklist 10222026191"])], [("#1", "removed", None)]))

    def test_a_tag_that_cannot_be_removed_leaves_the_cancel_done_and_can_be_retried(self):
        self.create(["g1"])
        self.picklist = self.active_picklist(shopify_tag="picklist 10222026191")
        self.tag_fail = {"g1": "Shopify unavailable"}
        res = self.client.post("/api/picklists/42/cancel")
        cancelled = (res.status_code, res.get_json()["tagging"]["failed"][0]["error"], self.tag_states())
        self.tag_fail = {}
        self.picklist = self.active_picklist(status="cancelled", shopify_tag="picklist 10222026191")
        retried = self.client.post("/api/picklists/42/tags/retry").get_json()["done"]
        self.assertEqual((cancelled, retried, self.tag_states()), (
            (200, "Shopify unavailable", [("#1", "remove_failed", "Shopify unavailable")]), 1, [("#1", "removed", None)]))

    def test_pick_lists_from_before_tagging_cancel_without_touching_shopify(self):
        self.picklist = self.active_picklist(shopify_tag=None)
        res = self.client.post("/api/picklists/42/cancel").get_json()
        self.assertEqual((res["tagging"], [c[0] for c in self.calls]), (None, ["delete_quotation", "cancel"]))

    def test_retry_refusals(self):
        self.picklist = None
        missing = self.client.post("/api/picklists/42/tags/retry").status_code
        self.picklist = self.active_picklist(shopify_tag=None)
        untagged = self.client.post("/api/picklists/42/tags/retry").status_code
        self.picklist = self.active_picklist(status="creating", shopify_tag="picklist 1")
        creating = self.client.post("/api/picklists/42/tags/retry").status_code
        self.assertEqual((missing, untagged, creating), (404, 400, 409))


    def test_orders_carry_the_presets_of_the_rules_in_place(self):
        rule = {"id": "big", "name": "Big", "action": "keep", "match": "all",
                "conditions": [{"field": "order_total", "op": "at_least", "value": 5}]}
        good = {"id": "p1", "name": "Retail", "join": "and",
                "groups": [{"join": "or", "items": [{"rule": "big", "negate": True}]}]}
        stale = {**good, "id": "p2", "name": "Stale", "groups": [{"join": "and", "items": [{"rule": "gone"}]}]}
        settings = {**SETTINGS, "picklist_rules": json.dumps([rule]), "picklist_presets": json.dumps([good, stale])}
        picklists_api.db.get_setting = lambda key, default=None: settings.get(key, default)
        data = self.client.get("/api/picklists/orders?store_ids=1").get_json()
        self.assertEqual((data["presets"], data["orders"][0]["rule_ids"]), ([good], ["big"]))


if __name__ == "__main__":
    unittest.main()

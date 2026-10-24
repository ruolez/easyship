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
import picklists  # noqa: E402
import picklists_api  # noqa: E402
import shipping_estimate  # noqa: E402
from shopify_client import ShopifyError  # noqa: E402

STORE = 1
DB_ID = 4
SETTINGS = {"picklist_backoffice_db_id": str(DB_ID), "picklist_quote_status": "2",
            "picklist_title_prefix": "PL", "picklist_number_tag": "19"}
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
            (picklists_api, "list_picklist_orders", self.fake_list),
            (picklists_api, "get_picklist_orders", lambda sid, gids: [n for n in self.shopify[sid] if n["id"] in gids]),
            (picklists, "active_order_keys", lambda store_ids: set(self.reserved)),
            (picklists, "reserve", self.fake_reserve),
            (picklists, "release", lambda pid: self.calls.append(("release", pid))),
            (picklists, "activate", lambda *a: self.calls.append(("activate",) + a)),
            (picklists, "cancel", lambda pid, uid: self.calls.append(("cancel", pid))),
            (picklists, "get_picklist", lambda pid: self.picklist),
            (backoffice, "list_sales_reps", lambda db_id: [REP]),
            (backoffice, "find_items_by_upc", lambda db_id, upcs: {u: ITEM for u in upcs if u == "111"}),
            (backoffice, "create_quotation", self.fake_create_quotation),
            (backoffice, "delete_quotation", self.fake_delete_quotation),
            (picklists_api, "get_order_fraud", lambda sid, gid: self.fraud_node),
            (picklists_api, "list_shop_tags", self.fake_shop_tags),
            (picklists_api, "change_tags", self.fake_change_tags),
            (shipping_estimate, "estimate_shipping", lambda sid, state, weight: {
                "cost": 7.5, "samples": 3, "tolerance_lb": 0.1, "state": state, "weight_lb": weight}),
        ]
        self.fraud_node = FRAUD_NODE
        self.tag_error = None
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

    def fake_list(self, store_id):
        if store_id == 2:
            raise ShopifyError("Shopify down")
        return self.shopify[store_id]

    def fake_reserve(self, user_id, db_id, customer, rep, orders, lines):
        taken = [o["name"] for o in orders if (o["store_id"], o["id"]) in self.reserved]
        if taken:
            raise picklists.ReservationConflict(taken)
        self.calls.append(("reserve", [o["name"] for o in orders], [(g["barcode"], g["qty"]) for g in lines]))
        return 42

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
        if self.tag_error:
            raise ShopifyError(self.tag_error)
        self.calls.append(("change_tags", store_id, gid, list(add), list(remove)))

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
            ([("#1", ["tx"])], [{"store_id": 2, "store_name": "Wholesale", "error": "Shopify down"}],
             [{"id": "tx", "name": "Texas", "action": "keep"}]))

    def test_stores_load_at_the_same_time(self):
        both_running = threading.Barrier(2, timeout=2)

        def slow_list(store_id):
            both_running.wait()  # breaks (and errors) unless the other store runs concurrently
            return [node(f"g{store_id}0", f"#{store_id}0")]

        picklists_api.list_picklist_orders = slow_list
        self.shopify[3] = []
        res = self.client.get("/api/picklists/orders?store_ids=1,3").get_json()
        self.assertEqual((sorted(o["name"] for o in res["orders"]), res["errors"]), (["#10", "#30"], []))

    def test_unexpected_store_failure_is_reported_not_a_server_error(self):
        def broken(store_id):
            if store_id == 2:
                raise KeyError("orders")
            return self.shopify[store_id]

        picklists_api.list_picklist_orders = broken
        res = self.client.get("/api/picklists/orders?store_ids=1,2")
        self.assertEqual((res.status_code, [e["error"] for e in res.get_json()["errors"]]),
                         (200, ["Unexpected error: 'orders'"]))

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
        ]))

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
            (status, data["order"]["tags"], data["order"]["rule_ids"], self.calls[-1]),
            (200, ["Keep", "rush", "Gift"], [], ("change_tags", STORE, "g1", ["rush", "Gift"], ["old"])))

    def test_customer_tags_go_to_the_orders_customer(self):
        self.shopify[STORE][0]["customer"] = {"id": "gid://shopify/Customer/5", "displayName": "Ann", "tags": ["vip"]}
        status, data = self.tag_change(kind="customer", add=["wholesale"])
        self.assertEqual((status, data["order"]["customer_tags"], self.calls[-1]),
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


if __name__ == "__main__":
    unittest.main()

import sys
import types
import unittest
from datetime import datetime, timezone

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp", MANIFESTS_DIR="/tmp"))

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import dbadmin  # noqa: E402
import shopify_client  # noqa: E402
import verify_api  # noqa: E402
import verify_store  # noqa: E402

APPLE = "012345678905"
STARTED = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
STORES = [{"id": 3, "name": "TSW", "prefix": "TS"}]


def shopify_node(name="#TS1001", qty=2, cancelled=None):
    return {"id": "gid://shopify/Order/9", "name": name, "cancelledAt": cancelled,
            "customer": {"displayName": "Ann"},
            "lineItems": {"nodes": [{"title": "Apple", "variant": {"barcode": APPLE}, "requiresShipping": True,
                                     "unfulfilledQuantity": qty, "currentQuantity": qty,
                                     "refundableQuantity": qty}]}}


class VerifyApiTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(verify_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess.update({"user_id": 5, "role": "user"})
        self.sections = ["verify"]
        self.opened = []
        self.line_changes = []
        self.audits = []
        self.history_filters = []
        self.node = shopify_node()
        self.quotation_status = {"QuotationNumber": "10282026191", "SourceDB": "PrimeWholesale", "Dop1": "881",
                                 "BusinessName": "Corner Shop"}
        self.quotation_rows = [{"ProductUPC": APPLE, "ProductDescription": "Apple", "ProductSKU": "A", "Qty": 3.0},
                               {"ProductUPC": "", "ProductDescription": "Shipping", "ProductSKU": "", "Qty": 1.0}]
        self.read_args = []
        self.verification = {
            "id": 1, "order_name": "#TS1001", "source": "shopify", "shopify_store_id": 3, "shopify_order_id": "gid://shopify/Order/9", "source_db": None,
            "customer": "Ann", "status": "in_progress", "forced": False, "started_at": STARTED,
            "started_by": 5, "verified_by": None, "started_by_name": "pat", "verified_at": None,
            "verified_by_name": None, "shopify_tag_state": None, "shopify_tag_error": None,
            "dbadmin_state": None, "dbadmin_error": None, "lines": [],
        }
        self.settings = {"verify_skip_prefixes_backoffice": "shipping"}

        def fake_query(sql, params=None, one=False):
            if "shopify_stores WHERE is_active" in sql:
                return STORES
            if "FROM shopify_stores" in sql:
                return {"name": "TSW"}
            return [] if not one else None

        patches = [
            (verify_api, "user_sections", lambda user_id, role: self.sections),
            (auth, "user_sections", lambda user_id, role: self.sections),
            (verify_api.db, "query", fake_query),
            (verify_api.db, "get_setting", lambda key, default=None: self.settings.get(key, default)),
            (verify_api, "audit", lambda action, detail=None: self.audits.append(action)),
            (verify_api.shopify_client, "resolve_order", lambda sid, n: {"id": "gid://shopify/Order/9"}),
            (verify_api.shopify_client, "get_picklist_orders", self._nodes),
            (verify_api.dbadmin, "find_quotation", lambda n: self.quotation_status),
            (verify_api.dbadmin, "read_quotation", self._read_quotation),
            (verify_api.verify_store, "live_by_key", lambda key: None),
            (verify_api.verify_store, "open_order", self._open),
            (verify_api.verify_store, "get", lambda vid: self.verification),
            (verify_api.verify_store, "set_line_qty", self._set_line_qty),
            (verify_api.verify_store, "list_cases", lambda: []),
            (verify_api.verify_store, "history", lambda f: self.history_filters.append(f) or []),
        ]
        self._orig = [(obj, name, getattr(obj, name)) for obj, name, _ in patches]
        for obj, name, fn in patches:
            setattr(obj, name, fn)

    def tearDown(self):
        for obj, name, fn in self._orig:
            setattr(obj, name, fn)

    def _nodes(self, store_id, gids):
        if isinstance(self.node, Exception):
            raise self.node
        return [self.node]

    def _read_quotation(self, nick, quotation_id, number):
        self.read_args.append((nick, quotation_id, number))
        return {"QuotationNumber": "10282026191 ", "BusinessName": "Corner Shop"}, self.quotation_rows

    def _open(self, order, lines, user_id):
        self.opened.append((order, [(l["key"], l["qty_required"], l["skip_reason"] is not None) for l in lines]))
        return 1, {"changes": [], "reopened": False}

    def _set_line_qty(self, vid, line_id, kind, user_id, qty=None, client_id=None):
        self.line_changes.append((kind, qty))
        return False

    def open(self, number):
        return self.client.post("/api/verify/open", json={"number": number})

    def test_shopify_prefix_opens_the_shopify_order(self):
        res = self.open("ts1001")
        order, lines = self.opened[0]
        self.assertEqual((res.status_code, order["order_key"], order["shopify_order_id"], order["customer"], lines),
                         (200, "shopify:3:TS1001", "gid://shopify/Order/9", "Ann", [("12345678905", 2, False)]))

    def test_other_numbers_go_through_db_admin(self):
        res = self.open("10282026191")
        order, lines = self.opened[0]
        self.assertEqual(
            (res.status_code, self.read_args, order["order_key"], order["source_db"], order["quotation_id"], lines),
            (200, [("PrimeWholesale", 881, "10282026191")], "backoffice:10282026191", "PrimeWholesale", 881,
             [("12345678905", 3, False), ("skip:SHIPPING", 1, True)]))

    def test_quotation_missing_from_db_admin_is_404(self):
        self.quotation_status = None
        res = self.open("999")
        self.assertEqual((res.status_code, self.opened), (404, []))

    def test_db_admin_down_is_502_with_its_message(self):
        def down(number):
            raise dbadmin.DbAdminError("DB_Admin connection failed: timeout")
        dbadmin_find, verify_api.dbadmin.find_quotation = verify_api.dbadmin.find_quotation, down
        try:
            res = self.open("999")
        finally:
            verify_api.dbadmin.find_quotation = dbadmin_find
        self.assertEqual((res.status_code, res.get_json()["error"]), (502, "DB_Admin connection failed: timeout"))

    def test_shopify_down_is_502(self):
        self.node = shopify_client.ShopifyUnavailable("Shopify unavailable after 3 attempts")
        self.assertEqual(self.open("TS1001").status_code, 502)

    def test_cancelled_and_empty_orders_are_refused(self):
        self.node = shopify_node(cancelled="2026-10-28T10:00:00Z")
        cancelled = self.open("TS1001")
        self.node = shopify_node(qty=0)
        empty = self.open("TS1001")
        self.assertEqual((cancelled.status_code, empty.status_code, self.opened), (409, 409, []))

    def test_without_the_verify_section_nothing_opens(self):
        self.sections = []
        self.assertEqual((self.open("TS1001").status_code, self.opened), (403, []))

    def test_typed_count_accepts_digit_strings(self):
        self.client.post("/api/verify/1/lines/7/count", json={"qty": " 12 "})
        self.client.post("/api/verify/1/lines/7/count", json={"qty": 4})
        self.assertEqual(self.line_changes, [("count", 12), ("count", 4)])

    def test_force_needs_the_override_permission(self):
        denied = self.client.post("/api/verify/1/lines/7/force")
        self.sections = ["verify", "verify_override"]
        allowed = self.client.post("/api/verify/1/lines/7/force")
        self.assertEqual((denied.status_code, allowed.status_code, self.line_changes),
                         (403, 200, [("force_line", None)]))

    def test_packers_see_only_their_own_day(self):
        self.client.get("/api/verify/history?user_id=9&from=2026-01-01")
        f = self.history_filters[0]
        self.assertEqual((f["user_id"], f["since"].date() >= datetime(2026, 10, 1).date()), (5, True))

    def test_override_users_filter_freely(self):
        self.sections = ["verify", "verify_override"]
        self.client.get("/api/verify/history?user_id=9&from=2026-01-01&to=2026-01-31&status=bogus")
        f = self.history_filters[0]
        self.assertEqual((f["user_id"], f["since"].isoformat(), f["until"].isoformat(), f["status"]),
                         (9, "2026-01-01T00:00:00-06:00", "2026-02-01T00:00:00-06:00", None))

    def test_store_error_becomes_its_status(self):
        def refuse(*a, **k):
            raise verify_store.VerifyError("Scan one unit of this item first")
        verify_api.verify_store.set_line_qty = refuse
        res = self.client.post("/api/verify/1/lines/7/check-all")
        self.assertEqual((res.status_code, res.get_json()["error"]), (400, "Scan one unit of this item first"))


class SourceConfigTest(unittest.TestCase):
    def test_port_given_connects_by_host_and_port(self):
        cfg = dbadmin.source_config({"ipAddress": "10.0.0.1 ", "ShareName": "SQL2", "Port": 2209,
                                     "NameDB": "DB1", "Username": "u", "Password": "p"})
        self.assertEqual(cfg, {"server": "10.0.0.1", "port": 2209, "database": "DB1", "user": "u", "password": "p"})

    def test_no_port_uses_the_named_instance(self):
        cfg = dbadmin.source_config({"ipAddress": "10.0.0.1", "ShareName": "SQL2", "Port": "",
                                     "NameDB": "DB1", "Username": "u", "Password": None})
        self.assertEqual((cfg["server"], cfg["port"], cfg["password"]), ("10.0.0.1\\SQL2", 1433, ""))


if __name__ == "__main__":
    unittest.main()

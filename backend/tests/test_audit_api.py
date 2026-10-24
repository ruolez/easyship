import json
import sys
import types
import unittest
from datetime import datetime, timezone

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp",
    MANIFESTS_DIR="/tmp"))

from flask import Flask  # noqa: E402

import settings_api  # noqa: E402

WHEN = datetime(2026, 10, 2, 18, 30, tzinfo=timezone.utc)  # 01:30 PM Central
ROW = {"id": 9, "action": "profit.bypass", "detail": {"order": "#1001", "profit": -7.35},
       "created_at": WHEN, "username": "eugene"}
GROUPS = [{"value": "label"}, {"value": "profit"}]
USERS = [{"username": "eugene"}, {"username": "pat"}]
LABEL_ROW = {"id": 10, "action": "label.buy", "detail": {"group_id": "g1", "boxes": 2, "shipment_ids": [41, 42]},
             "created_at": WHEN, "username": "pat"}
SHIPMENTS = [
    {"id": 41, "shopify_order_name": "#TS1", "backoffice_invoice_number": None, "courier_name": "USPS Ground Advantage",
     "tracking_number": "9400111", "shipping_cost": 5.12, "status": "fulfilled"},
    {"id": 42, "shopify_order_name": None, "backoffice_invoice_number": "INV-7", "courier_name": None,
     "tracking_number": None, "shipping_cost": None, "status": "voided"},
]


class AuditListTest(unittest.TestCase):
    """Admins page through the audit log newest first, narrowed to one
    family of actions or one exact action."""

    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(settings_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "admin"
        self.queries = []
        self.total = 1
        self.rows = [ROW]
        self._orig = settings_api.db.query
        settings_api.db.query = self._query

    def tearDown(self):
        settings_api.db.query = self._orig

    def _query(self, sql, params=None, one=False):
        self.queries.append((sql, params))
        if "COUNT(*)" in sql:
            return {"total": self.total}
        if "DISTINCT u.username" in sql:
            return USERS
        if "DISTINCT" in sql:
            return GROUPS
        if "FROM shipments" in sql:
            return [s for s in SHIPMENTS if s["id"] in params[0]]
        return self.rows

    def _list(self, query=""):
        res = self.client.get(f"/api/audit?{query}")
        return res.status_code, json.loads(res.get_data())

    def test_rows_carry_central_time_user_and_detail(self):
        status, body = self._list()
        self.assertEqual((status, body), (200, {
            "rows": [{"id": 9, "action": "profit.bypass", "detail": {"order": "#1001", "profit": -7.35},
                      "created_at": "10/02/2026 01:30 PM", "date": "2026-10-02", "username": "eugene"}],
            "total": 1, "limit": settings_api.AUDIT_PAGE_DEFAULT, "offset": 0,
            "groups": ["label", "profit"], "users": ["eugene", "pat"], "shipments": {}}))
        self.assertIn("ORDER BY a.created_at DESC, a.id DESC LIMIT %s OFFSET %s", self.queries[1][0])
        self.assertEqual(self.queries[1][1], [settings_api.AUDIT_PAGE_DEFAULT, 0])

    def test_group_filter_matches_the_action_family(self):
        self._list("group=profit")
        count_sql, count_params = self.queries[0]
        page_sql, page_params = self.queries[1]
        self.assertIn("WHERE split_part(a.action, '.', 1) = %s", count_sql)
        self.assertIn("WHERE split_part(a.action, '.', 1) = %s", page_sql)
        self.assertEqual((count_params, page_params), (["profit"], ["profit", settings_api.AUDIT_PAGE_DEFAULT, 0]))

    def test_action_filter_matches_exactly_and_combines_with_group(self):
        self._list("group=profit&action=profit.bypass_denied")
        sql, params = self.queries[1]
        self.assertIn("WHERE split_part(a.action, '.', 1) = %s AND a.action = %s", sql)
        self.assertEqual(params[:2], ["profit", "profit.bypass_denied"])

    def test_paging_is_clamped(self):
        status, body = self._list("limit=9999&offset=-3")
        self.assertEqual((status, body["limit"], body["offset"]), (200, settings_api.AUDIT_PAGE_MAX, 0))
        self.assertEqual(self._list("limit=0")[1]["limit"], 1)

    def test_non_integer_paging_is_rejected(self):
        status, body = self._list("limit=lots")
        self.assertEqual((status, body), (400, {"error": "limit and offset must be whole numbers"}))

    def test_empty_log_skips_the_page_query(self):
        self.total = 0
        status, body = self._list()
        self.assertEqual((status, body["rows"], body["total"]), (200, [], 0))
        self.assertFalse(any("LIMIT %s OFFSET %s" in sql for sql, _ in self.queries))

    def test_user_search_and_period_filters_combine(self):
        self._list("user=pat&q=50%25_off&days=7")
        sql, params = self.queries[1]
        self.assertIn("WHERE u.username = %s AND (a.action ILIKE %s OR a.detail::text ILIKE %s)"
                      " AND a.created_at >= now() - make_interval(days => %s)", sql)
        self.assertEqual(params[:5], ["pat", "%50\\%\\_off%", "%50\\%\\_off%", 7, settings_api.AUDIT_PAGE_DEFAULT])
        self.assertIn("LEFT JOIN users u", self.queries[0][0])

    def test_unknown_period_is_rejected(self):
        status, body = self._list("days=soon")
        self.assertEqual((status, body), (400, {"error": "days must be a whole number"}))

    def test_label_rows_bring_their_shipments_order_carrier_and_tracking(self):
        self.rows = [LABEL_ROW]
        status, body = self._list()
        self.assertEqual((status, body["shipments"]), (200, {
            "41": {"order": "#TS1", "courier": "USPS Ground Advantage", "tracking": "9400111", "cost": 5.12,
                   "status": "fulfilled"},
            "42": {"order": "INV-7", "courier": None, "tracking": None, "cost": None, "status": "voided"},
        }))

    def test_non_admins_are_refused(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "user"
        self.assertEqual(self.client.get("/api/audit").status_code, 403)


if __name__ == "__main__":
    unittest.main()

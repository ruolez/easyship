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
        self._orig = settings_api.db.query
        settings_api.db.query = self._query

    def tearDown(self):
        settings_api.db.query = self._orig

    def _query(self, sql, params=None, one=False):
        self.queries.append((sql, params))
        if "COUNT(*)" in sql:
            return {"total": self.total}
        if "DISTINCT" in sql:
            return GROUPS
        return [ROW]

    def _list(self, query=""):
        res = self.client.get(f"/api/audit?{query}")
        return res.status_code, json.loads(res.get_data())

    def test_rows_carry_central_time_user_and_detail(self):
        status, body = self._list()
        self.assertEqual((status, body), (200, {
            "rows": [{"id": 9, "action": "profit.bypass", "detail": {"order": "#1001", "profit": -7.35},
                      "created_at": "10/02/2026 01:30 PM", "username": "eugene"}],
            "total": 1, "limit": settings_api.AUDIT_PAGE_DEFAULT, "offset": 0,
            "groups": ["label", "profit"]}))
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
        self.assertEqual(len(self.queries), 2)

    def test_non_admins_are_refused(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "user"
        self.assertEqual(self.client.get("/api/audit").status_code, 403)


if __name__ == "__main__":
    unittest.main()

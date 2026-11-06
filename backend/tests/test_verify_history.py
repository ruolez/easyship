import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp", MANIFESTS_DIR="/tmp"))

import verify_store  # noqa: E402

STATS = {"verified": 3, "in_progress": 1, "forced": 0, "errors": 2}


class HistoryWhereTest(unittest.TestCase):
    """Verification history filters become one WHERE clause shared by the
    page query and the totals, so the stat cards describe every match."""

    def test_no_filters_match_everything(self):
        self.assertEqual(verify_store.history_where({}), ("TRUE", []))

    def test_sources_are_ored_and_malformed_ones_ignored(self):
        sql, params = verify_store.history_where({"sources": ["shopify:3", "backoffice:Prime", "shopify:x", "nope"]})
        self.assertEqual((sql, params), (
            "((v.source = 'shopify' AND v.shopify_store_id = %s) OR (v.source = 'backoffice' AND v.source_db = %s))",
            [3, "Prime"]))

    def test_users_match_who_started_or_verified(self):
        sql, params = verify_store.history_where({"user_ids": [5, 9]})
        self.assertEqual((sql, params), ("(v.verified_by = ANY(%s) OR v.started_by = ANY(%s))", [[5, 9], [5, 9]]))

    def test_flags_and_statuses_combine_with_and(self):
        sql, params = verify_store.history_where({"statuses": ["verified"], "flags": ["errors", "writeback_failed"]})
        self.assertEqual((sql, params), (
            "v.status = ANY(%s) AND er.errors > 0 AND (v.shopify_tag_state = 'failed' OR v.dbadmin_state = 'failed')",
            [["verified"]]))


class HistoryOrderTest(unittest.TestCase):
    def test_unknown_sort_keeps_newest_first(self):
        self.assertEqual(verify_store.history_order("nope", True), verify_store.HISTORY_DEFAULT_ORDER)

    def test_every_sort_ends_with_the_unique_tiebreak(self):
        for key, expr in verify_store.HISTORY_SORT.items():
            with self.subTest(key=key):
                self.assertEqual(verify_store.history_order(key, desc=True),
                                 f"{expr} DESC NULLS LAST, {verify_store.HISTORY_DEFAULT_ORDER}")


class HistoryPageTest(unittest.TestCase):
    def setUp(self):
        self.queries = []
        self.total = 0
        self.addCleanup(setattr, verify_store.db, "query", verify_store.db.query)
        verify_store.db.query = self._query

    def _query(self, sql, params=None, one=False):
        self.queries.append(params)
        return {"total": self.total, **STATS} if one else [{"id": 1}]

    def test_empty_result_skips_the_page_query(self):
        page = verify_store.history({}, limit=50, offset=0)
        self.assertEqual((page, len(self.queries)),
                         ({"rows": [], "total": 0, "offset": 0, "limit": 50, "stats": STATS}, 1))

    def test_offset_past_the_end_lands_on_the_last_page(self):
        self.total = 120
        page = verify_store.history({"q": "ann"}, limit=50, offset=500)
        self.assertEqual((page["offset"], self.queries[1]), (100, ("%ann%", "%ann%", 50, 100)))


if __name__ == "__main__":
    unittest.main()

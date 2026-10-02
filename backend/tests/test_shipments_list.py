import json
import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2025-07", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp",
    MANIFESTS_DIR="/tmp"))

from flask import Flask  # noqa: E402

import shipments_api  # noqa: E402

ACCOUNTS = [
    {"value": "easyship", "label": "Easyship"},
    {"value": "shipstation-5", "label": "East"},
]
EMPTY_AGG = {"total": 0, "shipments": 0, "shipping_cost": 0}
PAGE_SIZE = shipments_api.LIST_PAGE_DEFAULT


class ShipmentsListTest(unittest.TestCase):
    """The Parcels list is paged and filtered server-side: every filter, sort
    and total covers the whole table, and the filter options come from every
    parcel rather than the page on screen."""

    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.register_blueprint(shipments_api.bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
        self.queries = []
        self.rows = []
        self.agg = dict(EMPTY_AGG)
        self._orig = shipments_api.db.query
        shipments_api.db.query = self._query

    def tearDown(self):
        shipments_api.db.query = self._orig

    def _query(self, sql, params=None, **kw):
        self.queries.append((sql, params))
        return self.agg if "COUNT(*)" in sql else self.rows

    def _list(self, query=""):
        self.agg["total"] = self.agg["total"] or 1
        res = self.client.get(f"/api/shipments?{query}")
        self.assertEqual(res.status_code, 200)
        return json.loads(res.get_data())

    def _page_query(self):
        return self.queries[1]

    # ---- filters -------------------------------------------------------

    def test_provider_param_filters_by_account_key(self):
        self._list("provider=easyship,shipstation-5")
        sql, params = self._page_query()
        self.assertIn("s.provider = ANY(%s)", sql)
        self.assertIn(["easyship", "shipstation-5"], params)

    def test_no_filter_params_leave_the_list_unfiltered(self):
        self._list()
        sql, params = self._page_query()
        for fragment in ("s.provider = ANY", "s.status = ANY", "u.username = ANY",
                         shipments_api.SERVICE_NAME_SQL + " = ANY", "s.courier_name = ANY",
                         shipments_api.CARRIER_SQL + " = ANY", shipments_api.BOX_SIZE_SQL + " = ANY"):
            self.assertNotIn(fragment, sql)
        self.assertEqual(params, [PAGE_SIZE, 0])

    def test_store_service_carrier_and_size_filter_in_sql(self):
        cases = {
            "store": shipments_api.SERVICE_NAME_SQL,
            "service": "s.courier_name",
            "carrier": shipments_api.CARRIER_SQL,
            "size": shipments_api.BOX_SIZE_SQL,
        }
        for param, expr in cases.items():
            with self.subTest(param=param):
                self.queries = []
                self._list(f"{param}=A,B")
                sql, params = self._page_query()
                self.assertIn(f"{expr} = ANY(%s)", sql)
                self.assertIn(["A", "B"], params)

    def test_totals_query_shares_the_page_filters(self):
        self._list("status=fulfilled&q=acme")
        agg_sql, agg_params = self.queries[0]
        page_sql, page_params = self._page_query()
        self.assertIn("COUNT(DISTINCT COALESCE(s.group_id, '#' || s.id::text)) AS shipments", agg_sql)
        self.assertIn("COALESCE(SUM(s.shipping_cost), 0) AS shipping_cost", agg_sql)
        self.assertNotIn("LIMIT", agg_sql)
        self.assertIn("s.status = ANY(%s)", agg_sql)
        self.assertEqual(page_params, agg_params + [PAGE_SIZE, 0])

    # ---- paging --------------------------------------------------------

    def test_response_carries_rows_and_whole_set_totals(self):
        self.agg = {"total": 4320, "shipments": 3980, "shipping_cost": 12345.67}
        body = self._list()
        self.assertEqual(body, {"rows": [], "total": 4320, "shipments": 3980,
                                "shipping_cost": 12345.67, "offset": 0, "limit": PAGE_SIZE})

    def test_limit_and_offset_params_page_the_query(self):
        self.agg["total"] = 1000
        body = self._list("limit=50&offset=100")
        sql, params = self._page_query()
        self.assertTrue(sql.rstrip().endswith("LIMIT %s OFFSET %s"))
        self.assertEqual(params, [50, 100])
        self.assertEqual((body["limit"], body["offset"]), (50, 100))

    def test_limit_and_offset_are_clamped(self):
        self.agg["total"] = 1
        self.assertEqual(self._list("limit=99999")["limit"], shipments_api.LIST_PAGE_MAX)
        self.assertEqual(self._list("limit=0")["limit"], 1)
        self.assertEqual(self._list("offset=-5")["offset"], 0)

    def test_offset_past_the_end_returns_the_last_page(self):
        self.agg["total"] = 120
        body = self._list("limit=50&offset=500")
        self.assertEqual(self._page_query()[1], [50, 100])
        self.assertEqual(body["offset"], 100)

    def test_empty_result_skips_the_page_query(self):
        res = self.client.get("/api/shipments?q=nothing")
        self.assertEqual(json.loads(res.get_data())["rows"], [])
        self.assertEqual(len(self.queries), 1)

    def test_non_integer_paging_params_are_rejected(self):
        for query in ("limit=abc", "offset=1.5"):
            with self.subTest(query=query):
                res = self.client.get(f"/api/shipments?{query}")
                self.assertEqual(res.status_code, 400)
                self.assertIn("whole number", json.loads(res.get_data())["error"])

    # ---- sorting -------------------------------------------------------

    def test_default_order_is_newest_first_with_stable_tiebreaks(self):
        self._list()
        self.assertIn("ORDER BY s.created_at DESC, s.box_number ASC, s.id ASC LIMIT", self._page_query()[0])

    def test_sort_and_dir_params_order_the_query(self):
        self._list("sort=cost&dir=desc")
        self.assertIn("ORDER BY COALESCE(s.shipping_cost, -1) DESC, s.created_at DESC", self._page_query()[0])

    def test_unknown_sort_key_falls_back_to_default_order(self):
        self._list("sort=bogus&dir=desc")
        self.assertIn("ORDER BY s.created_at DESC, s.box_number ASC, s.id ASC LIMIT", self._page_query()[0])

    def test_unknown_dir_sorts_ascending(self):
        self._list("sort=created&dir=sideways")
        self.assertIn("ORDER BY s.created_at ASC, s.created_at DESC", self._page_query()[0])

    def test_every_sortable_column_has_an_expression(self):
        columns = ["ref", "user", "store", "address", "boxes", "size", "weight", "account",
                   "courier", "carrier", "cost", "tracking", "status", "created"]
        self.assertEqual(sorted(shipments_api.SORT_SQL), sorted(columns))

    # ---- filter options ------------------------------------------------

    def test_providers_endpoint_lists_accounts_as_filter_options(self):
        self.rows = [dict(a) for a in ACCOUNTS]
        res = self.client.get("/api/shipments/providers")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(json.loads(res.get_data()), ACCOUNTS)
        self.assertIn("LEFT JOIN provider_instances", self.queries[0][0])

    def test_filter_options_come_from_the_whole_table(self):
        self.rows = [{"value": "A"}, {"value": "B"}]
        res = self.client.get("/api/shipments/filter-options")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(json.loads(res.get_data()),
                         {"stores": ["A", "B"], "services": ["A", "B"],
                          "carriers": ["A", "B"], "sizes": ["A", "B"]})
        self.assertEqual(len(self.queries), 4)
        for sql, _ in self.queries:
            self.assertIn("SELECT DISTINCT", sql)
            self.assertNotIn("LIMIT", sql)
        self.assertIn("ORDER BY volume, value", self.queries[3][0])


if __name__ == "__main__":
    unittest.main()

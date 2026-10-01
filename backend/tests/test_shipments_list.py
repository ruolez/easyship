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


class ShipmentsListTest(unittest.TestCase):
    """The Parcels list filters by shipping account server-side, and the
    account filter's options come from every account that ever shipped."""

    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.register_blueprint(shipments_api.bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
        self.queries = []
        self.rows = []
        self._orig = shipments_api.db.query
        shipments_api.db.query = self._query

    def tearDown(self):
        shipments_api.db.query = self._orig

    def _query(self, sql, params=None, **kw):
        self.queries.append((sql, params))
        return self.rows

    def test_provider_param_filters_by_account_key(self):
        res = self.client.get("/api/shipments?provider=easyship,shipstation-5")
        self.assertEqual(res.status_code, 200)
        sql, params = self.queries[0]
        self.assertIn("s.provider = ANY(%s)", sql)
        self.assertIn(["easyship", "shipstation-5"], params)

    def test_no_provider_param_leaves_accounts_unfiltered(self):
        self.client.get("/api/shipments")
        sql, params = self.queries[0]
        self.assertNotIn("s.provider = ANY", sql)
        self.assertEqual(params, [200])

    def test_providers_endpoint_lists_accounts_as_filter_options(self):
        self.rows = [dict(a) for a in ACCOUNTS]
        res = self.client.get("/api/shipments/providers")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(json.loads(res.get_data()), ACCOUNTS)
        self.assertIn("LEFT JOIN provider_instances", self.queries[0][0])


if __name__ == "__main__":
    unittest.main()

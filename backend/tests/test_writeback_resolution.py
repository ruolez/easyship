import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp",
    MANIFESTS_DIR="/tmp"))

import shipments_api  # noqa: E402
import shopify_client  # noqa: E402

STORE, GID, NAME, TRACKING = 3, "gid://shopify/Order/77", "#1234", "1Z1"


def row(**overrides):
    base = {
        "id": 348, "group_id": "g1", "box_number": 1, "source": "shopify",
        "shopify_store_id": STORE, "shopify_order_id": None, "shopify_order_name": "1234",
        "tracking_number": TRACKING, "tracking_numbers": [TRACKING], "courier_name": "UPS® Ground",
        "courier_umbrella_name": "UPS",
        "writeback_shopify_at": None, "writeback_backoffice_at": None,
        "status": "label_created", "shipping_cost": 10.0,
    }
    base.update(overrides)
    return base


class GroupWritebackResolutionTest(unittest.TestCase):
    """A shipment whose ship page never got the order id (Shopify was down at
    scan time) still carries the scanned number, which the writeback resolves."""

    def setUp(self):
        self.rows = [row()]
        self.executed = []
        self.fulfilled = []
        self._orig = (shipments_api._group_rows, shipments_api.db.execute,
                      shopify_client.resolve_order, shopify_client.fulfill_order)
        shipments_api._group_rows = lambda group_id: [dict(r) for r in self.rows]
        shipments_api.db.execute = lambda sql, params=None, **kw: self.executed.append((sql, params))
        shopify_client.resolve_order = lambda store_id, number: (
            {"id": GID, "name": NAME} if number == "1234" else None)
        shopify_client.fulfill_order = lambda *a, **k: self.fulfilled.append((a, k)) or {"id": "gid://F/1"}

    def tearDown(self):
        (shipments_api._group_rows, shipments_api.db.execute,
         shopify_client.resolve_order, shopify_client.fulfill_order) = self._orig

    def order_updates(self):
        return [p for sql, p in self.executed if "shopify_order_id=%s, shopify_order_name=%s" in sql]

    def test_missing_order_id_is_resolved_from_the_number_and_persisted(self):
        results = shipments_api.run_group_writebacks("g1")
        self.assertEqual(results, {"shopify": "ok"})
        self.assertEqual(self.order_updates(), [(GID, NAME, 348)])
        self.assertEqual(self.fulfilled[0], (
            (STORE, GID, TRACKING, "UPS® Ground"),
            {"all_numbers": [TRACKING], "umbrella_name": "UPS"},
        ))

    def test_missing_store_fails_without_calling_shopify(self):
        self.rows = [row(shopify_store_id=None)]
        results = shipments_api.run_group_writebacks("g1")
        self.assertIn("No Shopify store linked", results["shopify"])
        self.assertEqual((self.fulfilled, self.order_updates()), ([], []))

    def test_unknown_number_reports_not_found(self):
        self.rows = [row(shopify_order_name="9999")]
        results = shipments_api.run_group_writebacks("g1")
        self.assertEqual(results, {"shopify": "error: Order 9999 not found in the Shopify store"})
        self.assertEqual(self.fulfilled, [])

    def test_unavailable_shopify_is_named_as_such_in_the_row_error(self):
        def down(*a, **k):
            raise shopify_client.ShopifyUnavailable("Shopify unavailable after 3 attempts — http 503")
        shopify_client.fulfill_order = down
        self.rows = [row(shopify_order_id=GID, shopify_order_name=NAME)]
        results = shipments_api.run_group_writebacks("g1")
        self.assertEqual(results, {"shopify": "error: Shopify unavailable after 3 attempts — http 503"})
        error_writes = [p for sql, p in self.executed if "SET error_message=%s" in sql]
        self.assertEqual(error_writes, [("Shopify unavailable after 3 attempts — http 503", 348)])


if __name__ == "__main__":
    unittest.main()

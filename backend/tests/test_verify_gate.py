import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp", MANIFESTS_DIR="/tmp"))

import backoffice  # noqa: E402
import shipments_api  # noqa: E402
import shipper  # noqa: E402
import shopify_client  # noqa: E402
import verify  # noqa: E402
import verify_gate  # noqa: E402
from test_profit_gate import BuyRouteTest  # noqa: E402

APPLE, PEAR = "012345678905", "098765432109"
SHOPIFY = {"source": "shopify", "shopify_store_id": 3, "shopify_order_id": "gid://shopify/Order/9",
           "shopify_order_name": "#TS1001"}
BACKOFFICE = {"source": "backoffice", "backoffice_db_id": 2, "backoffice_invoice_id": 55,
              "backoffice_invoice_number": "700123"}


def shopify_node(*lines):
    return {"lineItems": {"nodes": [
        {"title": t, "variant": {"barcode": b}, "requiresShipping": True, "unfulfilledQuantity": q,
         "currentQuantity": q, "refundableQuantity": q} for t, b, q in lines]}}


def verified_lines(*lines):
    return [{**l, "qty_verified": l["qty_required"]} for l in verify.build_lines(
        [{"title": t, "barcode": b, "qty": q} for t, b, q in lines], [])]


class GateCheckTest(unittest.TestCase):
    def setUp(self):
        self.live = None
        self.lines = []
        self.shopify_nodes = [shopify_node(("Apple", APPLE, 2))]
        self.shipper_status = "not_found"
        self.invoice_rows = []
        self.keys = []
        patches = [
            (verify_gate.verify_store, "live_by_key", lambda key: self.keys.append(key) or self.live),
            (verify_gate.verify_store, "live_lines", lambda vid: self.lines),
            (verify_gate.shopify_client, "get_picklist_orders", self._nodes),
            (verify_gate.shipper, "check_shopify", lambda n: {"status": self.shipper_status}),
            (verify_gate.shipper, "check_quotation", lambda n: {"status": self.shipper_status}),
            (verify_gate.backoffice, "quotation_number_for_invoice", lambda db_id, n: "10282026191"),
            (verify_gate.backoffice, "invoice_verify_lines", lambda db_id, inv: self.invoice_rows),
            (verify_gate.db, "get_setting", lambda key, default=None: "Shipping"),
        ]
        self._orig = [(obj, name, getattr(obj, name)) for obj, name, _ in patches]
        for obj, name, fn in patches:
            setattr(obj, name, fn)

    def tearDown(self):
        for obj, name, fn in self._orig:
            setattr(obj, name, fn)

    def _nodes(self, store_id, gids):
        if isinstance(self.shopify_nodes, Exception):
            raise self.shopify_nodes
        return self.shopify_nodes

    def test_unverified_order_is_blocked(self):
        out = verify_gate.check(SHOPIFY)
        self.assertEqual((out["verified"], out["message"], self.keys),
                         (False, "This order has not been verified", ["shopify:3:TS1001"]))

    def test_verified_and_unchanged_passes(self):
        self.live = {"id": 1, "status": "verified"}
        self.lines = verified_lines(("Apple", APPLE, 2))
        self.assertEqual(verify_gate.check(SHOPIFY)["where"], "easyship")

    def test_order_changed_after_verifying_lists_the_new_units(self):
        self.live = {"id": 1, "status": "verified"}
        self.lines = verified_lines(("Apple", APPLE, 2))
        self.shopify_nodes = [shopify_node(("Apple", APPLE, 3), ("Pear", PEAR, 1))]
        out = verify_gate.check(SHOPIFY)
        self.assertEqual((out["verified"], out["unverified"]), (False, [
            {"title": "Apple", "barcode": APPLE, "qty_required": 3, "qty_verified": 2},
            {"title": "Pear", "barcode": PEAR, "qty_required": 1, "qty_verified": 0}]))

    def test_shopify_down_keeps_the_verification(self):
        self.live = {"id": 1, "status": "verified"}
        self.lines = verified_lines(("Apple", APPLE, 2))
        self.shopify_nodes = shopify_client.ShopifyUnavailable("down")
        self.assertEqual(verify_gate.check(SHOPIFY)["verified"], True)

    def test_checked_in_old_shipper_passes(self):
        self.shipper_status = "verified"
        self.assertEqual(verify_gate.check(SHOPIFY)["where"], "shipper")

    def test_shipper_unreachable_does_not_pass(self):
        def down(number):
            raise shipper.ShipperError("down")
        verify_gate.shipper.check_shopify = down
        self.assertEqual(verify_gate.check(SHOPIFY)["verified"], False)

    def test_unfinished_verification_lists_what_is_left(self):
        self.live = {"id": 1, "status": "in_progress"}
        self.lines = [{**l, "qty_verified": 1} for l in verified_lines(("Apple", APPLE, 2))]
        out = verify_gate.check(SHOPIFY)
        self.assertEqual((out["message"], out["unverified"]), ("Verification of this order is not finished", [
            {"title": "Apple", "barcode": APPLE, "qty_required": 2, "qty_verified": 1}]))

    def test_backoffice_compares_invoice_lines_and_skips_prefixed_names(self):
        self.live = {"id": 1, "status": "verified"}
        self.lines = verified_lines(("Apple", APPLE, 2))
        self.invoice_rows = [
            {"ProductUPC": APPLE, "ProductDescription": "Apple", "QtyShipped": 0, "QtyOrdered": 2},
            {"ProductUPC": "", "ProductDescription": "SHIPPING CHARGE", "QtyShipped": 1, "QtyOrdered": 1},
        ]
        out = verify_gate.check(BACKOFFICE)
        self.assertEqual((out["verified"], self.keys), (True, ["backoffice:10282026191"]))

    def test_backoffice_invoice_without_quotation_is_blocked(self):
        verify_gate.backoffice.quotation_number_for_invoice = lambda db_id, n: None
        self.assertEqual(verify_gate.check(BACKOFFICE)["message"],
                         "No quotation found for invoice 700123 — the order cannot have been verified")

    def test_manual_shipments_are_not_gated(self):
        self.assertEqual(verify_gate.check({"source": "manual"})["verified"], True)


class BuyVerifyGateTest(BuyRouteTest):
    def setUp(self):
        super().setUp()
        self.settings = {}
        self.sections = []
        self.check = {"verified": False, "message": "This order has not been verified", "unverified": []}
        shipments_api.verify_gate.check = lambda shipment: self.check
        self._orig_sections = shipments_api.user_sections
        shipments_api.user_sections = lambda user_id, role: self.sections
        self.rows[0]["profit_check"] = None

    def tearDown(self):
        shipments_api.user_sections = self._orig_sections
        super().tearDown()

    def test_blocked_without_override_permission(self):
        status, body = self._buy(verify_override=True)
        self.assertEqual((status, body["code"], body["can_override"]), (403, "verify_gate", False))

    def test_override_permission_buys_and_records_it(self):
        self.sections = ["verify_override"]
        status, _ = self._buy(verify_override=True)
        recorded = [p for sql, p in self.executed if "verify_gate" in sql]
        self.assertEqual((status, len(recorded), [a for a, _ in self.audits if a.startswith("verify")]),
                         (200, 1, ["verify.override_buy"]))

    def test_resume_after_override_is_not_asked_again(self):
        self.rows[0]["verify_gate"] = {"overridden_by": 1}
        self.assertEqual(self._buy()[0], 200)

    def test_verified_order_buys(self):
        self.check = {"verified": True}
        self.assertEqual(self._buy()[0], 200)


if __name__ == "__main__":
    unittest.main()

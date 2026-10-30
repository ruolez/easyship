import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp", MANIFESTS_DIR="/tmp"))

import verify  # noqa: E402

APPLE, PEAR, CASE = "012345678905", "098765432109", "10012345678902"
CASES = {"10012345678902": ("12345678905", 6)}


def item(barcode, qty, title="Apple", sku=""):
    return {"barcode": barcode, "qty": qty, "title": title, "sku": sku}


def lines(*items, prefixes=()):
    return verify.build_lines(list(items), list(prefixes))


def scanned(line_list, key, qty, scanned_flag=True):
    return [{**line, "qty_verified": qty, "scanned": scanned_flag} if line["key"] == key else line
            for line in line_list]


class ParsePrefixesTest(unittest.TestCase):
    def test_newlines_and_commas_trimmed_and_deduplicated_ignoring_case(self):
        self.assertEqual(verify.parse_prefixes(" Shipping\nshipping protection, SHIPPING ,\n\n"),
                         ["Shipping", "shipping protection"])


class BuildLinesTest(unittest.TestCase):
    def test_same_barcode_lines_add_up_whatever_the_zero_padding(self):
        built = lines(item(APPLE, 2), item("0" + APPLE, 3.0), item(PEAR, 1, "Pear"))
        self.assertEqual([(l["key"], l["qty_required"], l["title"]) for l in built],
                         [("12345678905", 5, "Apple"), ("98765432109", 1, "Pear")])

    def test_name_prefix_skips_line_and_blank_barcode_needs_a_tap(self):
        built = lines(item("", 1, "Shipping - Ground"), item("", 2, "Gift wrap"), prefixes=["shipping"])
        self.assertEqual([(l["key"], l["skip_reason"], verify.needs_tap(l)) for l in built], [
            ("skip:SHIPPING - GROUND", "Name starts with “shipping”", False),
            ("item:GIFT WRAP", None, True),
        ])

    def test_zero_and_negative_quantities_are_dropped(self):
        self.assertEqual(lines(item(APPLE, 0), item(PEAR, -1), item(PEAR, "x")), [])

    def test_fractional_quotation_qty_rounds_to_units(self):
        self.assertEqual([l["qty_required"] for l in lines(item(APPLE, 2.6))], [3])


class ShopifyItemsTest(unittest.TestCase):
    def test_uses_pickable_qty_and_drops_non_shipping_lines(self):
        node = {"lineItems": {"nodes": [
            {"title": "Apple", "sku": "A", "variant": {"barcode": APPLE}, "requiresShipping": True,
             "unfulfilledQuantity": 3, "currentQuantity": 2, "refundableQuantity": 3},
            {"title": "Route protection", "variant": None, "requiresShipping": False, "unfulfilledQuantity": 1},
        ]}}
        self.assertEqual(verify.shopify_items(node), [{"title": "Apple", "sku": "A", "barcode": APPLE, "qty": 2}])


class JudgeScanTest(unittest.TestCase):
    def setUp(self):
        self.lines = lines(item(APPLE, 12), item(PEAR, 1, "Pear"))

    def judge(self, code, line_list=None):
        out = verify.judge_scan(self.lines if line_list is None else line_list, code, CASES)
        return out["result"], out["line_key"], out["units"]

    def test_unit_scan_counts_one(self):
        self.assertEqual(self.judge(APPLE + "\r"), (verify.SCAN_OK, "12345678905", 1))

    def test_case_scan_counts_its_units(self):
        self.assertEqual(self.judge(CASE), (verify.SCAN_OK, "12345678905", 6))

    def test_case_bigger_than_what_is_left_is_refused(self):
        self.assertEqual(self.judge(CASE, scanned(self.lines, "12345678905", 7)),
                         (verify.SCAN_CASE_OVER, "12345678905", 0))

    def test_last_unit_of_a_line_and_of_the_order(self):
        self.assertEqual(self.judge(PEAR), (verify.SCAN_LINE_DONE, "98765432109", 1))
        done_apples = scanned(self.lines, "12345678905", 12)
        self.assertEqual(self.judge(PEAR, done_apples), (verify.SCAN_ORDER_DONE, "98765432109", 1))

    def test_complete_line_is_an_over_scan(self):
        self.assertEqual(self.judge(PEAR, scanned(self.lines, "98765432109", 1)),
                         (verify.SCAN_OVER, "98765432109", 0))

    def test_unknown_and_skipped_barcodes_are_wrong(self):
        skipped = lines(item(APPLE, 1, "Shipping"), prefixes=["shipping"])
        self.assertEqual([self.judge("555"), self.judge(APPLE, skipped)],
                         [(verify.SCAN_WRONG, None, 0), (verify.SCAN_WRONG, None, 0)])

    def test_order_done_needs_tap_lines_confirmed_too(self):
        with_tap = lines(item(PEAR, 1, "Pear"), item("", 1, "Gift wrap"))
        self.assertEqual(self.judge(PEAR, with_tap), (verify.SCAN_LINE_DONE, "98765432109", 1))

    def test_units_counted_never_exceed_what_is_required(self):
        state = self.lines
        for _ in range(30):
            out = verify.judge_scan(state, CASE if _ % 3 else APPLE, CASES)
            if out["units"]:
                state = scanned(state, out["line_key"],
                                next(l for l in state if l["key"] == out["line_key"])["qty_verified"] + out["units"])
            self.assertTrue(all(l["qty_verified"] <= l["qty_required"] for l in state))


class CountErrorTest(unittest.TestCase):
    def setUp(self):
        self.line = lines(item(APPLE, 5))[0]

    def test_needs_a_scan_first(self):
        self.assertEqual(verify.count_error(self.line, 5), "Scan one unit of this item first")

    def test_bounds(self):
        line = {**self.line, "scanned": True}
        self.assertEqual([verify.count_error(line, n) for n in (1, 5, 6, 0, True, 2.5)], [
            None, None, "Only 5 ordered", "Enter a whole number of at least 1",
            "Enter a whole number of at least 1", "Enter a whole number of at least 1"])

    def test_tap_and_skipped_lines_refuse_counts(self):
        tap, skip = lines(item("", 1, "Wrap"), item("", 1, "Shipping"), prefixes=["shipping"])
        self.assertEqual([verify.count_error(tap, 1), verify.count_error(skip, 1)],
                         ["This item has no barcode — confirm it instead", "This item does not need verifying"])


class ProgressTest(unittest.TestCase):
    def test_counts_only_active_lines(self):
        built = scanned(lines(item(APPLE, 3), item(PEAR, 2, "Pear"), item("", 1, "Shipping"),
                              prefixes=["shipping"]), "12345678905", 3)
        self.assertEqual((verify.progress(built), verify.is_complete(built)), (
            {"units_required": 5, "units_verified": 3, "lines_total": 2, "lines_done": 1}, False))


class MergeAndUnverifiedTest(unittest.TestCase):
    def test_reread_keeps_verified_counts_capped_at_the_new_quantity(self):
        old = scanned(lines(item(APPLE, 4), item(PEAR, 2, "Pear")), "12345678905", 4)
        old = scanned(old, "98765432109", 2)
        new = lines(item(APPLE, 3), item(PEAR, 5, "Pear"), item("555", 1, "Plum"))
        merged = verify.merge_lines(old, new)
        self.assertEqual([(l["key"], l["qty_required"], l["qty_verified"], l["scanned"]) for l in merged], [
            ("12345678905", 3, 3, True), ("98765432109", 5, 2, True), ("555", 1, 0, False)])

    def test_unverified_items_lists_only_what_needs_more(self):
        verified = scanned(lines(item(APPLE, 4)), "12345678905", 4)
        current = lines(item(APPLE, 3), item(PEAR, 2, "Pear"), item("", 1, "Shipping"), prefixes=["shipping"])
        self.assertEqual(verify.unverified_items(verified, current), [
            {"title": "Pear", "barcode": PEAR, "qty_required": 2, "qty_verified": 0}])

    def test_a_fully_verified_unchanged_order_has_nothing_unverified(self):
        built = lines(item(APPLE, 2), item(PEAR, 1, "Pear"))
        done = [{**l, "qty_verified": l["qty_required"]} for l in built]
        self.assertEqual(verify.unverified_items(done, built), [])


class DetectStoreTest(unittest.TestCase):
    STORES = [{"id": 1, "prefix": "TS"}, {"id": 2, "prefix": "TSW"}, {"id": 3, "prefix": ""}]

    def test_longest_prefix_wins_ignoring_case_and_hash(self):
        self.assertEqual([verify.detect_store(n, self.STORES)["id"] for n in ("#tsw1001", "TS1001")], [2, 1])

    def test_no_match_and_tie(self):
        self.assertIsNone(verify.detect_store("10282026191", self.STORES))
        with self.assertRaises(ValueError):
            verify.detect_store("AB1", [{"id": 1, "prefix": "AB"}, {"id": 2, "prefix": "ab"}])


class OrderKeyTest(unittest.TestCase):
    def test_shopify_key_ignores_hash_and_case(self):
        self.assertEqual({verify.shopify_order_key(3, "#tsw1001"), verify.shopify_order_key(3, " TSW1001 ")},
                         {"shopify:3:TSW1001"})


if __name__ == "__main__":
    unittest.main()

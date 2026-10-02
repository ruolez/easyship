import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

import profit  # noqa: E402

LINES = [
    {"description": "Widget", "sku": "W-1", "quantity": 2, "unit_price": 20.0, "unit_cost": 8.0},
    {"description": "Gadget", "sku": "G-1", "quantity": 1, "unit_price": 50.0, "unit_cost": 30.0},
]
# revenue 100 (90 items + 10 shipping), items cost 46
ECON = profit.compute_economics(LINES, 90.0, 10.0, "USD")
LABEL = 10.0  # → profit 44.00, margin 44.0%
OFF = profit.parse_thresholds("", "")


def costed(lines, **over):
    return profit.compute_economics([dict(line, **over) for line in lines], 90.0, 10.0, "USD")


class ComputeEconomicsTest(unittest.TestCase):
    def test_costed_lines_sum_to_revenue_and_cost(self):
        self.assertEqual(ECON, {
            "available": True, "reason": None, "reason_text": None, "currency": "USD",
            "items_subtotal": 90.0, "shipping_paid": 10.0, "revenue": 100.0,
            "items_cost": 46.0, "line_count": 2, "missing_cost": []})

    def test_missing_or_zero_cost_is_charged_at_ninety_percent_of_price_and_flagged(self):
        lines = [dict(LINES[0], unit_cost=None), dict(LINES[1], unit_cost=0)]
        econ = profit.compute_economics(lines, 90.0, 0, "USD")
        self.assertEqual((econ["items_cost"], econ["missing_cost"]), (81.0, [
            {"description": "Widget", "sku": "W-1", "quantity": 2},
            {"description": "Gadget", "sku": "G-1", "quantity": 1}]))

    def test_zero_quantity_lines_are_skipped(self):
        econ = profit.compute_economics([dict(LINES[0], quantity=0), LINES[1]], 50.0, 0, "USD")
        self.assertEqual((econ["line_count"], econ["items_cost"], econ["missing_cost"]), (1, 30.0, []))

    def test_money_is_rounded_to_cents(self):
        econ = profit.compute_economics(
            [{"quantity": 3, "unit_price": 1.0, "unit_cost": 0.333}], 2.996, 0.004, "USD")
        self.assertEqual((econ["items_cost"], econ["items_subtotal"], econ["shipping_paid"], econ["revenue"]),
                         (1.0, 3.0, 0.0, 3.0))

    def test_string_amounts_are_accepted(self):
        econ = profit.compute_economics(
            [{"quantity": "2", "unit_price": "20.00", "unit_cost": "8.00"}], "40.00", "0.00", "USD")
        self.assertEqual((econ["revenue"], econ["items_cost"]), (40.0, 16.0))


class EvaluateTest(unittest.TestCase):
    def test_profit_and_margin_above_both_minimums_pass(self):
        self.assertEqual(profit.evaluate(ECON, LABEL, "USD", profit.parse_thresholds("10", "20")), {
            "available": True, "reason": None, "reason_text": None, "currency": "USD",
            "revenue": 100.0, "items_cost": 46.0, "label_cost": 10.0,
            "profit": 44.0, "margin_pct": 44.0, "below_threshold": False, "reasons": [],
            "thresholds": {"min_amount": 10.0, "min_margin_pct": 20.0}, "missing_cost": []})

    def test_profit_under_the_dollar_minimum_is_flagged(self):
        out = profit.evaluate(ECON, LABEL, "USD", profit.parse_thresholds("50", ""))
        self.assertEqual((out["below_threshold"], out["reasons"]),
                         (True, ["Profit $44.00 is below the $50.00 minimum"]))

    def test_margin_under_the_percent_minimum_is_flagged(self):
        out = profit.evaluate(ECON, LABEL, "USD", profit.parse_thresholds("", "50"))
        self.assertEqual((out["below_threshold"], out["reasons"]),
                         (True, ["Margin 44.0% is below the 50% minimum"]))

    def test_both_checks_failing_list_both_reasons(self):
        out = profit.evaluate(ECON, LABEL, "USD", profit.parse_thresholds("50", "50"))
        self.assertEqual(out["reasons"], ["Profit $44.00 is below the $50.00 minimum",
                                         "Margin 44.0% is below the 50% minimum"])

    def test_negative_profit_is_formatted_with_a_leading_minus(self):
        out = profit.evaluate(ECON, 57.1, "USD", profit.parse_thresholds("10", ""))
        self.assertEqual((out["profit"], out["margin_pct"], out["reasons"]),
                         (-3.1, -3.1, ["Profit -$3.10 is below the $10.00 minimum"]))

    def test_no_thresholds_means_never_below_but_profit_still_computed(self):
        out = profit.evaluate(ECON, LABEL, "USD", OFF)
        self.assertEqual((out["profit"], out["below_threshold"], out["reasons"]), (44.0, False, []))

    def test_unavailable_economics_count_as_below_when_a_threshold_is_set(self):
        econ = profit.unavailable("no_order_id")
        out = profit.evaluate(econ, LABEL, "USD", profit.parse_thresholds("0", ""))
        self.assertEqual((out["profit"], out["below_threshold"], out["reasons"]), (None, True, [
            "Profit could not be determined: the order could not be loaded from Shopify"]))
        self.assertFalse(profit.evaluate(econ, LABEL, "USD", OFF)["below_threshold"])

    def test_missing_economics_read_as_rated_before_the_check_existed(self):
        out = profit.evaluate(None, LABEL, "USD", profit.parse_thresholds("0", ""))
        self.assertEqual(out["reasons"], [
            "Profit could not be determined: the shipment was rated before profit checks were turned on"])

    def test_unknown_label_cost_counts_as_below(self):
        out = profit.evaluate(ECON, None, None, profit.parse_thresholds("0", ""))
        self.assertEqual((out["label_cost"], out["below_threshold"], out["reasons"]), (None, True, [
            "Profit could not be determined: the chosen rate was not among the rates offered for this shipment"]))

    def test_currency_mismatch_counts_as_below(self):
        out = profit.evaluate(ECON, LABEL, "CAD", profit.parse_thresholds("0", ""))
        self.assertEqual((out["profit"], out["reasons"]),
                         (None, ["Rate currency CAD differs from the order currency USD"]))

    def test_zero_revenue_fails_only_the_margin_check(self):
        free = profit.compute_economics([], 0, 0, "USD")
        only_amount = profit.evaluate(free, 0, "USD", profit.parse_thresholds("0", ""))
        self.assertEqual((only_amount["margin_pct"], only_amount["below_threshold"]), (None, False))
        with_pct = profit.evaluate(free, 0, "USD", profit.parse_thresholds("0", "10"))
        self.assertEqual((with_pct["below_threshold"], with_pct["reasons"]),
                         (True, ["Margin cannot be measured — the order has no revenue"]))

    def test_missing_cost_lines_are_carried_through(self):
        econ = costed(LINES[:1], unit_cost=None)
        out = profit.evaluate(econ, LABEL, "USD", OFF)
        self.assertEqual(out["missing_cost"], [{"description": "Widget", "sku": "W-1", "quantity": 2}])


class ThresholdsTest(unittest.TestCase):
    def test_blank_or_garbage_values_disable_the_check(self):
        for amount, pct in (("", ""), (None, None), ("abc", " ")):
            with self.subTest(amount=amount, pct=pct):
                self.assertEqual(profit.parse_thresholds(amount, pct),
                                 {"min_amount": None, "min_margin_pct": None, "enabled": False})

    def test_one_value_is_enough_to_enable(self):
        self.assertEqual(profit.parse_thresholds("12.5", ""),
                         {"min_amount": 12.5, "min_margin_pct": None, "enabled": True})
        self.assertEqual(profit.parse_thresholds("", "0"),
                         {"min_amount": None, "min_margin_pct": 0.0, "enabled": True})

    def test_load_reads_both_settings(self):
        orig = profit.db.get_setting
        profit.db.get_setting = lambda key, default=None: {
            profit.SETTING_MIN_AMOUNT: "5", profit.SETTING_MIN_PCT: "15"}.get(key, default)
        try:
            self.assertEqual(profit.load_thresholds(),
                             {"min_amount": 5.0, "min_margin_pct": 15.0, "enabled": True})
        finally:
            profit.db.get_setting = orig


RATES = [
    {"provider": "easyship", "courier_service_id": 101, "total_charge": 12.35, "currency": "USD"},
    {"provider": "shippo", "courier_service_id": "ups_ground", "total_charge": 9.9, "currency": "USD"},
]


class GateSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.check = profit.snapshot(ECON, [dict(r, preferred=False, courier_name="x") for r in RATES])

    def test_snapshot_keeps_only_what_the_gate_needs(self):
        self.assertEqual(self.check, {"economics": ECON, "offered_rates": RATES, "cleared": None})

    def test_label_cost_is_resolved_by_provider_and_service_regardless_of_id_type(self):
        self.assertEqual(profit.resolve_label_cost(self.check, "easyship", "101"), (12.35, "USD"))
        self.assertEqual(profit.resolve_label_cost(self.check, "shippo", "ups_ground"), (9.9, "USD"))
        self.assertEqual(profit.resolve_label_cost(self.check, "easyship", "ups_ground"), (None, None))
        self.assertEqual(profit.resolve_label_cost(None, "easyship", "101"), (None, None))

    def test_gate_does_not_apply_to_manual_shipments_or_when_off(self):
        row = {"source": "shopify", "profit_check": self.check}
        self.assertIsNone(profit.gate_for_buy(dict(row, source="manual"), "easyship", 101,
                                              profit.parse_thresholds("0", "")))
        self.assertIsNone(profit.gate_for_buy(row, "easyship", 101, OFF))

    def test_gate_evaluates_the_snapshot_rate(self):
        row = {"source": "backoffice", "profit_check": self.check}
        out = profit.gate_for_buy(row, "shippo", "ups_ground", profit.parse_thresholds("50", ""))
        self.assertEqual((out["label_cost"], out["profit"], out["below_threshold"]), (9.9, 44.1, True))

    def test_draft_without_snapshot_is_gated_as_unknown(self):
        out = profit.gate_for_buy({"source": "shopify", "profit_check": None}, "easyship", 101,
                                  profit.parse_thresholds("0", ""))
        self.assertEqual((out["available"], out["below_threshold"]), (False, True))

    def test_cleared_marker_matches_the_same_rate_only(self):
        cleared = dict(self.check, cleared={"provider": "easyship", "courier_service_id": 101, "bypassed": True})
        self.assertTrue(profit.already_cleared({"profit_check": cleared}, "easyship", "101"))
        self.assertFalse(profit.already_cleared({"profit_check": cleared}, "shippo", "ups_ground"))
        self.assertFalse(profit.already_cleared({"profit_check": self.check}, "easyship", 101))
        self.assertFalse(profit.already_cleared({"profit_check": None}, "easyship", 101))


if __name__ == "__main__":
    unittest.main()

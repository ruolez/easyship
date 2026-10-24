import json
import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

import picklist_rules  # noqa: E402

TEXAS = {"id": "tx", "name": "Texas only", "action": "keep", "match": "all",
         "conditions": [{"field": "ship_state", "op": "in", "value": ["TX"]}]}


def order(tags=(), customer_tags=(), state="TX", store_id=1, shipping="UPS Ground", same_address=True,
          total="150.00"):
    return {"store_id": store_id, "tags": list(tags), "customer_tags": list(customer_tags),
            "ship_state": state, "shipping_method": shipping, "billing_matches_shipping": same_address,
            "total": total}


def rule(*conditions, match="all", action="hide", rid="r1"):
    return {"id": rid, "name": rid, "action": action, "match": match,
            "conditions": [{"field": f, "op": o, "value": v} for f, o, v in conditions]}


class ParseRulesTest(unittest.TestCase):
    def test_bad_input_gives_no_rules(self):
        self.assertEqual(
            [picklist_rules.parse_rules(raw) for raw in (None, "", "not json", "{}", "[1, \"x\"]")],
            [[], [], [], [], []])

    def test_clean_rule_round_trips(self):
        self.assertEqual(picklist_rules.parse_rules(json.dumps([TEXAS])), [TEXAS])

    def test_values_are_normalized_per_field(self):
        raw = [rule(("ship_state", "in", " tx, ca ,"), ("order_tag", "contains", "  Wholesale "),
                    ("store", "not_in", [2, "3"]), match="ANY", action="Keep")]
        self.assertEqual(picklist_rules.parse_rules(raw), [{
            "id": "r1", "name": "r1", "action": "keep", "match": "any",
            "conditions": [
                {"field": "ship_state", "op": "in", "value": ["TX", "CA"]},
                {"field": "order_tag", "op": "contains", "value": "Wholesale"},
                {"field": "store", "op": "not_in", "value": ["2", "3"]},
            ]}])

    def test_shipping_method_keeps_its_text(self):
        raw = [rule(("shipping_method", "contains", "  Economy "), ("shipping_method", "has", "x"),
                    ("shipping_method", "not_contains", ""))]
        self.assertEqual(picklist_rules.parse_rules(raw)[0]["conditions"],
                         [{"field": "shipping_method", "op": "contains", "value": "Economy"}])

    def test_unknown_fields_ops_and_empty_values_are_dropped(self):
        raw = [rule(("color", "contains", "red"), ("order_tag", "in", "x"), ("customer_tag", "contains", " "),
                    ("ship_state", "in", ",,"), ("customer_tag", "not_contains", "vip"))]
        self.assertEqual(picklist_rules.parse_rules(raw)[0]["conditions"],
                         [{"field": "customer_tag", "op": "not_contains", "value": "vip"}])

    def test_saved_has_and_not_has_tag_rules_become_contains(self):
        raw = [rule(("order_tag", "has", "wholesale"), ("customer_tag", "not_has", "vip"))]
        self.assertEqual(picklist_rules.parse_rules(raw)[0]["conditions"], [
            {"field": "order_tag", "op": "contains", "value": "wholesale"},
            {"field": "customer_tag", "op": "not_contains", "value": "vip"},
        ])

    def test_order_total_keeps_a_dollar_amount(self):
        raw = [rule(("order_total", "at_least", " $1,250.5 "), ("order_total", "under", 0),
                    ("order_total", "under", "99.999"), ("order_total", "at_least", "-5"),
                    ("order_total", "at_least", "lots"), ("order_total", "under", ""),
                    ("order_total", "at_least", "nan"), ("order_total", "contains", "10"))]
        self.assertEqual(picklist_rules.parse_rules(raw)[0]["conditions"], [
            {"field": "order_total", "op": "at_least", "value": 1250.5},
            {"field": "order_total", "op": "under", "value": 0.0},
            {"field": "order_total", "op": "under", "value": 100.0},
        ])

    def test_rule_without_name_or_conditions_is_dropped(self):
        nameless = {**TEXAS, "name": "  "}
        empty = {**TEXAS, "id": "e", "conditions": [{"field": "order_tag", "op": "contains", "value": ""}]}
        self.assertEqual(picklist_rules.parse_rules([nameless, empty]), [])

    def test_missing_or_duplicate_ids_get_fresh_ones(self):
        rules = picklist_rules.parse_rules([{**TEXAS, "id": ""}, TEXAS, TEXAS])
        ids = [r["id"] for r in rules]
        self.assertEqual((len(set(ids)), ids[1]), (3, "tx"))


class RuleMatchesTest(unittest.TestCase):
    def test_tag_contains_matches_part_of_any_tag_without_case(self):
        r = rule(("order_tag", "contains", "WHOLESALE"))
        self.assertEqual(
            [picklist_rules.rule_matches(r, order(tags=t))
             for t in (["wholesale"], ["retail", "Wholesale-Tier2"], ["retail"], [])],
            [True, True, False, False])

    def test_tag_not_contains_matches_when_no_tag_holds_the_text(self):
        r = rule(("customer_tag", "not_contains", "vip"))
        self.assertEqual(
            [picklist_rules.rule_matches(r, order(customer_tags=t)) for t in (["VIP"], ["gold-vip"], ["gold"], [])],
            [False, False, True, True])

    def test_saved_has_rule_matches_like_contains(self):
        r = picklist_rules.parse_rules([rule(("order_tag", "has", "wholesale"))])[0]
        self.assertEqual(picklist_rules.rule_matches(r, order(tags=["Wholesale-Tier2"])), True)

    def test_order_total_at_least_and_under_split_at_the_amount(self):
        at_least = rule(("order_total", "at_least", 100.0))
        under = rule(("order_total", "under", 100.0))
        totals = ("99.99", "100.00", "100.01", "2500")
        self.assertEqual(
            [[picklist_rules.rule_matches(r, order(total=t)) for t in totals] for r in (at_least, under)],
            [[False, True, True, True], [True, False, False, False]])

    def test_two_total_conditions_make_a_range(self):
        r = rule(("order_total", "at_least", 50.0), ("order_total", "under", 200.0))
        self.assertEqual(
            [picklist_rules.rule_matches(r, order(total=t)) for t in ("49.99", "50", "199.99", "200")],
            [False, True, True, False])

    def test_unknown_order_total_matches_neither_operator(self):
        conds = [rule(("order_total", op, 100.0)) for op in ("at_least", "under")]
        self.assertEqual(
            [picklist_rules.rule_matches(r, order(total=t)) for r in conds for t in (None, "", "n/a")],
            [False] * 6)

    def test_state_and_store_lists(self):
        r = rule(("ship_state", "not_in", ["CA", "NY"]), ("store", "in", ["2"]))
        self.assertEqual(
            [picklist_rules.rule_matches(r, order(state=s, store_id=sid))
             for s, sid in (("TX", 2), ("CA", 2), ("TX", 1), ("tx", 2))],
            [True, False, False, True])

    def test_shipping_method_matches_part_of_the_name_without_case(self):
        contains = rule(("shipping_method", "contains", "economy"))
        not_contains = rule(("shipping_method", "not_contains", "economy"))
        economy = order(shipping="Economy Shipping (Estimated 5-8 days)")
        express = order(shipping="UPS Next Day Air")
        none = order(shipping="")
        self.assertEqual(
            [picklist_rules.rule_matches(r, o) for r in (contains, not_contains) for o in (economy, express, none)],
            [True, False, False, False, True, True])

    def test_address_match_flag(self):
        parsed = picklist_rules.parse_rules([rule(("address_match", "match", None), ("address_match", "differ", "x"))])
        r = parsed[0]
        self.assertEqual(
            (r["conditions"], [picklist_rules.rule_matches(r, order(same_address=v)) for v in (True, False, None)]),
            ([{"field": "address_match", "op": "match", "value": "yes"}], [True, False, False]))

    def test_all_versus_any(self):
        conds = (("order_tag", "contains", "a"), ("order_tag", "contains", "b"))
        only_a = order(tags=["a"])
        self.assertEqual(
            (picklist_rules.rule_matches(rule(*conds, match="all"), only_a),
             picklist_rules.rule_matches(rule(*conds, match="any"), only_a)),
            (False, True))

    def test_missing_order_fields_do_not_crash(self):
        r = rule(("ship_state", "in", ["TX"]), ("order_tag", "contains", "x"), ("order_total", "at_least", 1.0),
                 match="any")
        self.assertEqual(picklist_rules.rule_matches(r, {}), False)


class MatchedRuleIdsTest(unittest.TestCase):
    def test_returns_ids_of_every_matching_rule_in_order(self):
        rules = [rule(("order_tag", "contains", "a"), rid="first"),
                 rule(("ship_state", "in", ["CA"]), rid="second"),
                 rule(("store", "in", ["1"]), rid="third")]
        self.assertEqual(picklist_rules.matched_rule_ids(rules, order(tags=["A"])), ["first", "third"])


def preset(*groups, join="and", name="Retail", pid="p1"):
    return {"id": pid, "name": name, "join": join,
            "groups": [{"join": gj, "items": [{"rule": r, "negate": n} for r, n in items]} for gj, items in groups]}


class ParsePresetsTest(unittest.TestCase):
    RULES = ["wholesale", "big", "same"]

    def test_bad_input_gives_no_presets(self):
        self.assertEqual([picklist_rules.parse_presets(raw, self.RULES) for raw in (None, "", "nope", "{}", "[3]")],
                         [[], [], [], [], []])

    def test_clean_preset_round_trips(self):
        clean = preset(("or", [("wholesale", False), ("big", False)]), ("and", [("same", True)]))
        self.assertEqual(picklist_rules.parse_presets(json.dumps([clean]), self.RULES), [clean])

    def test_joins_and_negation_are_normalized(self):
        raw = [{"id": "p1", "name": "  Mixed ", "join": "OR", "groups": [
            {"join": "nonsense", "items": [{"rule": "big", "negate": "yes"}, {"rule": "same", "negate": True}]}]}]
        self.assertEqual(picklist_rules.parse_presets(raw, self.RULES), [preset(
            ("and", [("big", False), ("same", True)]), join="or", name="Mixed")])

    def test_items_of_deleted_rules_then_empty_groups_then_empty_presets_go(self):
        raw = [preset(("and", [("gone", False), ("big", False)]), ("or", [("gone", True)])),
               preset(("and", [("gone", False)]), pid="p2", name="Only gone")]
        self.assertEqual(picklist_rules.parse_presets(raw, self.RULES), [preset(("and", [("big", False)]))])

    def test_repeated_items_in_a_group_are_kept_once(self):
        raw = [preset(("and", [("big", False), ("big", False), ("big", True)]))]
        self.assertEqual(picklist_rules.parse_presets(raw, self.RULES)[0]["groups"][0]["items"],
                         [{"rule": "big", "negate": False}, {"rule": "big", "negate": True}])

    def test_preset_without_a_name_is_dropped_and_ids_stay_unique(self):
        raw = [preset(("and", [("big", False)]), name=" "), preset(("and", [("big", False)])),
               preset(("and", [("same", False)]), name="Twin")]
        presets = picklist_rules.parse_presets(raw, self.RULES)
        self.assertEqual(([p["name"] for p in presets], presets[0]["id"], len({p["id"] for p in presets})),
                         (["Retail", "Twin"], "p1", 2))


if __name__ == "__main__":
    unittest.main()

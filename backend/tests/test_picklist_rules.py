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


def order(tags=(), customer_tags=(), state="TX", store_id=1):
    return {"store_id": store_id, "tags": list(tags), "customer_tags": list(customer_tags),
            "ship_state": state}


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
        raw = [rule(("ship_state", "in", " tx, ca ,"), ("order_tag", "has", "  Wholesale "),
                    ("store", "not_in", [2, "3"]), match="ANY", action="Keep")]
        self.assertEqual(picklist_rules.parse_rules(raw), [{
            "id": "r1", "name": "r1", "action": "keep", "match": "any",
            "conditions": [
                {"field": "ship_state", "op": "in", "value": ["TX", "CA"]},
                {"field": "order_tag", "op": "has", "value": "Wholesale"},
                {"field": "store", "op": "not_in", "value": ["2", "3"]},
            ]}])

    def test_unknown_fields_ops_and_empty_values_are_dropped(self):
        raw = [rule(("color", "has", "red"), ("order_tag", "in", "x"), ("customer_tag", "has", " "),
                    ("ship_state", "in", ",,"), ("customer_tag", "not_has", "vip"))]
        self.assertEqual(picklist_rules.parse_rules(raw)[0]["conditions"],
                         [{"field": "customer_tag", "op": "not_has", "value": "vip"}])

    def test_rule_without_name_or_conditions_is_dropped(self):
        nameless = {**TEXAS, "name": "  "}
        empty = {**TEXAS, "id": "e", "conditions": [{"field": "order_tag", "op": "has", "value": ""}]}
        self.assertEqual(picklist_rules.parse_rules([nameless, empty]), [])

    def test_missing_or_duplicate_ids_get_fresh_ones(self):
        rules = picklist_rules.parse_rules([{**TEXAS, "id": ""}, TEXAS, TEXAS])
        ids = [r["id"] for r in rules]
        self.assertEqual((len(set(ids)), ids[1]), (3, "tx"))


class RuleMatchesTest(unittest.TestCase):
    def test_tag_conditions_are_case_insensitive(self):
        r = rule(("order_tag", "has", "WHOLESALE"))
        self.assertEqual(
            (picklist_rules.rule_matches(r, order(tags=["wholesale"])),
             picklist_rules.rule_matches(r, order(tags=["retail"]))),
            (True, False))

    def test_not_has_matches_when_tag_absent(self):
        r = rule(("customer_tag", "not_has", "vip"))
        self.assertEqual(
            (picklist_rules.rule_matches(r, order(customer_tags=["VIP"])),
             picklist_rules.rule_matches(r, order(customer_tags=[]))),
            (False, True))

    def test_state_and_store_lists(self):
        r = rule(("ship_state", "not_in", ["CA", "NY"]), ("store", "in", ["2"]))
        self.assertEqual(
            [picklist_rules.rule_matches(r, order(state=s, store_id=sid))
             for s, sid in (("TX", 2), ("CA", 2), ("TX", 1), ("tx", 2))],
            [True, False, False, True])

    def test_all_versus_any(self):
        conds = (("order_tag", "has", "a"), ("order_tag", "has", "b"))
        only_a = order(tags=["a"])
        self.assertEqual(
            (picklist_rules.rule_matches(rule(*conds, match="all"), only_a),
             picklist_rules.rule_matches(rule(*conds, match="any"), only_a)),
            (False, True))

    def test_missing_order_fields_do_not_crash(self):
        r = rule(("ship_state", "in", ["TX"]), ("order_tag", "has", "x"), match="any")
        self.assertEqual(picklist_rules.rule_matches(r, {}), False)


class MatchedRuleIdsTest(unittest.TestCase):
    def test_returns_ids_of_every_matching_rule_in_order(self):
        rules = [rule(("order_tag", "has", "a"), rid="first"),
                 rule(("ship_state", "in", ["CA"]), rid="second"),
                 rule(("store", "in", ["1"]), rid="third")]
        self.assertEqual(picklist_rules.matched_rule_ids(rules, order(tags=["A"])), ["first", "third"])


if __name__ == "__main__":
    unittest.main()

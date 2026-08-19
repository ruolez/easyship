import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(get_setting=lambda *a, **k: None))

import tag_rules  # noqa: E402


class ParseRulesTest(unittest.TestCase):
    def test_keeps_valid_rules_and_normalizes(self):
        raw = '[{"tag": " Adult ", "service": "UPS Ground", "signature": "ADULT"}, {"tag": ""}, 5]'
        self.assertEqual(tag_rules.parse_rules(raw), [
            {"tag": "Adult", "service": "UPS Ground", "signature": "adult"},
        ])

    def test_bad_json_or_shape_is_empty(self):
        self.assertEqual(tag_rules.parse_rules("{not json"), [])
        self.assertEqual(tag_rules.parse_rules('{"tag": "x"}'), [])
        self.assertEqual(tag_rules.parse_rules(None), [])


class ServiceMatchTest(unittest.TestCase):
    def test_matches_across_provider_name_styles(self):
        self.assertTrue(tag_rules.service_matches("UPS Ground", "UPS® Ground"))
        self.assertTrue(tag_rules.service_matches("ups  ground", "UPS Ground"))
        self.assertTrue(tag_rules.service_matches("Priority Mail", "USPS Priority Mail Express"))
        self.assertFalse(tag_rules.service_matches("UPS Ground", "UPS Next Day Air"))
        self.assertFalse(tag_rules.service_matches("", "UPS Ground"))


class ResolveTest(unittest.TestCase):
    rules = [
        {"tag": "adult-signature", "service": "", "signature": "adult"},
        {"tag": "fragile", "service": "UPS Ground", "signature": "signature"},
        {"tag": "rush", "service": "UPS Next Day Air", "signature": "none"},
    ]

    def test_strongest_signature_and_first_service_win(self):
        out = tag_rules.resolve(self.rules, ["Fragile", "ADULT-SIGNATURE", "rush"])
        self.assertEqual((out["signature"], out["service"]), ("adult", "UPS Ground"))
        self.assertEqual([r["tag"] for r in out["matched"]], ["adult-signature", "fragile", "rush"])

    def test_no_matching_tags(self):
        self.assertEqual(tag_rules.resolve(self.rules, ["gift"]),
                         {"signature": "none", "service": "", "matched": []})
        self.assertEqual(tag_rules.resolve(self.rules, None)["signature"], "none")


class PreferredByTest(unittest.TestCase):
    def test_tag_rule_outranks_preset_id(self):
        self.assertEqual(
            tag_rules.preferred_by("UPS® Ground", "svc-1", "UPS Ground", "svc-1"), "tag_rule")

    def test_preset_id_matches_when_no_rule_name(self):
        self.assertEqual(tag_rules.preferred_by("UPS® Ground", 42, "", "42"), "preset")
        self.assertEqual(tag_rules.preferred_by("UPS® Ground", "svc-1", "", "svc-2"), None)

    def test_empty_inputs_never_match(self):
        self.assertEqual(tag_rules.preferred_by("UPS® Ground", "", "", ""), None)
        self.assertEqual(tag_rules.preferred_by("UPS® Ground", "svc-1", "", ""), None)


if __name__ == "__main__":
    unittest.main()

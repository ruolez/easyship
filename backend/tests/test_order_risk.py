import unittest

import order_risk


def txn(name, kind="SALE", status="SUCCESS"):
    return {"kind": kind, "status": status, "paymentDetails": {"name": name}}


def addr(first, last):
    return {"firstName": first, "lastName": last}


class CardholderNameMatchTest(unittest.TestCase):
    def test_cases(self):
        cases = [
            ("John Smith", "John Smith", "MATCH"),
            ("J Smith", "John Smith", "MATCH"),
            ("SMITH/JOHN", "John Smith", "MATCH"),
            ("Mr. José A. Smith Jr", "Jose Smith", "MATCH"),
            ("Jane Smith", "John Smith", "LAST_NAME_ONLY"),
            ("John Brown", "John Smith", "MISMATCH"),
            ("", "John Smith", None),
        ]
        self.assertEqual(
            [order_risk.cardholder_name_match(card, name) for card, name, _ in cases],
            [expected for _, _, expected in cases])


class CardholderCheckTest(unittest.TestCase):
    def test_each_card_takes_its_best_name_and_the_worst_card_decides(self):
        order = {
            "transactions": [txn("John Smith"), txn("Jane Smith"), txn("Ann Brown", status="FAILURE")],
            "billingAddress": addr("John", "Smith"),
            "shippingAddress": addr("Ann", "Smith"),
        }
        self.assertEqual(order_risk.cardholder_check(order), {
            "status": "LAST_NAME_ONLY",
            "card_names": ["John Smith", "Jane Smith"],
            "billing_name": "John Smith",
            "shipping_name": "Ann Smith",
        })

    def test_refunds_and_missing_names_give_nothing_to_check(self):
        order = {"transactions": [txn("John Smith", kind="REFUND")], "billingAddress": addr("John", "Smith")}
        self.assertEqual(order_risk.cardholder_check(order), None)


class OrderRiskTest(unittest.TestCase):
    def test_level_is_the_most_severe_assessment(self):
        risk = {"assessments": [{"riskLevel": "LOW"}, {"riskLevel": "HIGH"}, {"riskLevel": "WEIRD"}]}
        self.assertEqual(order_risk.risk_level(risk), "HIGH")

    def test_panel_shape(self):
        order = {
            "clientIp": "8.8.8.8",
            "risk": {
                "recommendation": "INVESTIGATE",
                "assessments": [{
                    "riskLevel": "MEDIUM",
                    "provider": None,
                    "facts": [{"description": "Billing address differs", "sentiment": "NEGATIVE"}, {"sentiment": "NEUTRAL"}],
                }],
            },
        }
        self.assertEqual(order_risk.order_risk(order), {
            "level": "MEDIUM",
            "recommendation": "INVESTIGATE",
            "assessments": [{"provider": None, "level": "MEDIUM",
                             "facts": [{"description": "Billing address differs", "sentiment": "NEGATIVE"}]}],
            "ip": "8.8.8.8",
            "ip_location": None,
            "cardholder": None,
        })

    def test_no_risk_data_at_all_is_none(self):
        self.assertEqual(order_risk.order_risk({}), None)


class IpLocationTest(unittest.TestCase):
    def test_private_and_invalid_ips_are_never_looked_up(self):
        orig = order_risk.requests.get
        order_risk.requests.get = lambda *a, **k: self.fail("looked up")
        try:
            self.assertEqual([order_risk.ip_location(ip) for ip in ("10.0.0.1", "nonsense", None)],
                             [None, None, None])
        finally:
            order_risk.requests.get = orig


if __name__ == "__main__":
    unittest.main()

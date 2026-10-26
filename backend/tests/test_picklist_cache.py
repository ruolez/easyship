import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp", MANIFESTS_DIR="/tmp"))

import picklist_cache  # noqa: E402

NOW = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)


def state(synced_ago, full_ago):
    return {"synced_at": NOW - synced_ago, "full_at": NOW - full_ago, "changed_since": NOW - synced_ago}


class SyncModeTest(unittest.TestCase):
    JUST_NOW = picklist_cache.FRESH / 2
    A_WHILE = picklist_cache.FRESH * 2
    STALE = picklist_cache.FULL_EVERY

    def test_cases(self):
        cases = [
            ("never read", None, False, "full"),
            ("never read, Refresh", None, True, "full"),
            ("read moments ago", state(self.JUST_NOW, self.A_WHILE), False, "none"),
            ("read a while ago", state(self.A_WHILE, self.A_WHILE), False, "changes"),
            ("whole read too old", state(self.A_WHILE, self.STALE), False, "full"),
            ("Refresh", state(self.JUST_NOW, self.A_WHILE), True, "full"),
            ("Refresh right after a whole read", state(self.JUST_NOW, self.JUST_NOW), True, "none"),
        ]
        for name, row, full, expected in cases:
            with self.subTest(name):
                self.assertEqual(picklist_cache.sync_mode(row, NOW, full), expected)


def order(**over):
    return {"id": "g1", "cancelledAt": None, "closed": False, "displayFinancialStatus": "PAID",
            "displayFulfillmentStatus": "UNFULFILLED", **over}


class WorthCachingTest(unittest.TestCase):
    def test_cases(self):
        cases = [
            ("open, paid, unfulfilled", order(), True),
            ("partially refunded", order(displayFinancialStatus="PARTIALLY_REFUNDED"), True),
            ("on hold", order(displayFulfillmentStatus="ON_HOLD"), True),
            ("fulfilled", order(displayFulfillmentStatus="FULFILLED"), False),
            ("partially fulfilled", order(displayFulfillmentStatus="PARTIALLY_FULFILLED"), False),
            ("cancelled", order(cancelledAt="2026-10-25T10:00:00Z"), False),
            ("closed", order(closed=True), False),
            ("payment pending", order(displayFinancialStatus="PENDING"), False),
            ("refunded", order(displayFinancialStatus="REFUNDED"), False),
        ]
        for name, node, expected in cases:
            with self.subTest(name):
                self.assertEqual(picklist_cache.worth_caching(node), expected)


if __name__ == "__main__":
    unittest.main()

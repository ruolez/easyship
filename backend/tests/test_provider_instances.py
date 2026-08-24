import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

import providers  # noqa: E402


class FakeDb:
    """Records every statement; answers instance lookups from `rows` and
    settings reads from `settings`."""

    def __init__(self, rows, settings=None):
        self.rows = rows
        self.settings = settings or {}
        self.executed = []

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def query(self, sql, params=None, one=False):
        if "FROM provider_instances WHERE id" in sql:
            return next((r for r in self.rows if r["id"] == params[0]), None)
        if "FROM provider_instances WHERE key" in sql:
            return next((r for r in self.rows if r["key"] == params[0]), None)
        if "FROM provider_instances" in sql:
            return list(self.rows)
        return None

    def execute(self, sql, params=None, returning=False):
        self.executed.append((sql, params))
        if returning:
            return {"id": 5, "platform": params[0], "key": f"{params[0]}-5", "label": params[2]}
        return None


ROWS = [
    {"id": 1, "platform": "easyship", "key": "easyship", "label": "Easyship"},
    {"id": 4, "platform": "shipstation", "key": "shipstation", "label": "ShipStation"},
    {"id": 5, "platform": "shipstation", "key": "shipstation-5", "label": "East"},
]


def with_db(fake):
    orig = providers.db
    providers.db = fake
    return orig


class InstancesTest(unittest.TestCase):
    def test_objects_carry_key_alias_and_platform(self):
        orig = with_db(FakeDb(ROWS))
        try:
            out = [(p.name, p.label, p.platform, p.platform_label) for p in providers.instances()]
        finally:
            providers.db = orig
        self.assertEqual(out, [
            ("easyship", "Easyship", "easyship", "Easyship"),
            ("shipstation", "ShipStation", "shipstation", "ShipStation"),
            ("shipstation-5", "East", "shipstation", "ShipStation"),
        ])

    def test_unknown_key_resolves_to_none_not_easyship(self):
        orig = with_db(FakeDb(ROWS))
        try:
            self.assertEqual((providers.get_provider("shipstation-9"), providers.get_provider(None)),
                             (None, None))
        finally:
            providers.db = orig

    def test_enabled_needs_an_explicit_flag_per_instance(self):
        orig = with_db(FakeDb(ROWS, {"easyship_enabled": "true", "shipstation-5_enabled": "true"}))
        try:
            self.assertEqual([p.name for p in providers.enabled_providers()], ["easyship", "shipstation-5"])
        finally:
            providers.db = orig


class CreateInstanceTest(unittest.TestCase):
    def test_unknown_platform_is_rejected(self):
        orig = with_db(FakeDb(ROWS))
        try:
            with self.assertRaises(ValueError):
                providers.create_instance("fedex", "Main")
        finally:
            providers.db = orig

    def test_blank_and_duplicate_labels_are_rejected(self):
        orig = with_db(FakeDb(ROWS))
        try:
            for label in ("", "   ", "east", "x" * 61):
                with self.assertRaises(ValueError, msg=label):
                    providers.create_instance("shipstation", label)
        finally:
            providers.db = orig

    def test_key_is_platform_dash_id(self):
        fake = FakeDb(ROWS)
        orig = with_db(fake)
        try:
            row = providers.create_instance("shipstation", "  West ")
        finally:
            providers.db = orig
        self.assertEqual(row, {"id": 5, "platform": "shipstation", "key": "shipstation-5", "label": "West"})


class RenameInstanceTest(unittest.TestCase):
    def test_rename_keeps_the_key_and_allows_its_own_label(self):
        fake = FakeDb(ROWS)
        orig = with_db(fake)
        try:
            row = providers.rename_instance(5, "east")
        finally:
            providers.db = orig
        self.assertEqual(row, {"id": 5, "platform": "shipstation", "key": "shipstation-5", "label": "east"})

    def test_missing_instance(self):
        orig = with_db(FakeDb(ROWS))
        try:
            with self.assertRaises(LookupError):
                providers.rename_instance(99, "x")
        finally:
            providers.db = orig


class DeleteInstanceTest(unittest.TestCase):
    def test_refused_while_enabled(self):
        fake = FakeDb(ROWS, {"shipstation-5_enabled": "true"})
        orig = with_db(fake)
        try:
            with self.assertRaises(providers.InstanceEnabled):
                providers.delete_instance(5)
        finally:
            providers.db = orig
        self.assertEqual(fake.executed, [])

    def test_removes_only_that_instance_prefix_and_scrubs_user_allow_lists(self):
        fake = FakeDb(ROWS, {"shipstation-5_enabled": "false"})
        orig = with_db(fake)
        try:
            providers.delete_instance(5)
        finally:
            providers.db = orig
        params = [p for _, p in fake.executed]
        self.assertEqual(params, [(5,), ("shipstation-5_",), ("shipstation-5", "shipstation-5")])
        self.assertIn("starts_with(key, %s)", fake.executed[1][0])
        self.assertNotIn("LIKE", fake.executed[1][0])


if __name__ == "__main__":
    unittest.main()

import json
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

    def __init__(self, rows, settings=None, users=None):
        self.rows = rows
        self.settings = settings or {}
        self.users = users or []
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
        if "FROM users" in sql:
            return [u for u in self.users if u.get("is_active", True) and u.get("role", "user") != "admin"]
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

    def test_refused_while_it_is_a_users_only_account(self):
        fake = FakeDb(ROWS, {}, users=[user(7, "pat", ["shipstation-5"]), user(8, "sam", None)])
        orig = with_db(fake)
        try:
            with self.assertRaises(providers.InstanceInUse) as ctx:
                providers.delete_instance(5)
        finally:
            providers.db = orig
        self.assertEqual((fake.executed, "pat" in str(ctx.exception)), ([], True))

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


def user(uid, username, allowed, role="user", is_active=True):
    return {"id": uid, "username": username, "allowed_providers": allowed, "role": role, "is_active": is_active}


class SetInstanceUsersTest(unittest.TestCase):
    """Assigning from the account's own card: only the users whose list must
    change are written, and nobody can be left without an account."""

    def run_case(self, users, user_ids, instance_id=5, rows=ROWS):
        fake = FakeDb(rows, users=users)
        orig = with_db(fake)
        try:
            result = providers.set_instance_users(instance_id, user_ids)
        finally:
            providers.db = orig
        return result["user_ids"], [(uid, p and json.loads(p)) for p, uid in (x[1] for x in fake.executed)]

    def test_unrestricted_user_kept_assigned_is_not_written(self):
        self.assertEqual(self.run_case([user(7, "pat", None)], [7]), ([7], []))

    def test_restricted_user_gains_the_account_in_creation_order(self):
        self.assertEqual(self.run_case([user(7, "pat", ["easyship"])], [7]),
                         ([7], [(7, ["easyship", "shipstation-5"])]))

    def test_completing_the_set_collapses_to_unrestricted(self):
        self.assertEqual(self.run_case([user(7, "pat", ["easyship", "shipstation"])], [7]),
                         ([7], [(7, None)]))

    def test_unrestricted_user_excluded_gets_every_other_account(self):
        self.assertEqual(self.run_case([user(7, "pat", None)], []),
                         ([], [(7, ["easyship", "shipstation"])]))

    def test_restricted_user_excluded_loses_only_this_account(self):
        self.assertEqual(self.run_case([user(7, "pat", ["easyship", "shipstation-5"])], []),
                         ([], [(7, ["easyship"])]))

    def test_excluded_user_already_without_the_account_is_not_written(self):
        self.assertEqual(self.run_case([user(7, "pat", ["easyship"])], []), ([], []))

    def test_leaving_a_user_with_no_account_is_refused_before_any_write(self):
        fake = FakeDb(ROWS, users=[user(7, "pat", ["shipstation-5"]), user(8, "sam", ["easyship"])])
        orig = with_db(fake)
        try:
            with self.assertRaises(ValueError) as ctx:
                providers.set_instance_users(5, [8])
        finally:
            providers.db = orig
        self.assertEqual((fake.executed, "pat" in str(ctx.exception)), ([], True))

    def test_only_account_in_the_registry_cannot_be_unassigned(self):
        fake = FakeDb([ROWS[0]], users=[user(7, "pat", None)])
        orig = with_db(fake)
        try:
            with self.assertRaises(ValueError):
                providers.set_instance_users(1, [])
        finally:
            providers.db = orig
        self.assertEqual(fake.executed, [])

    def test_admins_and_inactive_users_are_never_written(self):
        users = [user(1, "root", None, role="admin"), user(9, "old", ["easyship"], is_active=False)]
        self.assertEqual((self.run_case(users, [1, 9]), self.run_case(users, [])), (([], []), ([], [])))

    def test_string_and_duplicate_ids_behave_like_ints(self):
        self.assertEqual(self.run_case([user(7, "pat", ["easyship"])], ["7", 7]),
                         ([7], [(7, ["easyship", "shipstation-5"])]))

    def test_unknown_instance(self):
        fake = FakeDb(ROWS, users=[user(7, "pat", None)])
        orig = with_db(fake)
        try:
            with self.assertRaises(LookupError):
                providers.set_instance_users(99, [7])
        finally:
            providers.db = orig
        self.assertEqual(fake.executed, [])


if __name__ == "__main__":
    unittest.main()

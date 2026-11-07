import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

import dbadmin  # noqa: E402

NUMBER = "10222026191"
QUOTATION_ID = 555


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = conn.rowcount

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        if self.conn.fail:
            raise RuntimeError(self.conn.fail)
        self.conn.statements.append((" ".join(sql.split()), params))


class FakeConn:
    def __init__(self, rowcount=1, fail=None):
        self.rowcount, self.fail = rowcount, fail
        self.statements, self.events = [], []

    def cursor(self, **_):
        return FakeCursor(self)

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")


class DeleteQuotationStatusTest(unittest.TestCase):
    def run_with(self, conn, number=NUMBER, quotation_id=QUOTATION_ID):
        orig, dbadmin._connect = dbadmin._connect, lambda: conn
        try:
            return dbadmin.delete_quotation_status(number, quotation_id)
        finally:
            dbadmin._connect = orig

    def test_deletes_only_rows_matching_both_the_number_and_the_quotation_id(self):
        conn = FakeConn(rowcount=1)
        deleted = self.run_with(conn, number=f" {NUMBER} ")
        self.assertEqual((deleted, conn.statements, conn.events), (1, [(
            "DELETE FROM QuotationsStatus WHERE LTRIM(RTRIM(QuotationNumber)) = %s AND LTRIM(RTRIM(Dop1)) = %s",
            (NUMBER, str(QUOTATION_ID)))], ["commit", "close"]))

    def test_reports_zero_when_no_row_is_left(self):
        conn = FakeConn(rowcount=0)
        self.assertEqual((self.run_with(conn), conn.events), (0, ["commit", "close"]))

    def test_a_failing_delete_rolls_back_and_raises_dbadmin_error(self):
        conn = FakeConn(fail="deadlock victim")
        with self.assertRaisesRegex(dbadmin.DbAdminError, f"{NUMBER}.*deadlock victim"):
            self.run_with(conn)
        self.assertEqual(conn.events, ["rollback", "close"])


if __name__ == "__main__":
    unittest.main()

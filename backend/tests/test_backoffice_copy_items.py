import sys
import types
import unittest
from decimal import Decimal

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

import backoffice  # noqa: E402

DB_ID = 4
INVENTORY_ROW = {
    "ProductID": 77010, "CateID": 3, "SubCateID": 12, "ProductSKU": "PEAR-1", "ProductUPC": " 222 ",
    "ProductDescription": "PEAR", "UnitPrice": Decimal("2.50"), "UnitCost": Decimal("1.10"),
    "ItemSize": "12CT", "ItemWeight": "1.5", "UnitID": 2, "ItemTaxID": 1, "UnitDesc": "CASE",
    "QuantOnHand": 40.0,
}


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.added = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        if self.conn.fail:
            raise RuntimeError(self.conn.fail)
        self.conn.statements.append((" ".join(sql.split()), params))
        self.added = 0 if params[0] in self.conn.present else 1

    def fetchone(self):
        return {"added": self.added}


class FakeConn:
    def __init__(self, present=(), fail=None):
        self.present, self.fail = set(present), fail
        self.statements, self.events = [], []

    def cursor(self, **_):
        return FakeCursor(self)

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")


EXPECTED_SQL = (
    "DECLARE @added INT = 0; "
    "IF NOT EXISTS (SELECT 1 FROM Items_tbl WITH (UPDLOCK, HOLDLOCK) WHERE ProductUPC = %s) "
    "BEGIN INSERT INTO Items_tbl (CateID, SubCateID, ProductSKU, ProductUPC, ProductDescription, UnitPrice, "
    "UnitCost, ItemSize, ItemWeight, UnitID, ItemTaxID, SPPromoted) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s); SET @added = 1; END; "
    "SELECT @added AS added"
)


class CopyItemsTest(unittest.TestCase):
    def run_with(self, conn, rows):
        orig, backoffice._connect = backoffice._connect, lambda db_id: conn
        try:
            return backoffice.copy_items(DB_ID, rows)
        finally:
            backoffice._connect = orig

    def test_copies_the_inventory_columns_with_sppromoted_off_in_one_transaction(self):
        conn = FakeConn()
        added = self.run_with(conn, [INVENTORY_ROW])
        self.assertEqual((added, conn.statements, conn.events), (["222"], [(EXPECTED_SQL, (
            "222",
            3, 12, "PEAR-1", "222", "PEAR", Decimal("2.50"), Decimal("1.10"), "12CT", "1.5", 2, 1, 0,
        ))], ["commit", "close"]))

    def test_text_longer_than_the_backoffice_columns_is_cut_to_fit(self):
        conn = FakeConn()
        long_row = {**INVENTORY_ROW, "ProductSKU": "S" * 25, "ProductDescription": "D" * 60,
                    "ItemSize": "Z" * 12, "ItemWeight": "W" * 12}
        self.run_with(conn, [long_row])
        params = conn.statements[0][1]
        self.assertEqual((params[3], params[5], params[8], params[9]), ("S" * 20, "D" * 50, "Z" * 10, "W" * 10))

    def test_a_barcode_backoffice_already_has_is_not_reported_as_added(self):
        conn = FakeConn(present={"222"})
        other = {**INVENTORY_ROW, "ProductUPC": "333"}
        self.assertEqual((self.run_with(conn, [INVENTORY_ROW, other]), conn.events), (["333"], ["commit", "close"]))

    def test_a_failing_insert_rolls_back_and_raises_backoffice_error(self):
        conn = FakeConn(fail="String or binary data would be truncated")
        with self.assertRaises(backoffice.BackofficeError) as caught:
            self.run_with(conn, [INVENTORY_ROW])
        self.assertEqual((str(caught.exception), conn.events), (
            "Could not add products to BackOffice: String or binary data would be truncated", ["rollback", "close"]))

    def test_nothing_to_copy_does_not_connect(self):
        self.assertEqual(backoffice.copy_items(DB_ID, []), [])


if __name__ == "__main__":
    unittest.main()

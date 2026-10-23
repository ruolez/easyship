import json
import sys
import types
import unittest
from datetime import datetime

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp"))

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import backoffice  # noqa: E402
import settings_api  # noqa: E402


class UserSectionsTest(unittest.TestCase):
    def run_case(self, role, stored):
        orig = auth.db.query
        auth.db.query = lambda sql, params=None, one=False: {"allowed_sections": stored}
        try:
            return auth.user_sections(7, role)
        finally:
            auth.db.query = orig

    def test_access_per_role_and_grant(self):
        self.assertEqual(
            [self.run_case("admin", None), self.run_case("user", None), self.run_case("user", []),
             self.run_case("user", ["picklists", "bogus"])],
            [["picklists"], [], [], ["picklists"]])


class SetUserSectionsRouteTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(settings_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess.update({"user_id": 1, "role": "admin"})
        self.role = "user"
        self.writes = []
        self._orig = (settings_api.db.query, settings_api.db.execute)
        settings_api.db.query = lambda sql, params=None, one=False: {"id": 7, "role": self.role}
        settings_api.db.execute = lambda sql, params=None, returning=False: (
            self.writes.append(params) if sql.lstrip().startswith("UPDATE users") else None)

    def tearDown(self):
        settings_api.db.query, settings_api.db.execute = self._orig

    def put(self, sections):
        return self.client.put("/api/users/7/sections", json={"allowed_sections": sections})

    def test_grant_and_revoke(self):
        granted = self.put(["picklists", "nope"])
        revoked = self.put([])
        self.assertEqual(
            (granted.get_json()["allowed_sections"], revoked.get_json()["allowed_sections"], self.writes),
            (["picklists"], [], [(json.dumps(["picklists"]), 7), (None, 7)]))

    def test_admin_users_cannot_be_restricted(self):
        self.role = "admin"
        self.assertEqual((self.put([]).status_code, self.writes), (400, []))


class PicklistSettingsValidationTest(unittest.TestCase):
    def test_errors(self):
        cases = [
            ({}, None),
            ({"picklist_quote_status": "2", "picklist_number_tag": "19", "picklist_title_prefix": "PL"}, None),
            ({"picklist_quote_status": "x"}, "Pick list quotation status must be a whole number"),
            ({"picklist_number_tag": "12345"}, "Pick list number tag must be 1–4 digits"),
            ({"picklist_number_tag": "A1"}, "Pick list number tag must be 1–4 digits"),
        ]
        self.assertEqual([settings_api._picklist_settings_error(d) for d, _ in cases],
                         [expected for _, expected in cases])


class QuotationDetailRowTest(unittest.TestCase):
    def test_values_follow_the_column_list(self):
        exp = datetime(2022, 11, 1)
        item = {"CateID": 2, "SubCateID": 5, "UnitDesc": "EACH", "ProductID": 1010, "ProductSKU": "AP-1",
                "ProductUPC": "111", "ProductDescription": "APPLE " * 20, "ItemSize": "12CT",
                "UnitPrice": 2.5, "UnitCost": 1.25, "ItemWeight": "1.5", "ExpDate": exp, "ItemTaxID": 2}
        row = backoffice.quotation_detail_row(555, item, 4)
        columns = [c.strip() for c in backoffice.QUOTATION_DETAIL_COLUMNS.split(",")]
        self.assertEqual(dict(zip(columns, row)), {
            "QuotationID": 555, "CateID": 2, "SubCateID": 5, "UnitDesc": "EACH", "UnitQty": 1,
            "ProductID": 1010, "ProductSKU": "AP-1", "ProductUPC": "111",
            "ProductDescription": ("APPLE " * 20)[:50], "ItemSize": "12CT",
            "ExpDate": exp, "ReasonID": None, "LineMessage": "",
            "UnitPrice": 2.5, "OriginalPrice": 2.5, "RememberPrice": 0, "UnitCost": 1.25,
            "Discount": 0, "ds_Percent": 0, "Qty": 4, "ItemWeight": None,
            "ExtendedPrice": 10.0, "ExtendedDisc": 0, "ExtendedCost": 5.0,
            "PromotionID": 0, "PromotionLine": 0, "ActExtendedPrice": 10.0,
            "SPPromoted": 0, "SPPromotionDescription": "",
            "Taxable": 0, "ItemTaxID": 2, "Catch": 0, "Flag": 0,
        })

    def test_missing_unit_and_size_are_blank_not_null(self):
        row = backoffice.quotation_detail_row(555, {"ProductID": 1}, 1)
        columns = [c.strip() for c in backoffice.QUOTATION_DETAIL_COLUMNS.split(",")]
        values = dict(zip(columns, row))
        self.assertEqual((values["UnitDesc"], values["ItemSize"], values["ItemTaxID"], values["ExpDate"]),
                         ("", "", 0, None))


if __name__ == "__main__":
    unittest.main()

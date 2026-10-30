import random
import unittest

from barcodes import barcode_key, case_lookup

UPC_A = "012345678905"


class BarcodeKeyTest(unittest.TestCase):
    def test_upc_a_ean13_and_gtin14_forms_share_one_key(self):
        forms = [UPC_A, "0" + UPC_A, "00" + UPC_A, UPC_A.lstrip("0")]
        self.assertEqual({barcode_key(f) for f in forms}, {"12345678905"})

    def test_scanner_suffixes_and_aim_prefix_are_ignored(self):
        for raw in (UPC_A + "\r", UPC_A + "\n", "\t" + UPC_A, "]E0" + UPC_A, " ]E0" + UPC_A + "\r\n"):
            self.assertEqual(barcode_key(raw), "12345678905", repr(raw))

    def test_text_codes_ignore_case_but_keep_zeros(self):
        self.assertEqual(barcode_key(" ab-007x "), "AB-007X")

    def test_all_zero_and_empty_codes(self):
        self.assertEqual([barcode_key("000"), barcode_key(""), barcode_key(None), barcode_key("\r\n")],
                         ["0", "", "", ""])

    def test_leading_zeros_never_change_a_numeric_key(self):
        rng = random.Random(7)
        for _ in range(200):
            digits = str(rng.randrange(1, 10**14))
            pads = rng.randrange(0, 6)
            self.assertEqual(barcode_key("0" * pads + digits), barcode_key(digits))

    def test_key_is_idempotent(self):
        for raw in (UPC_A, "]E0" + UPC_A, "abc", "0", "x\x1dy"):
            self.assertEqual(barcode_key(barcode_key(raw)), barcode_key(raw))

    def test_different_products_keep_different_keys(self):
        self.assertNotEqual(barcode_key("012345678905"), barcode_key("012345678912"))


class CaseLookupTest(unittest.TestCase):
    def test_rows_become_normalized_lookup_and_bad_rows_are_dropped(self):
        rows = [
            {"barcode": "10012345678902", "unit_barcode": UPC_A, "quantity": 12},
            {"barcode": "777", "unit_barcode": "", "quantity": 6},
            {"barcode": "888", "unit_barcode": UPC_A, "quantity": 0},
        ]
        self.assertEqual(case_lookup(rows), {"10012345678902": ("12345678905", 12)})


if __name__ == "__main__":
    unittest.main()

import io
import unittest
from datetime import datetime, timezone

from pypdf import PdfReader

import manifest_sheet

ORIGIN = {"origin_company": "Acme Supply", "origin_address1": "1 Main St", "origin_address2": "",
          "origin_city": "Saint Cloud", "origin_state": "MN", "origin_zip": "56301",
          "origin_phone": "3205550100"}


def rows(count, boxes_per_order=2):
    return [{
        "order": f"SD{100 + i // boxes_per_order}",
        "box": f"{i % boxes_per_order + 1}/{boxes_per_order}",
        "tracking": f"SP5555550{i:04d}",
        "recipient": f"Customer (#{i}) \\ Ltd",
        "city": "Minneapolis, MN",
        "weight": 2.5,
    } for i in range(count)]


def pdf_text(data):
    reader = PdfReader(io.BytesIO(data))
    return len(reader.pages), "\n".join(page.extract_text() for page in reader.pages)


class SummarySheetTest(unittest.TestCase):
    def test_lists_every_package_with_totals_and_signature_line(self):
        data = manifest_sheet.summary_sheet_pdf("North — pickup manifest", "11/02/2026", ORIGIN, rows(3, 3))
        pages, text = pdf_text(data)
        self.assertEqual(pages, 1)
        for needle in ("SP55555500000", "SP55555500001", "SP55555500002", "Total packages: 3",
                       "Total weight: 7.5 lb", "3 package(s) in 1 order(s)", "Driver signature",
                       "Acme Supply", "Saint Cloud, MN 56301", "Customer (#0) \\ Ltd", "North — pickup manifest"):
            self.assertIn(needle, text)

    def test_long_list_paginates_and_keeps_every_package(self):
        packages = rows(100)
        pages, text = pdf_text(manifest_sheet.summary_sheet_pdf("T", "11/02/2026", ORIGIN, packages))
        self.assertEqual(pages, 3)
        self.assertEqual([p["tracking"] in text for p in packages], [True] * len(packages))
        self.assertIn("Page 3 of 3", text)

    def test_footer_moves_to_its_own_page_when_the_last_page_is_full(self):
        full = manifest_sheet.ROWS_FIRST_PAGE
        pages, text = pdf_text(manifest_sheet.summary_sheet_pdf("T", "11/02/2026", ORIGIN, rows(full)))
        self.assertEqual((pages, f"Total packages: {full}" in text), (2, True))


class SheetRowTest(unittest.TestCase):
    def test_row_from_a_shipment(self):
        row = {"box_number": 2, "box_total": 3, "tracking_number": "SPX2", "total_weight_lb": 4.25,
               "destination": {"contact": "Jane", "company": "", "city": "Fargo", "state": "ND"}}
        self.assertEqual(manifest_sheet.sheet_row(row, "SD9"), {
            "order": "SD9", "box": "2/3", "tracking": "SPX2", "recipient": "Jane",
            "city": "Fargo, ND", "weight": 4.25})

    def test_central_date(self):
        self.assertEqual(manifest_sheet.central_date(datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)),
                         "11/01/2026")


if __name__ == "__main__":
    unittest.main()

import sys
import types
import unittest
from decimal import Decimal

# backoffice imports pymssql (absent on the host) and db at module load; stub both.
sys.modules.setdefault("pymssql", types.SimpleNamespace(connect=lambda **k: None))
sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

import backoffice  # noqa: E402


def invoice(**over):
    base = {"InvoiceSubtotal": Decimal("120.00"), "TotalDiscounts": Decimal("20.00"),
            "TotalTaxes": Decimal("8.25"), "OtherCharges": Decimal("3.00")}
    base.update(over)
    return base


def detail(sku, shipped, ordered, price, unit_cost=None, avr=None, last=None, item_cost=None):
    return {"ProductSKU": sku, "ProductDescription": sku.lower(), "QtyShipped": shipped,
            "QtyOrdered": ordered, "UnitPrice": Decimal(price),
            "UnitCost": Decimal(unit_cost) if unit_cost is not None else None,
            "AvrCost": Decimal(avr) if avr is not None else None,
            "LastCost": Decimal(last) if last is not None else None,
            "ItemUnitCost": Decimal(item_cost) if item_cost is not None else None}


class EconomicsFromInvoiceTest(unittest.TestCase):
    """Revenue is the invoice subtotal less discounts — shipping is billed as
    a line item, taxes and the label cost written back into ShippingCost are
    never revenue. Line cost is the first positive of the invoice line's
    UnitCost, then the item's average, last and unit cost."""

    def test_subtotal_less_discounts_and_line_costs(self):
        lines = [detail("A", 2.0, 2.0, "30.00", unit_cost="10.00"),
                 detail("B", 1.0, 3.0, "60.00", unit_cost="25.00")]
        self.assertEqual(backoffice.economics_from_invoice(invoice(), lines), {
            "available": True, "reason": None, "reason_text": None, "currency": "USD",
            "items_subtotal": 100.0, "shipping_paid": 0.0, "revenue": 100.0,
            "items_cost": 45.0, "line_count": 2, "missing_cost": []})

    def test_cost_falls_through_zero_and_null_to_the_item_master(self):
        lines = [detail("A", 1.0, 1.0, "10.00", unit_cost="0", avr="4.00"),
                 detail("B", 1.0, 1.0, "10.00", unit_cost=None, avr=None, last="3.00"),
                 detail("C", 1.0, 1.0, "10.00", item_cost="2.00"),
                 detail("D", 1.0, 1.0, "10.00")]
        econ = backoffice.economics_from_invoice(invoice(), lines)
        self.assertEqual((econ["items_cost"], econ["missing_cost"]),
                         (18.0, [{"description": "d", "sku": "D", "quantity": 1}]))

    def test_ordered_quantity_is_used_when_nothing_shipped_yet(self):
        econ = backoffice.economics_from_invoice(invoice(), [detail("A", None, 3.0, "10.00", unit_cost="1.00")])
        self.assertEqual((econ["line_count"], econ["items_cost"]), (1, 3.0))

    def test_missing_subtotal_is_rebuilt_from_the_lines(self):
        lines = [detail("A", 2.0, 2.0, "30.00", unit_cost="10.00")]
        econ = backoffice.economics_from_invoice(invoice(InvoiceSubtotal=None, TotalDiscounts=None), lines)
        self.assertEqual(econ["revenue"], 60.0)


if __name__ == "__main__":
    unittest.main()

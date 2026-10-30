"""Barcode matching for order verification.

Scans and stored barcodes are compared by a normalized key: control
characters (the scanner's Enter/Tab, GS) and an AIM symbology prefix such as
"]E0" are dropped, number-only codes ignore leading zeros (so a UPC-A, its
EAN-13 form and a UPC saved without its leading zero all match), and text
codes ignore case.
"""
import re

_AIM_PREFIX = re.compile(r"^\][A-Za-z][0-9A-Za-z]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DIGITS = re.compile(r"[0-9]+")


def barcode_key(code):
    text = _CONTROL.sub("", str(code or "")).strip()
    text = _AIM_PREFIX.sub("", text, count=1).strip()
    if _DIGITS.fullmatch(text):
        return text.lstrip("0") or "0"
    return text.upper()


def case_lookup(rows):
    """case_barcodes rows → {case key: (unit key, units per scan)}."""
    return {
        barcode_key(r["barcode"]): (barcode_key(r["unit_barcode"]), int(r["quantity"]))
        for r in rows
        if barcode_key(r["barcode"]) and barcode_key(r["unit_barcode"]) and int(r["quantity"]) > 0
    }

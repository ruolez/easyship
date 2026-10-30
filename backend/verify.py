"""Order verification: the packer scans every product of an order before it
ships, so the right products in the right quantities go in the box.

The pure functions below turn an order's lines (Shopify or a BackOffice
quotation) into verification lines and judge each scan; verify_store keeps
them in Postgres and verify_api serves the Verify page.

A line is keyed by its barcode (barcodes.barcode_key), so order lines sharing
a barcode are added together. A line whose product name starts with one of
the source's skip prefixes (shipping, shipping protection…) never needs
scanning; a line without a barcode is confirmed with a tap.
"""
import re

import picklists
from barcodes import barcode_key

SKIP_PREFIX_KEYS = {"shopify": "verify_skip_prefixes_shopify", "backoffice": "verify_skip_prefixes_backoffice"}
CHECKED_TAG = "Checked"

SCAN_OK = "ok"
SCAN_LINE_DONE = "line_done"
SCAN_ORDER_DONE = "order_done"
SCAN_OVER = "over"
SCAN_CASE_OVER = "case_over"
SCAN_WRONG = "wrong"
SCAN_ERRORS = (SCAN_OVER, SCAN_CASE_OVER, SCAN_WRONG)


def parse_prefixes(raw):
    """Skip prefixes as typed in Settings: one per line or comma separated."""
    seen, out = set(), []
    for part in re.split(r"[\n,]", raw or ""):
        prefix = part.strip()
        if prefix and prefix.lower() not in seen:
            seen.add(prefix.lower())
            out.append(prefix)
    return out


def skip_prefix(title, prefixes):
    name = (title or "").strip().lower()
    for prefix in prefixes:
        if name.startswith(prefix.lower()):
            return prefix
    return None


def _qty(value):
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def build_lines(items, prefixes):
    """Order items ({barcode, title, sku, qty}) → verification lines, in order
    of first appearance. Items with the same barcode become one line."""
    lines = {}
    for item in items:
        qty = _qty(item.get("qty"))
        if qty <= 0:
            continue
        title = (item.get("title") or "").strip()
        barcode = (item.get("barcode") or "").strip()
        prefix = skip_prefix(title, prefixes)
        if prefix:
            key = "skip:" + title.upper()
        elif barcode_key(barcode):
            key = barcode_key(barcode)
        else:
            key = "item:" + title.upper()
        if key in lines:
            lines[key]["qty_required"] += qty
            continue
        lines[key] = {
            "key": key,
            "barcode": "" if key.startswith("item:") else barcode,
            "title": title,
            "sku": (item.get("sku") or "").strip(),
            "qty_required": qty,
            "qty_verified": 0,
            "scanned": False,
            "forced": False,
            "skip_reason": f"Name starts with “{prefix}”" if prefix else None,
        }
    return list(lines.values())


def shopify_items(node):
    """A Shopify order node (picklist line fields) → items still to ship.
    Removed, refunded and fulfilled units and non-shipping lines are left out."""
    items = []
    for li in (node.get("lineItems") or {}).get("nodes") or []:
        if li.get("requiresShipping") is False:
            continue
        items.append({
            "title": li.get("title") or "",
            "sku": li.get("sku") or "",
            "barcode": (li.get("variant") or {}).get("barcode") or "",
            "qty": picklists.pickable_qty(li),
        })
    return items


def quotation_items(rows):
    return [{"title": r.get("ProductDescription"), "sku": r.get("ProductSKU"),
             "barcode": r.get("ProductUPC"), "qty": r.get("Qty")} for r in rows]


def invoice_items(rows):
    return [{"title": r.get("ProductDescription"), "sku": r.get("ProductSKU"), "barcode": r.get("ProductUPC"),
             "qty": r.get("QtyShipped") or r.get("QtyOrdered")} for r in rows]


def is_active(line):
    return not line.get("skip_reason")


def needs_tap(line):
    """No barcode to scan: confirmed with a tap instead."""
    return is_active(line) and line["key"].startswith("item:")


def remaining(line):
    return max(0, line["qty_required"] - line["qty_verified"]) if is_active(line) else 0


def is_complete(lines):
    return all(remaining(line) == 0 for line in lines)


def progress(lines):
    active = [line for line in lines if is_active(line)]
    return {
        "units_required": sum(line["qty_required"] for line in active),
        "units_verified": sum(min(line["qty_verified"], line["qty_required"]) for line in active),
        "lines_total": len(active),
        "lines_done": sum(1 for line in active if remaining(line) == 0),
    }


def judge_scan(lines, code, cases):
    """What one scan does. A product barcode counts one unit; a case barcode
    (cases: {case key: (unit key, units)}) counts its units against the
    product's line. Returns {result, line_key, units, case_units, left}."""
    key = barcode_key(code)
    by_key = {line["key"]: line for line in lines if is_active(line) and not needs_tap(line)}
    line, units, case_units = by_key.get(key), 1, None
    if line is None and key in cases:
        unit_key, case_units = cases[key]
        line, units = by_key.get(unit_key), case_units
    if line is None:
        return {"result": SCAN_WRONG, "line_key": None, "units": 0, "case_units": case_units, "left": None}
    left = remaining(line)
    outcome = {"line_key": line["key"], "case_units": case_units, "left": left}
    if left == 0:
        return {**outcome, "result": SCAN_OVER, "units": 0}
    if units > left:
        return {**outcome, "result": SCAN_CASE_OVER, "units": 0}
    others_done = all(remaining(other) == 0 for other in lines if other is not line)
    if units < left:
        result = SCAN_OK
    else:
        result = SCAN_ORDER_DONE if others_done else SCAN_LINE_DONE
    return {**outcome, "result": result, "units": units}


def count_error(line, qty):
    """Why a typed count (or Check all) cannot be applied to a line, or None.
    One unit must have been scanned first — that proves it is the right product."""
    if not is_active(line):
        return "This item does not need verifying"
    if needs_tap(line):
        return "This item has no barcode — confirm it instead"
    if not line["scanned"]:
        return "Scan one unit of this item first"
    if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
        return "Enter a whole number of at least 1"
    if qty > line["qty_required"]:
        return f"Only {line['qty_required']} ordered"
    return None


def merge_lines(old_lines, new_lines):
    """The order's lines re-read, keeping what was already verified per key —
    never more than the new quantity, so a reduced line stays done and an
    increased one asks for the extra units."""
    old = {line["key"]: line for line in old_lines}
    merged = []
    for line in new_lines:
        prev = old.get(line["key"])
        if prev:
            line = {**line, "qty_verified": min(prev["qty_verified"], line["qty_required"]),
                    "scanned": prev["scanned"], "forced": prev["forced"]}
        merged.append(line)
    return merged


def unverified_items(verified_lines, current_lines):
    """Current order lines needing more units than were verified — the order
    changed after it was checked, or it was never finished."""
    done = {line["key"]: line["qty_verified"] for line in verified_lines}
    return [
        {"title": line["title"], "barcode": line["barcode"], "qty_required": line["qty_required"],
         "qty_verified": min(done.get(line["key"], 0), line["qty_required"])}
        for line in current_lines
        if is_active(line) and line["qty_required"] > done.get(line["key"], 0)
    ]


def shopify_order_key(store_id, order_name):
    return f"shopify:{store_id}:{(order_name or '').strip().lstrip('#').upper()}"


def backoffice_order_key(quotation_number):
    return f"backoffice:{(quotation_number or '').strip()}"


def detect_store(number, stores):
    """The Shopify store whose order-number prefix the scanned number starts
    with (longest prefix wins, case and a leading # ignored), or None. Raises
    ValueError when two stores share the longest matching prefix."""
    bare = (number or "").strip().lstrip("#").upper()
    matches = [s for s in stores if (s.get("prefix") or "").strip()
               and bare.startswith((s["prefix"] or "").strip().lstrip("#").upper())]
    if not matches:
        return None
    longest = max(len((s["prefix"] or "").strip().lstrip("#")) for s in matches)
    best = [s for s in matches if len((s["prefix"] or "").strip().lstrip("#")) == longest]
    if len(best) > 1:
        raise ValueError("More than one Shopify store uses this order-number prefix")
    return best[0]

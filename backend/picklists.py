"""Pick lists: batches of Shopify orders whose products, grouped by barcode,
become one BackOffice quotation the warehouse picks from.

An order is eligible while it is fully paid, not on hold, completely
unfulfilled and not on another live pick list. The pure functions shape and
judge orders; the DB helpers reserve orders so no order lands on two lists.
"""
import re

import psycopg2
import psycopg2.extras

import db
import order_gate
import order_risk
import po_box
import profit
from shopify_client import WEIGHT_TO_LB

STATUSES = ("creating", "active", "cancelled")
_ADDRESS_NOISE = re.compile(r"[.,#']")


class ReservationConflict(Exception):
    """Some orders were put on another pick list in the meantime."""

    def __init__(self, order_names):
        super().__init__("Already on another pick list: " + ", ".join(order_names))
        self.order_names = order_names


def _address_key(addr):
    def norm(value):
        return " ".join(_ADDRESS_NOISE.sub(" ", str(value or "")).lower().split())
    return (norm(addr.get("address1")), norm(addr.get("address2")), norm(addr.get("city")),
            norm(addr.get("provinceCode")), norm(addr.get("zip"))[:5], norm(addr.get("countryCodeV2")))


def addresses_match(billing, shipping):
    """Billing and ship-to are the same place — case, punctuation, spacing and
    ZIP+4 aside. None when either address is missing."""
    if not billing or not shipping:
        return None
    return _address_key(billing) == _address_key(shipping)


def from_shopify_order(node, store_id):
    """A raw GraphQL order node → what the page and the builder need. Only
    lines that still need shipping count: fulfilled, removed and non-shipping
    items (tips, gift cards) are left out."""
    gate = order_gate.from_shopify_order(node)
    addr = node.get("shippingAddress") or {}
    customer = node.get("customer") or {}
    risk = node.get("risk") or {}
    lines = []
    for li in (node.get("lineItems") or {}).get("nodes") or []:
        qty = li.get("unfulfilledQuantity") or 0
        if qty <= 0 or li.get("requiresShipping") is False:
            continue
        lines.append({
            "title": li.get("title") or "",
            "sku": li.get("sku") or "",
            "barcode": ((li.get("variant") or {}).get("barcode") or "").strip(),
            "qty": qty,
        })
    return {
        "store_id": store_id,
        "id": node["id"],
        "name": node.get("name") or "",
        "created_at": (node.get("createdAt") or "")[:10],
        "cancelled": bool(node.get("cancelledAt")),
        "fulfillment_status": node.get("displayFulfillmentStatus"),
        "financial_status": node.get("displayFinancialStatus"),
        "fully_paid": gate["fully_paid"],
        "on_hold": gate["on_hold"],
        "tags": node.get("tags") or [],
        "customer": customer.get("displayName") or addr.get("name") or "",
        "customer_tags": customer.get("tags") or [],
        "ship_address1": addr.get("address1") or "",
        "ship_address2": addr.get("address2") or "",
        "ship_city": addr.get("city") or "",
        "ship_zip": addr.get("zip") or "",
        "ship_state": addr.get("provinceCode") or "",
        "ship_country": addr.get("countryCodeV2") or "",
        "po_box": po_box.is_po_box(addr),
        "billing_matches_shipping": addresses_match(node.get("billingAddress"), node.get("shippingAddress")),
        "total": ((node.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount"),
        "shipping_method": (node.get("shippingLine") or {}).get("title") or "",
        "risk_level": order_risk.risk_level(risk),
        "risk_recommendation": risk.get("recommendation"),
        "lines": lines,
        "units": sum(line["qty"] for line in lines),
    }


def _amount(money_set):
    return ((money_set or {}).get("shopMoney") or {}).get("amount")


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def order_products(node):
    """The order's products for the order window: everything still on the
    order (removed items are left out), with what is left to ship, the price
    paid after discounts and the margin over the Shopify unit cost."""
    products = []
    for li in (node.get("lineItems") or {}).get("nodes") or []:
        qty = li.get("currentQuantity") or 0
        if qty <= 0:
            continue
        variant = li.get("variantTitle") or ""
        original = _amount(li.get("originalUnitPriceSet"))
        price = _amount(li.get("discountedUnitPriceAfterAllDiscountsSet")) or original
        cost = ((((li.get("variant") or {}).get("inventoryItem") or {}).get("unitCost")) or {}).get("amount")
        paid, unit_cost = _float(price), _float(cost)
        products.append({
            "title": li.get("title") or "",
            "variant": "" if variant == "Default Title" else variant,
            "sku": li.get("sku") or "",
            "barcode": ((li.get("variant") or {}).get("barcode") or "").strip(),
            "qty": qty,
            "to_ship": li.get("unfulfilledQuantity") or 0,
            "ships": li.get("requiresShipping") is not False,
            "price": price,
            "price_original": original if (_float(original) or 0) > (paid or 0) else None,
            "cost": cost,
            "margin_pct": round((paid - unit_cost) / paid * 100, 1) if paid and unit_cost is not None else None,
            "image": (li.get("image") or {}).get("url"),
        })
    return products


def order_weight_lb(node):
    """Weight of what is left to ship, from the variants' Shopify weights — the
    same figure the Ship page starts from."""
    total = 0.0
    for li in (node.get("lineItems") or {}).get("nodes") or []:
        qty = li.get("unfulfilledQuantity") or 0
        if qty <= 0 or li.get("requiresShipping") is False:
            continue
        weight = ((((li.get("variant") or {}).get("inventoryItem") or {}).get("measurement")) or {}).get("weight") or {}
        total += (_float(weight.get("value")) or 0) * WEIGHT_TO_LB.get(weight.get("unit"), 0) * qty
    return round(total, 2)


NO_THRESHOLDS = profit.parse_thresholds(None, None)


def order_profitability(economics, estimate, thresholds):
    """Revenue − product cost − estimated shipping, judged against the profit
    check's thresholds. Without a shipping estimate the profit is before
    shipping and is not judged, since it would only look better than it is."""
    if not economics.get("available"):
        return {"available": False, "reason_text": economics.get("reason_text"), "shipping_estimate": estimate}
    shipping = estimate.get("cost")
    judged = profit.evaluate(economics, shipping if shipping is not None else 0, None,
                             thresholds if shipping is not None else NO_THRESHOLDS)
    return {
        "available": True,
        "currency": economics["currency"],
        "items_subtotal": economics["items_subtotal"],
        "shipping_paid": economics["shipping_paid"],
        "revenue": economics["revenue"],
        "items_cost": economics["items_cost"],
        "missing_cost": economics["missing_cost"],
        "shipping_estimate": estimate,
        "includes_shipping": shipping is not None,
        "profit": judged["profit"],
        "margin_pct": judged["margin_pct"],
        "below_threshold": judged["below_threshold"],
        "reasons": judged["reasons"],
        "thresholds": {"min_amount": thresholds["min_amount"], "min_margin_pct": thresholds["min_margin_pct"]},
    }


def ineligible_reason(order, reserved_keys):
    """Why an order cannot go on a new pick list, or None when it can."""
    if (order["store_id"], order["id"]) in reserved_keys:
        return "Already on a pick list"
    if order["cancelled"]:
        return "Cancelled in Shopify"
    if order["on_hold"]:
        return "On hold in Shopify"
    if not order["fully_paid"]:
        return "Not fully paid"
    if order["fulfillment_status"] != "UNFULFILLED":
        return "Already fulfilled or partially fulfilled"
    if not order["lines"]:
        return "Nothing to pick"
    return None


def summary(order):
    return {key: order.get(key) for key in ("store_id", "id", "name", "customer", "ship_state", "units")}


def pick_issues(order, known_upcs):
    """The products that would leave the order off a pick list: no barcode, or
    a barcode BackOffice does not know. known_upcs None means BackOffice was
    not asked, so only missing barcodes count."""
    issues = []
    for line in order["lines"]:
        if not line["barcode"]:
            issues.append({"kind": "no_barcode", "text": f"{line['title']} has no barcode in Shopify"})
        elif known_upcs is not None and line["barcode"] not in known_upcs:
            issues.append({"kind": "unknown_barcode",
                           "text": f"{line['title']} — barcode {line['barcode']} is not in BackOffice"})
    return issues


def split_blocked(orders, known_upcs):
    """(orders that can be picked, orders left out with reasons). An order is
    left out whole when any of its products has an issue — the quotation never
    misses part of an order."""
    included, blocked = [], []
    for order in orders:
        reasons = [issue["text"] for issue in pick_issues(order, known_upcs)]
        if reasons:
            blocked.append({**summary(order), "reasons": reasons})
        else:
            included.append(order)
    return included, blocked


def parse_tag_list(raw):
    """The "tags hidden on the Pick lists page" setting → unique tags, first spelling kept."""
    seen, tags = set(), []
    for tag in str(raw or "").split(","):
        tag = tag.strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    return tags


def group_lines(orders, items):
    """One line per barcode with the total quantity across orders. `items`
    maps barcode → its BackOffice Items_tbl row."""
    groups = {}
    for order in orders:
        for line in order["lines"]:
            group = groups.setdefault(line["barcode"], {
                "barcode": line["barcode"], "qty": 0, "title": line["title"], "orders": []})
            group["qty"] += line["qty"]
            if order["name"] not in group["orders"]:
                group["orders"].append(order["name"])
    result = []
    for group in groups.values():
        item = items.get(group["barcode"]) or {}
        result.append({
            **group,
            "product_id": item.get("ProductID"),
            "sku": item.get("ProductSKU") or "",
            "description": item.get("ProductDescription") or group["title"],
        })
    return sorted(result, key=lambda g: (g["description"].lower(), g["barcode"]))


def number_prefix(when, tag):
    """Quotation numbers are MMDDYYYY + the pick-list tag + a daily counter."""
    return f"{when:%m%d%Y}{tag}"


def shopify_tag(quotation_number):
    """The tag every order of a pick list carries in Shopify while the list is live."""
    return f"picklist {quotation_number}"


# Which orders a tag pass works on. Adding retries what is not on yet; removing
# touches every order that may carry the tag (a failed add may still have landed).
TAG_TARGET_STATES = {"add": ("pending", "failed"), "remove": ("pending", "tagged", "failed", "remove_failed")}
TAG_RESULT_STATES = {"add": ("tagged", "failed"), "remove": ("removed", "remove_failed")}


def tag_targets(rows, mode):
    return [r for r in rows if r["tag_state"] in TAG_TARGET_STATES[mode]]


# ---------- Postgres ----------

def active_order_keys(store_ids):
    rows = db.query(
        "SELECT store_id, order_gid FROM picklist_orders WHERE active AND store_id = ANY(%s)",
        (list(store_ids),),
    )
    return {(r["store_id"], r["order_gid"]) for r in rows or []}


def reserve(user_id, db_id, customer, rep, orders, lines):
    """Record a pick list in `creating` state with its orders and lines, all in
    one transaction. The partial unique index refuses an order that is on a
    live list already — that surfaces as ReservationConflict."""
    conn = db.get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO picklists (status, backoffice_db_id, customer_id, customer_name,
                       sales_rep_id, sales_rep_name, order_count, unit_count, line_count, created_by)
                   VALUES ('creating', %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (db_id, customer["id"], customer["name"], rep["id"], rep["name"], len(orders),
                 sum(o["units"] for o in orders), len(lines), user_id),
            )
            picklist_id = cur.fetchone()[0]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO picklist_orders
                       (picklist_id, store_id, order_gid, order_name, customer_name, ship_state, units)
                   VALUES %s""",
                [(picklist_id, o["store_id"], o["id"], o["name"], o["customer"], o["ship_state"], o["units"])
                 for o in orders],
            )
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO picklist_lines (picklist_id, barcode, product_id, sku, description, qty) VALUES %s",
                [(picklist_id, g["barcode"], g["product_id"], g["sku"], g["description"], g["qty"]) for g in lines],
            )
        conn.commit()
        return picklist_id
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        taken = active_order_keys({o["store_id"] for o in orders})
        raise ReservationConflict([o["name"] for o in orders if (o["store_id"], o["id"]) in taken])
    except Exception:
        conn.rollback()
        raise


def activate(picklist_id, quotation_id, quotation_number, customer_name):
    db.execute(
        """UPDATE picklists SET status = 'active', quotation_id = %s, quotation_number = %s,
                  customer_name = %s
           WHERE id = %s AND status = 'creating'""",
        (quotation_id, quotation_number, customer_name, picklist_id),
    )


def start_tagging(picklist_id, tag):
    """Remember the pick list's tag and mark every order as waiting for it."""
    db.execute("UPDATE picklists SET shopify_tag = %s WHERE id = %s", (tag, picklist_id))
    db.execute("UPDATE picklist_orders SET tag_state = 'pending', tag_error = NULL WHERE picklist_id = %s",
               (picklist_id,))


def tag_rows(picklist_id):
    return db.query(
        """SELECT o.id, o.store_id, s.name AS store_name, o.order_gid, o.order_name, o.tag_state, o.tag_error
           FROM picklist_orders o LEFT JOIN shopify_stores s ON s.id = o.store_id
           WHERE o.picklist_id = %s ORDER BY o.id""",
        (picklist_id,),
    ) or []


def record_tag_results(results):
    """[(picklist_orders.id, tag_state, error or None)]"""
    for row_id, state, error in results:
        db.execute("UPDATE picklist_orders SET tag_state = %s, tag_error = %s WHERE id = %s", (state, error, row_id))


def release(picklist_id):
    """Drop a reservation whose quotation could not be created."""
    db.execute("DELETE FROM picklists WHERE id = %s AND status = 'creating'", (picklist_id,))


def cancel(picklist_id, user_id):
    conn = db.get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE picklists SET status = 'cancelled', cancelled_by = %s, cancelled_at = now()
                   WHERE id = %s""",
                (user_id, picklist_id),
            )
            cur.execute("UPDATE picklist_orders SET active = FALSE WHERE picklist_id = %s", (picklist_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_picklist(picklist_id):
    return db.query("SELECT * FROM picklists WHERE id = %s", (picklist_id,), one=True)


def list_picklists(limit=200):
    return db.query(
        """SELECT p.*, u.username AS created_by_name,
                  (SELECT COUNT(*) FROM picklist_orders o
                    WHERE o.picklist_id = p.id AND o.tag_state IN ('failed', 'remove_failed')) AS tag_failed
           FROM picklists p LEFT JOIN users u ON u.id = p.created_by
           ORDER BY p.id DESC LIMIT %s""",
        (limit,),
    )


def picklist_detail(picklist_id):
    orders = db.query(
        """SELECT o.store_id, s.name AS store_name, o.order_gid, o.order_name, o.customer_name,
                  o.ship_state, o.units, o.tag_state, o.tag_error
           FROM picklist_orders o LEFT JOIN shopify_stores s ON s.id = o.store_id
           WHERE o.picklist_id = %s ORDER BY o.id""",
        (picklist_id,),
    )
    lines = db.query(
        """SELECT barcode, product_id, sku, description, qty FROM picklist_lines
           WHERE picklist_id = %s ORDER BY description, barcode""",
        (picklist_id,),
    )
    return {"orders": orders or [], "lines": lines or []}

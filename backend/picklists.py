"""Pick lists: batches of Shopify orders whose products, grouped by barcode,
become one BackOffice quotation the warehouse picks from.

An order is eligible while it is fully paid, not on hold, completely
unfulfilled and not on another live pick list. The pure functions shape and
judge orders; the DB helpers reserve orders so no order lands on two lists.
"""
import psycopg2
import psycopg2.extras

import db
import order_gate
import order_risk

STATUSES = ("creating", "active", "cancelled")


class ReservationConflict(Exception):
    """Some orders were put on another pick list in the meantime."""

    def __init__(self, order_names):
        super().__init__("Already on another pick list: " + ", ".join(order_names))
        self.order_names = order_names


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
        "total": ((node.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount"),
        "risk_level": order_risk.risk_level(risk),
        "risk_recommendation": risk.get("recommendation"),
        "lines": lines,
        "units": sum(line["qty"] for line in lines),
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


def split_blocked(orders, known_upcs):
    """(orders that can be picked, orders left out with reasons). An order is
    left out whole when any of its products has no barcode or a barcode
    BackOffice does not know — the quotation never misses part of an order."""
    included, blocked = [], []
    for order in orders:
        reasons = []
        for line in order["lines"]:
            if not line["barcode"]:
                reasons.append(f"{line['title']} has no barcode in Shopify")
            elif line["barcode"] not in known_upcs:
                reasons.append(f"{line['title']} — barcode {line['barcode']} is not in BackOffice")
        if reasons:
            blocked.append({**summary(order), "reasons": reasons})
        else:
            included.append(order)
    return included, blocked


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
        """SELECT p.*, u.username AS created_by_name
           FROM picklists p LEFT JOIN users u ON u.id = p.created_by
           ORDER BY p.id DESC LIMIT %s""",
        (limit,),
    )


def picklist_detail(picklist_id):
    orders = db.query(
        """SELECT o.store_id, s.name AS store_name, o.order_gid, o.order_name, o.customer_name,
                  o.ship_state, o.units
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

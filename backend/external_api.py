from datetime import timezone

from flask import Blueprint, jsonify, request

import db
from util import api_error

bp = Blueprint("external", __name__, url_prefix="/api/external")

MAX_ORDER_NUMBERS = 500


def _iso(dt):
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _num(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n


def _normalize(order_number):
    return str(order_number).strip().lstrip("#").lower()


def _box_to_json(row):
    parcel = (row["parcels"] or [{}])[0]
    dims = [_num(parcel.get(k)) for k in ("length", "width", "height")]
    box_size = "×".join(f"{d:g}" for d in dims) if all(d and d > 0 for d in dims) else None
    weight = row.get("total_weight_lb")
    if weight is None:
        weight = sum(_num(p.get("weight")) or 0 for p in row["parcels"] or [])
    return {
        "box_number": row.get("box_number") or 1,
        "box_total": row.get("box_total") or 1,
        "box_size": box_size,
        "length": dims[0],
        "width": dims[1],
        "height": dims[2],
        "weight_lb": round(float(weight), 2) if weight else None,
        "shipping_cost": _num(row["shipping_cost"]),
        "tracking_number": row["tracking_number"],
    }


@bp.post("/orders/lookup")
def lookup_orders():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return api_error("Request body must be a JSON object")
    numbers = data.get("order_numbers")
    if not isinstance(numbers, list) or not numbers:
        return api_error("order_numbers must be a non-empty list")
    if len(numbers) > MAX_ORDER_NUMBERS:
        return api_error(f"order_numbers is limited to {MAX_ORDER_NUMBERS} entries")
    if not all(isinstance(n, (str, int)) for n in numbers):
        return api_error("order_numbers entries must be strings")

    requested = {}
    for n in numbers:
        requested.setdefault(_normalize(n), str(n))
    normalized = list(requested)

    rows = db.query(
        """SELECT s.*, ss.name AS store_name, bd.name AS db_name
           FROM shipments s
           LEFT JOIN shopify_stores ss ON ss.id = s.shopify_store_id
           LEFT JOIN backoffice_dbs bd ON bd.id = s.backoffice_db_id
           WHERE s.status IN ('label_created', 'fulfilled')
             AND (LOWER(LTRIM(s.shopify_order_name, '#')) = ANY(%s)
                  OR LOWER(s.backoffice_invoice_number) = ANY(%s))
           ORDER BY s.created_at, s.box_number""",
        (normalized, normalized),
    )

    orders = {}
    for row in rows:
        shopify_key = _normalize(row["shopify_order_name"] or "")
        if shopify_key in requested:
            key, matched_source = shopify_key, "shopify"
        else:
            key, matched_source = _normalize(row["backoffice_invoice_number"] or ""), "backoffice"
        order = orders.setdefault(
            key,
            {
                "order_number": requested[key],
                "source": matched_source,
                "store_name": row["store_name"],
                "db_name": row["db_name"],
                "status": row["status"],
                "total_shipping_cost": 0.0,
                "tracking_numbers": [],
                "shipments": [],
            },
        )
        group_key = row["group_id"] or f"row-{row['id']}"
        group = next((g for g in order["shipments"] if g["group_id"] == group_key), None)
        if group is None:
            group = {
                "group_id": group_key,
                "courier": row["courier_name"],
                "courier_umbrella": row["courier_umbrella_name"],
                "label_created_at": _iso(row["label_created_at"]),
                "shipping_cost": 0.0,
                "boxes": [],
            }
            order["shipments"].append(group)
        box = _box_to_json(row)
        group["boxes"].append(box)
        if box["shipping_cost"]:
            group["shipping_cost"] = round(group["shipping_cost"] + box["shipping_cost"], 2)
            order["total_shipping_cost"] = round(order["total_shipping_cost"] + box["shipping_cost"], 2)
        for t in row.get("tracking_numbers") or ([row["tracking_number"]] if row["tracking_number"] else []):
            if t and t not in order["tracking_numbers"]:
                order["tracking_numbers"].append(t)

    not_found = [orig for key, orig in requested.items() if key not in orders]
    return jsonify({"orders": list(orders.values()), "not_found": not_found})

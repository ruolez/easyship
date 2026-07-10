import json
import os
import time

from flask import Blueprint, jsonify, request, send_file, session

import config
import db
import easyship_client as easyship
from auth import login_required
from easyship_client import EasyshipError
from util import api_error, audit, central_time

bp = Blueprint("shipments", __name__, url_prefix="/api/shipments")

LABEL_READY_STATES = {"generated", "printed", "shipping_document_generated"}


def _row_to_json(row):
    total_weight = row.get("total_weight_lb")
    if total_weight is None:
        total_weight = sum(float(p.get("weight") or 0) for p in row["parcels"] or [])
    return {
        "id": row["id"],
        "source": row["source"],
        "service_name": row.get("store_name") or row.get("db_name")
                        or ("Manual" if row["source"] == "manual" else row["source"]),
        "courier_umbrella_name": row.get("courier_umbrella_name")
                                 or (row["rate"] or {}).get("umbrella_name"),
        "total_weight_lb": round(float(total_weight), 2) if total_weight else None,
        "label_created_at": central_time(row.get("label_created_at")),
        "shopify_store_id": row["shopify_store_id"],
        "shopify_order_id": row["shopify_order_id"],
        "shopify_order_name": row["shopify_order_name"],
        "backoffice_db_id": row["backoffice_db_id"],
        "backoffice_invoice_id": row["backoffice_invoice_id"],
        "backoffice_invoice_number": row["backoffice_invoice_number"],
        "destination": row["destination"],
        "parcels": row["parcels"],
        "items": row["items"],
        "easyship_shipment_id": row["easyship_shipment_id"],
        "courier_name": row["courier_name"],
        "shipping_cost": float(row["shipping_cost"]) if row["shipping_cost"] is not None else None,
        "tracking_number": row["tracking_number"],
        "tracking_numbers": row.get("tracking_numbers") or ([row["tracking_number"]] if row["tracking_number"] else []),
        "has_label": bool(row["label_path"]),
        "status": row["status"],
        "error_message": row["error_message"],
        "writeback_shopify_at": central_time(row["writeback_shopify_at"]),
        "writeback_backoffice_at": central_time(row["writeback_backoffice_at"]),
        "created_by": row.get("created_by_username") or row["created_by"],
        "created_at": central_time(row["created_at"]),
    }


@bp.post("/rates")
@login_required
def get_rates():
    data = request.get_json(silent=True) or {}
    destination = data.get("destination") or {}
    parcels = data.get("parcels") or []
    items = data.get("items") or []
    source = data.get("source") or "manual"

    for field in ("address1", "city", "state", "zip"):
        if not (destination.get(field) or "").strip():
            return api_error(f"Destination {field} is required")
    if not parcels:
        return api_error("At least one parcel is required")
    for i, p in enumerate(parcels):
        if not p.get("weight") or float(p["weight"]) <= 0:
            return api_error(f"Parcel {i + 1} needs a weight greater than 0")

    row = db.execute(
        """INSERT INTO shipments
             (source, shopify_store_id, shopify_order_id, shopify_order_name,
              backoffice_db_id, backoffice_invoice_id, backoffice_invoice_number,
              destination, parcels, items, status, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s)
           RETURNING id""",
        (
            source,
            data.get("store_id"),
            data.get("order_id"),
            data.get("order_name"),
            data.get("db_id"),
            data.get("invoice_id"),
            data.get("invoice_number"),
            json.dumps(destination),
            json.dumps(parcels),
            json.dumps(items),
            session["user_id"],
        ),
        returning=True,
    )
    shipment_id = row["id"]

    try:
        es = easyship.create_shipment(destination, parcels, items)
    except EasyshipError as e:
        db.execute(
            "UPDATE shipments SET status='error', error_message=%s, updated_at=now() WHERE id=%s",
            (str(e), shipment_id),
        )
        return api_error(str(e), 502)

    rates = sorted(es.get("rates") or [], key=lambda r: r.get("total_charge") or 0)
    db.execute(
        "UPDATE shipments SET easyship_shipment_id=%s, status='rated', error_message=NULL, updated_at=now() WHERE id=%s",
        (es["easyship_shipment_id"], shipment_id),
    )
    if not rates:
        return api_error(
            "No rates available for this shipment. Check the address and parcel details "
            "(some couriers do not support multi-box shipments — try one box per shipment).",
            422,
        )
    return jsonify({
        "shipment_id": shipment_id,
        "easyship_shipment_id": es["easyship_shipment_id"],
        "rates": [
            {
                "courier_service_id": r["courier_service"]["id"],
                "courier_name": r["courier_service"].get("name"),
                "umbrella_name": r["courier_service"].get("umbrella_name"),
                "total_charge": r.get("total_charge"),
                "currency": r.get("currency"),
                "min_delivery_time": r.get("min_delivery_time"),
                "max_delivery_time": r.get("max_delivery_time"),
                "value_for_money_rank": r.get("value_for_money_rank"),
            }
            for r in rates
        ],
    })


@bp.post("/<int:shipment_id>/buy")
@login_required
def buy(shipment_id):
    data = request.get_json(silent=True) or {}
    courier_service_id = data.get("courier_service_id")
    if not courier_service_id:
        return api_error("courier_service_id is required")

    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    if not row:
        return api_error("Shipment not found", 404)
    if row["status"] not in ("rated", "error"):
        return api_error(f"Shipment is {row['status']} — cannot buy a label")
    if not row["easyship_shipment_id"]:
        return api_error("Shipment has no Easyship shipment id — get rates first")

    try:
        es = easyship.buy_label(row["easyship_shipment_id"], courier_service_id)
    except EasyshipError as e:
        db.execute(
            "UPDATE shipments SET status='error', error_message=%s, updated_at=now() WHERE id=%s",
            (str(e), shipment_id),
        )
        return api_error(str(e), 502)

    # Label generation may lag briefly — poll until ready
    label_state = es.get("label_state")
    tries = 0
    while label_state not in LABEL_READY_STATES and label_state != "failed" and tries < 10:
        time.sleep(2)
        tries += 1
        try:
            es = easyship.get_shipment(row["easyship_shipment_id"])
            label_state = es.get("label_state")
        except EasyshipError:
            break

    if label_state == "failed":
        db.execute(
            "UPDATE shipments SET status='error', error_message='Label generation failed at Easyship', updated_at=now() WHERE id=%s",
            (shipment_id,),
        )
        return api_error("Label generation failed at Easyship", 502)

    tracking_numbers = easyship.extract_tracking_numbers(es)
    tracking_number = tracking_numbers[0] if tracking_numbers else None
    courier = es.get("courier_service") or {}
    rate = data.get("rate") or {}
    total_charge = rate.get("total_charge")

    label_bytes, label_format = easyship.extract_label_document(es)
    if not label_bytes:
        try:
            es_docs = easyship.get_shipment(row["easyship_shipment_id"], pdf_4x6=True)
            label_bytes, label_format = easyship.extract_label_document(es_docs)
        except EasyshipError:
            pass

    label_path = None
    if label_bytes:
        os.makedirs(config.LABELS_DIR, exist_ok=True)
        label_path = os.path.join(config.LABELS_DIR, f"{shipment_id}.{label_format or 'pdf'}")
        with open(label_path, "wb") as f:
            f.write(label_bytes)

    total_weight_lb = sum(float(p.get("weight") or 0) for p in row["parcels"] or [])
    db.execute(
        """UPDATE shipments SET
             courier_name=%s, courier_service_id=%s, courier_umbrella_name=%s,
             rate=%s, shipping_cost=%s, total_weight_lb=%s,
             tracking_number=%s, tracking_numbers=%s, label_path=%s, label_format=%s,
             label_created_at=now(),
             status='label_created', error_message=NULL, updated_at=now()
           WHERE id=%s""",
        (
            courier.get("name") or rate.get("courier_name"),
            courier_service_id,
            courier.get("umbrella_name") or rate.get("umbrella_name"),
            json.dumps(rate) if rate else None,
            total_charge,
            round(total_weight_lb, 2),
            tracking_number,
            json.dumps(tracking_numbers) if tracking_numbers else None,
            label_path,
            label_format or "pdf",
            shipment_id,
        ),
    )
    audit("label.buy", {
        "shipment_id": shipment_id,
        "easyship_shipment_id": row["easyship_shipment_id"],
        "courier": courier.get("name"),
        "cost": total_charge,
    })

    writebacks = run_writebacks(shipment_id)
    printed = _auto_print(shipment_id)
    updated = _get_with_username(shipment_id)
    return jsonify({**_row_to_json(updated), "writebacks": writebacks, "printed": printed})


def _auto_print(shipment_id):
    """Network-print the label right after purchase when print_mode is 'network'.
    Returns 'ok', an error string, or None when browser printing is configured."""
    if (db.get_setting("print_mode") or "browser") != "network":
        return None
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    try:
        import printer
        printer.print_shipment_label(row)
        return "ok"
    except Exception as e:
        return f"error: {e}"


def run_writebacks(shipment_id):
    """Push tracking to Shopify / BackOffice. Never raises — records errors on the row."""
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    results = {}
    errors = []

    if not row["tracking_number"]:
        return {"skipped": "no tracking number yet"}

    if row["source"] == "shopify" and not row["writeback_shopify_at"]:
        try:
            import shopify_client
            fulfillment = shopify_client.fulfill_order(
                row["shopify_store_id"], row["shopify_order_id"],
                row["tracking_number"], row["courier_name"],
                all_numbers=row["tracking_numbers"] or None,
            )
            db.execute(
                """UPDATE shipments SET writeback_shopify_at=now(),
                   shopify_fulfillment_id=%s, updated_at=now() WHERE id=%s""",
                ((fulfillment or {}).get("id"), shipment_id),
            )
            results["shopify"] = "ok"
        except Exception as e:
            results["shopify"] = f"error: {e}"
            errors.append(f"Shopify: {e}")

    if row["source"] == "backoffice" and not row["writeback_backoffice_at"]:
        try:
            import backoffice
            backoffice.write_tracking(
                row["backoffice_db_id"], row["backoffice_invoice_id"],
                row["tracking_number"], row["shipping_cost"],
                extra_numbers=(row["tracking_numbers"] or [])[1:],
            )
            db.execute(
                "UPDATE shipments SET writeback_backoffice_at=now(), updated_at=now() WHERE id=%s",
                (shipment_id,),
            )
            results["backoffice"] = "ok"
        except Exception as e:
            results["backoffice"] = f"error: {e}"
            errors.append(f"BackOffice: {e}")

    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    done = (
        (row["source"] == "manual")
        or (row["source"] == "shopify" and row["writeback_shopify_at"])
        or (row["source"] == "backoffice" and row["writeback_backoffice_at"])
    )
    if done and row["status"] == "label_created":
        db.execute(
            "UPDATE shipments SET status='fulfilled', error_message=NULL, updated_at=now() WHERE id=%s",
            (shipment_id,),
        )
    elif errors:
        db.execute(
            "UPDATE shipments SET error_message=%s, updated_at=now() WHERE id=%s",
            ("; ".join(errors), shipment_id),
        )
    return results


@bp.post("/<int:shipment_id>/writeback")
@login_required
def retry_writeback(shipment_id):
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    if not row:
        return api_error("Shipment not found", 404)
    if row["status"] not in ("label_created", "fulfilled"):
        return api_error("No label yet — nothing to write back")
    results = run_writebacks(shipment_id)
    updated = _get_with_username(shipment_id)
    return jsonify({**_row_to_json(updated), "writebacks": results})


LIST_SELECT = """
    SELECT s.*, u.username AS created_by_username,
           ss.name AS store_name, bd.name AS db_name
    FROM shipments s
    JOIN users u ON u.id = s.created_by
    LEFT JOIN shopify_stores ss ON ss.id = s.shopify_store_id
    LEFT JOIN backoffice_dbs bd ON bd.id = s.backoffice_db_id
"""


@bp.get("")
@login_required
def list_shipments():
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()
    user = (request.args.get("user") or "").strip()
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()
    limit = min(int(request.args.get("limit") or 200), 1000)
    sql = LIST_SELECT + " WHERE TRUE"
    params = []
    if q:
        sql += """ AND (s.tracking_number ILIKE %s OR s.shopify_order_name ILIKE %s
                   OR s.backoffice_invoice_number ILIKE %s OR s.destination->>'company' ILIKE %s
                   OR s.destination->>'contact' ILIKE %s OR s.destination->>'city' ILIKE %s)"""
        like = f"%{q}%"
        params += [like] * 6
    if status:
        sql += " AND s.status = %s"
        params.append(status)
    if user:
        sql += " AND u.username = %s"
        params.append(user)
    if date_from:
        sql += " AND (s.created_at AT TIME ZONE 'America/Chicago')::date >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND (s.created_at AT TIME ZONE 'America/Chicago')::date <= %s"
        params.append(date_to)
    sql += " ORDER BY s.created_at DESC LIMIT %s"
    params.append(limit)
    rows = db.query(sql, params)
    return jsonify([_row_to_json(r) for r in rows])


@bp.get("/creators")
@login_required
def creators():
    rows = db.query(
        """SELECT DISTINCT u.username FROM shipments s
           JOIN users u ON u.id = s.created_by ORDER BY u.username"""
    )
    return jsonify([r["username"] for r in rows])


def _get_with_username(shipment_id):
    return db.query(LIST_SELECT + " WHERE s.id = %s", (shipment_id,), one=True)


@bp.get("/<int:shipment_id>")
@login_required
def get_shipment(shipment_id):
    row = _get_with_username(shipment_id)
    if not row:
        return api_error("Shipment not found", 404)
    return jsonify(_row_to_json(row))


LABEL_MIMETYPES = {"pdf": "application/pdf", "png": "image/png", "zpl": "text/plain"}


@bp.get("/<int:shipment_id>/label")
@login_required
def get_label(shipment_id):
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    if not row or not row["label_path"]:
        return api_error("No label stored for this shipment", 404)
    if not os.path.exists(row["label_path"]):
        return api_error("Label file missing from storage", 410)
    # Sniff the real type — older rows may have been stored with a wrong
    # extension (e.g. 'url'), which made browsers download instead of display.
    with open(row["label_path"], "rb") as f:
        head = f.read(16)
    if head.startswith(b"%PDF"):
        fmt = "pdf"
    elif head.startswith(b"\x89PNG"):
        fmt = "png"
    elif head[:3] == b"^XA":
        fmt = "zpl"
    else:
        fmt = row["label_format"] if row["label_format"] in LABEL_MIMETYPES else "pdf"
    response = send_file(
        row["label_path"],
        mimetype=LABEL_MIMETYPES.get(fmt, "application/pdf"),
        download_name=f"label-{shipment_id}.{fmt}",
        as_attachment=False,
    )
    response.headers["Content-Disposition"] = f'inline; filename="label-{shipment_id}.{fmt}"'
    return response


@bp.post("/<int:shipment_id>/print")
@login_required
def print_label(shipment_id):
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    if not row:
        return api_error("Shipment not found", 404)
    try:
        import printer
        printer.print_shipment_label(row)
    except Exception as e:
        return api_error(str(e))
    audit("label.print", {"shipment_id": shipment_id})
    return jsonify({"ok": True})


@bp.post("/<int:shipment_id>/void")
@login_required
def void(shipment_id):
    """Undo a shipment: cancel the label at Easyship, remove the tracking number
    from Shopify (cancel fulfillment) / BackOffice (clear TrackingNo, ShippingCost).
    Calling it again on a voided shipment retries any undo step that failed."""
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    if not row:
        return api_error("Shipment not found", 404)
    if row["status"] not in ("label_created", "fulfilled", "rated", "error", "voided"):
        return api_error(f"Shipment is {row['status']} — cannot void")

    if row["status"] != "voided" and row["easyship_shipment_id"]:
        try:
            easyship.cancel_shipment(row["easyship_shipment_id"])
        except EasyshipError as e:
            return api_error(str(e), 502)

    undo = {}
    errors = []

    if row["writeback_shopify_at"]:
        try:
            import shopify_client
            fulfillment_gid = row["shopify_fulfillment_id"]
            if not fulfillment_gid and row["tracking_number"]:
                fulfillment_gid = shopify_client.find_fulfillment_by_tracking(
                    row["shopify_store_id"], row["shopify_order_id"], row["tracking_number"]
                )
            if fulfillment_gid:
                shopify_client.cancel_fulfillment(row["shopify_store_id"], fulfillment_gid)
            db.execute(
                """UPDATE shipments SET writeback_shopify_at=NULL,
                   shopify_fulfillment_id=NULL, updated_at=now() WHERE id=%s""",
                (shipment_id,),
            )
            undo["shopify"] = "fulfillment cancelled" if fulfillment_gid else "no matching fulfillment found"
        except Exception as e:
            undo["shopify"] = f"error: {e}"
            errors.append(f"Shopify undo: {e}")

    if row["writeback_backoffice_at"]:
        try:
            import backoffice
            backoffice.clear_tracking(
                row["backoffice_db_id"], row["backoffice_invoice_id"], row["tracking_number"],
                extra_numbers=(row["tracking_numbers"] or [])[1:],
            )
            db.execute(
                "UPDATE shipments SET writeback_backoffice_at=NULL, updated_at=now() WHERE id=%s",
                (shipment_id,),
            )
            undo["backoffice"] = "tracking number cleared"
        except Exception as e:
            undo["backoffice"] = f"error: {e}"
            errors.append(f"BackOffice undo: {e}")

    db.execute(
        "UPDATE shipments SET status='voided', error_message=%s, updated_at=now() WHERE id=%s",
        ("; ".join(errors) if errors else None, shipment_id),
    )
    audit("label.void", {
        "shipment_id": shipment_id,
        "easyship_shipment_id": row["easyship_shipment_id"],
        "undo": undo,
    })
    return jsonify({"ok": not errors, "undo": undo, "errors": errors})

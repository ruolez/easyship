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
    return {
        "id": row["id"],
        "source": row["source"],
        "shopify_store_id": row["shopify_store_id"],
        "shopify_order_id": row["shopify_order_id"],
        "shopify_order_name": row["shopify_order_name"],
        "backoffice_invoice_id": row["backoffice_invoice_id"],
        "backoffice_invoice_number": row["backoffice_invoice_number"],
        "destination": row["destination"],
        "parcels": row["parcels"],
        "items": row["items"],
        "easyship_shipment_id": row["easyship_shipment_id"],
        "courier_name": row["courier_name"],
        "shipping_cost": float(row["shipping_cost"]) if row["shipping_cost"] is not None else None,
        "tracking_number": row["tracking_number"],
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
              backoffice_invoice_id, backoffice_invoice_number,
              destination, parcels, items, status, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s)
           RETURNING id""",
        (
            source,
            data.get("store_id"),
            data.get("order_id"),
            data.get("order_name"),
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

    tracking_number = easyship.extract_tracking_number(es)
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

    db.execute(
        """UPDATE shipments SET
             courier_name=%s, courier_service_id=%s, rate=%s, shipping_cost=%s,
             tracking_number=%s, label_path=%s, label_format=%s,
             status='label_created', error_message=NULL, updated_at=now()
           WHERE id=%s""",
        (
            courier.get("name") or rate.get("courier_name"),
            courier_service_id,
            json.dumps(rate) if rate else None,
            total_charge,
            tracking_number,
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
    updated = _get_with_username(shipment_id)
    return jsonify({**_row_to_json(updated), "writebacks": writebacks})


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
            shopify_client.fulfill_order(
                row["shopify_store_id"], row["shopify_order_id"],
                row["tracking_number"], row["courier_name"],
            )
            db.execute(
                "UPDATE shipments SET writeback_shopify_at=now(), updated_at=now() WHERE id=%s",
                (shipment_id,),
            )
            results["shopify"] = "ok"
        except Exception as e:
            results["shopify"] = f"error: {e}"
            errors.append(f"Shopify: {e}")

    if row["source"] == "backoffice" and not row["writeback_backoffice_at"]:
        try:
            import backoffice
            backoffice.write_tracking(
                row["backoffice_invoice_id"], row["tracking_number"], row["shipping_cost"],
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


@bp.get("")
@login_required
def list_shipments():
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()
    limit = min(int(request.args.get("limit") or 100), 500)
    sql = """SELECT s.*, u.username AS created_by_username
             FROM shipments s JOIN users u ON u.id = s.created_by WHERE TRUE"""
    params = []
    if q:
        sql += """ AND (s.tracking_number ILIKE %s OR s.shopify_order_name ILIKE %s
                   OR s.backoffice_invoice_number ILIKE %s OR s.destination->>'company' ILIKE %s
                   OR s.destination->>'contact' ILIKE %s)"""
        like = f"%{q}%"
        params += [like, like, like, like, like]
    if status:
        sql += " AND s.status = %s"
        params.append(status)
    sql += " ORDER BY s.created_at DESC LIMIT %s"
    params.append(limit)
    rows = db.query(sql, params)
    return jsonify([_row_to_json(r) for r in rows])


def _get_with_username(shipment_id):
    return db.query(
        """SELECT s.*, u.username AS created_by_username
           FROM shipments s JOIN users u ON u.id = s.created_by WHERE s.id = %s""",
        (shipment_id,),
        one=True,
    )


@bp.get("/<int:shipment_id>")
@login_required
def get_shipment(shipment_id):
    row = _get_with_username(shipment_id)
    if not row:
        return api_error("Shipment not found", 404)
    return jsonify(_row_to_json(row))


@bp.get("/<int:shipment_id>/label")
@login_required
def get_label(shipment_id):
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    if not row or not row["label_path"]:
        return api_error("No label stored for this shipment", 404)
    if not os.path.exists(row["label_path"]):
        return api_error("Label file missing from storage", 410)
    mimetype = "application/pdf" if row["label_format"] == "pdf" else "application/octet-stream"
    return send_file(row["label_path"], mimetype=mimetype, download_name=f"label-{shipment_id}.{row['label_format']}")


@bp.post("/<int:shipment_id>/void")
@login_required
def void(shipment_id):
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    if not row:
        return api_error("Shipment not found", 404)
    if row["status"] not in ("label_created", "fulfilled", "rated", "error"):
        return api_error(f"Shipment is {row['status']} — cannot void")
    if row["easyship_shipment_id"]:
        try:
            easyship.cancel_shipment(row["easyship_shipment_id"])
        except EasyshipError as e:
            return api_error(str(e), 502)
    db.execute(
        "UPDATE shipments SET status='voided', updated_at=now() WHERE id=%s", (shipment_id,)
    )
    audit("label.void", {"shipment_id": shipment_id, "easyship_shipment_id": row["easyship_shipment_id"]})
    return jsonify({"ok": True})

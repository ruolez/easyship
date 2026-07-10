import json
import os
import threading
import time

from flask import Blueprint, current_app, jsonify, request, send_file, session

import config
import db
import easyship_client as easyship
from auth import admin_required, login_required
from easyship_client import EasyshipError
from util import api_error, audit, central_time

bp = Blueprint("shipments", __name__, url_prefix="/api/shipments")

LABEL_READY_STATES = {"generated", "printed", "shipping_document_generated"}


def _row_to_json(row):
    total_weight = row.get("total_weight_lb")
    if total_weight is None:
        total_weight = sum(float(p.get("weight") or 0) for p in row["parcels"] or [])
    box_count = len(row["parcels"] or []) or 1
    progress = row.get("progress") or {}
    progress_boxes = progress.get("boxes") or []
    if progress_boxes:
        boxes_ready = len([b for b in progress_boxes if b.get("status") == "ready"])
    else:
        boxes_ready = len(row.get("tracking_numbers") or ([1] if row["tracking_number"] else []))
    return {
        "box_count": box_count,
        "boxes_ready": min(boxes_ready, box_count),
        "courier_service_id": row["courier_service_id"],
        "rate": row["rate"],
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
        "progress": row.get("progress"),
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
        es_list = easyship.create_shipments(destination, parcels, items)
    except EasyshipError as e:
        db.execute(
            "UPDATE shipments SET status='error', error_message=%s, updated_at=now() WHERE id=%s",
            (str(e), shipment_id),
        )
        return api_error(str(e), 502)

    ids = [s["easyship_shipment_id"] for s in es_list]
    db.execute(
        """UPDATE shipments SET easyship_shipment_id=%s, easyship_shipment_ids=%s,
           status='rated', error_message=NULL, updated_at=now() WHERE id=%s""",
        (ids[0], json.dumps(ids), shipment_id),
    )
    rates = combine_rates(es_list)
    if not rates:
        message = "No rates available for this shipment. Check the address and parcel details."
        if len(es_list) > 1:
            message = ("No single courier returned rates for every box. "
                       "Check each box's weight and dimensions.")
        return api_error(message, 422)
    return jsonify({
        "shipment_id": shipment_id,
        "easyship_shipment_id": ids[0],
        "box_count": len(ids),
        "rates": rates,
    })


def combine_rates(es_list):
    """One quote list across per-box shipments: only couriers that can serve
    EVERY box, price = sum across boxes."""
    rate_maps = [
        {r["courier_service"]["id"]: r for r in (s.get("rates") or [])}
        for s in es_list
    ]
    common = set(rate_maps[0])
    for m in rate_maps[1:]:
        common &= set(m)
    combined = []
    for cid in common:
        rs = [m[cid] for m in rate_maps]
        combined.append({
            "courier_service_id": cid,
            "courier_name": rs[0]["courier_service"].get("name"),
            "umbrella_name": rs[0]["courier_service"].get("umbrella_name"),
            "total_charge": round(sum(r.get("total_charge") or 0 for r in rs), 2),
            "currency": rs[0].get("currency"),
            "min_delivery_time": max((r.get("min_delivery_time") or 0) for r in rs) or None,
            "max_delivery_time": max((r.get("max_delivery_time") or 0) for r in rs) or None,
            "value_for_money_rank": rs[0].get("value_for_money_rank"),
        })
    return sorted(combined, key=lambda r: r["total_charge"])


def _set_progress(shipment_id, state, boxes=None, message=None, extra=None):
    progress = {"state": state}
    if boxes is not None:
        progress["boxes"] = boxes
    if message:
        progress["message"] = message
    if extra:
        progress.update(extra)
    db.execute(
        "UPDATE shipments SET progress=%s, updated_at=now() WHERE id=%s",
        (json.dumps(progress), shipment_id),
    )


def _box_progress(ids, state, errors=None):
    errors = errors or {}
    boxes = []
    for i, sid in enumerate(ids):
        s = state.get(sid)
        box = {"box": i + 1}
        if not s or s.get("label_state") in (None, "not_created"):
            box["status"] = "purchasing"
        elif s.get("label_state") in LABEL_READY_STATES:
            numbers = easyship.extract_tracking_numbers(s)
            box.update(status="ready", tracking=numbers[0] if numbers else None)
        elif s.get("label_state") == "failed":
            box["status"] = "failed"
        else:
            box["status"] = "generating"
        if sid in errors and box["status"] in ("purchasing", "failed"):
            box["error"] = errors[sid][:200]
        boxes.append(box)
    return boxes


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
    progress = row["progress"] or {}
    if progress.get("state") == "buying":
        # the worker updates the row every few seconds; a long-stale 'buying'
        # means the process died mid-purchase — allow a retry then
        from datetime import datetime, timedelta, timezone
        if datetime.now(timezone.utc) - row["updated_at"] < timedelta(minutes=5):
            return api_error("Label purchase already in progress", 409)

    ids = row["easyship_shipment_ids"] or [row["easyship_shipment_id"]]
    # persist the chosen courier so an interrupted purchase can be resumed
    # from the Parcels page with the same selection
    db.execute(
        "UPDATE shipments SET courier_service_id=%s, rate=%s, updated_at=now() WHERE id=%s",
        (courier_service_id, json.dumps(data.get("rate") or {}), shipment_id),
    )
    _set_progress(shipment_id, "buying", boxes=[
        {"box": i + 1, "status": "purchasing"} for i in range(len(ids))
    ])

    app = current_app._get_current_object()
    worker = threading.Thread(
        target=_buy_worker,
        args=(app, shipment_id, courier_service_id, data.get("rate") or {}, session["user_id"]),
        daemon=True,
    )
    worker.start()
    return jsonify({"started": True, "box_count": len(ids)})


def _buy_worker(app, shipment_id, courier_service_id, rate, user_id):
    with app.app_context():
        try:
            _buy_impl(shipment_id, courier_service_id, rate, user_id)
        except Exception as e:  # never leave the row stuck in 'buying'
            db.execute(
                "UPDATE shipments SET status='error', error_message=%s, updated_at=now() WHERE id=%s",
                (str(e), shipment_id),
            )
            _set_progress(shipment_id, "error", message=str(e))


def _buy_impl(shipment_id, courier_service_id, rate, user_id):
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    ids = row["easyship_shipment_ids"] or [row["easyship_shipment_id"]]

    # Buy every box's label in parallel (one Easyship shipment per box).
    results = easyship.buy_labels(ids, courier_service_id)

    # Unified polling: a buy error may mean "already purchased" (retry after an
    # earlier timeout) or a gateway timeout while the label still generates —
    # keep refreshing every shipment until each is ready, failed, or we run out
    # of time. Retrying later can never double-charge: the same Easyship
    # shipments are reused and each can carry only one label.
    state = {}
    box_errors = {}
    last_error = None
    for sid in ids:
        res = results.get(sid)
        if isinstance(res, EasyshipError):
            last_error = res
            box_errors[sid] = str(res)
            state[sid] = None
        else:
            state[sid] = res

    def pending():
        return [
            sid for sid in ids
            if not state[sid] or (
                state[sid].get("label_state") not in LABEL_READY_STATES
                and state[sid].get("label_state") != "failed"
            )
        ]

    try:
        timeout_s = int(db.get_setting("label_timeout_seconds") or 120)
    except ValueError:
        timeout_s = 120
    _set_progress(shipment_id, "buying", boxes=_box_progress(ids, state, box_errors))
    deadline = time.monotonic() + max(timeout_s, 30)
    rebuy_next = {}
    while pending() and time.monotonic() < deadline:
        time.sleep(3)
        refreshed = easyship.get_shipments(pending())
        for sid, res in refreshed.items():
            if isinstance(res, EasyshipError):
                last_error = res
            else:
                state[sid] = res

        # A box still at not_created means its purchase request was LOST
        # (rate limit / gateway) — polling alone would wait forever. Re-issue
        # the purchase; a shipment can only carry one label, so this can
        # never double-charge.
        now = time.monotonic()
        rebuy_ids = [
            sid for sid in pending()
            if (not state[sid] or state[sid].get("label_state") in (None, "not_created"))
            and now >= rebuy_next.get(sid, 0)
        ]
        if rebuy_ids:
            for sid, res in easyship.buy_labels(rebuy_ids, courier_service_id).items():
                rebuy_next[sid] = time.monotonic() + 12
                if isinstance(res, EasyshipError):
                    last_error = res
                    box_errors[sid] = str(res)
                else:
                    state[sid] = res
                    box_errors.pop(sid, None)

        _set_progress(shipment_id, "buying", boxes=_box_progress(ids, state, box_errors))

    failed = [sid for sid in ids if state[sid] and state[sid].get("label_state") == "failed"]
    if failed:
        message = f"Label generation failed at Easyship for {len(failed)} of {len(ids)} box(es)"
        db.execute(
            "UPDATE shipments SET status='error', error_message=%s, updated_at=now() WHERE id=%s",
            (message, shipment_id),
        )
        _set_progress(shipment_id, "error", boxes=_box_progress(ids, state, box_errors), message=message)
        return

    if pending():
        message = (
            f"{last_error or 'Easyship did not finish in time.'} "
            f"{len(pending())} of {len(ids)} label(s) not confirmed yet — click Print label "
            "again to retry; the same shipments are reused so you cannot be charged twice."
        )
        db.execute(
            "UPDATE shipments SET status='rated', error_message=%s, updated_at=now() WHERE id=%s",
            (message, shipment_id),
        )
        _set_progress(shipment_id, "retry", boxes=_box_progress(ids, state, box_errors), message=message)
        return

    # All boxes ready: collect tracking numbers and label documents in box order
    tracking_numbers = []
    docs = []
    for sid in ids:
        for n in easyship.extract_tracking_numbers(state[sid]):
            if n not in tracking_numbers:
                tracking_numbers.append(n)
        sid_docs = easyship.extract_label_documents(state[sid])
        if not sid_docs:
            try:
                sid_docs = easyship.extract_label_documents(
                    easyship.get_shipment(sid, pdf_4x6=True)
                )
            except EasyshipError:
                pass
        docs.extend(sid_docs)

    tracking_number = tracking_numbers[0] if tracking_numbers else None
    es = state[ids[0]]
    courier = es.get("courier_service") or {}
    total_charge = rate.get("total_charge")
    _set_progress(shipment_id, "finalizing", boxes=_box_progress(ids, state),
                  message="Saving label and updating order…")
    label_bytes, label_format = easyship.merge_label_documents(docs)

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
        "easyship_shipment_ids": ids,
        "courier": courier.get("name"),
        "cost": total_charge,
    }, user_id=user_id)

    writebacks = run_writebacks(shipment_id)
    printed = _auto_print(shipment_id)
    _set_progress(shipment_id, "done", boxes=_box_progress(ids, state),
                  extra={"printed": printed, "writebacks": writebacks})


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


@bp.get("/<int:shipment_id>/easyship")
@admin_required
def easyship_raw(shipment_id):
    """Diagnostic: the raw shipment object as Easyship returns it right now."""
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    if not row:
        return api_error("Shipment not found", 404)
    if not row["easyship_shipment_id"]:
        return api_error("Shipment was never sent to Easyship")
    try:
        return jsonify(easyship.get_shipment(row["easyship_shipment_id"]))
    except EasyshipError as e:
        return api_error(str(e), 502)


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
        ids = row["easyship_shipment_ids"] or [row["easyship_shipment_id"]]
        cancel_errors = easyship.cancel_all(ids)
        if cancel_errors:
            return api_error("; ".join(cancel_errors), 502)

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

import json
import os
import threading
import time
import uuid

from flask import Blueprint, current_app, jsonify, request, send_file, session

import config
import db
import easyship_client as easyship
from auth import admin_required, login_required
from easyship_client import EasyshipError
from util import api_error, audit, central_time

bp = Blueprint("shipments", __name__, url_prefix="/api/shipments")

LABEL_READY_STATES = {"generated", "printed", "shipping_document_generated"}
LABEL_MIMETYPES = {"pdf": "application/pdf", "png": "image/png", "zpl": "text/plain"}

LIST_SELECT = """
    SELECT s.*, u.username AS created_by_username,
           ss.name AS store_name, bd.name AS db_name
    FROM shipments s
    JOIN users u ON u.id = s.created_by
    LEFT JOIN shopify_stores ss ON ss.id = s.shopify_store_id
    LEFT JOIN backoffice_dbs bd ON bd.id = s.backoffice_db_id
"""


def _row_to_json(row):
    total_weight = row.get("total_weight_lb")
    if total_weight is None:
        total_weight = sum(float(p.get("weight") or 0) for p in row["parcels"] or [])
    return {
        "id": row["id"],
        "group_id": row.get("group_id"),
        "box_number": row.get("box_number") or 1,
        "box_total": row.get("box_total") or 1,
        "courier_service_id": row["courier_service_id"],
        "rate": row["rate"],
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


def _get_with_username(shipment_id):
    return db.query(LIST_SELECT + " WHERE s.id = %s", (shipment_id,), one=True)


def _group_rows(group_id):
    return db.query(LIST_SELECT + " WHERE s.group_id = %s ORDER BY s.box_number", (group_id,))


# ============================================================ rates

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
            return api_error(f"Box {i + 1} needs a weight greater than 0")

    # One local row PER BOX — each box is its own parcel with its own label
    # and tracking number, linked by a group id.
    group_id = uuid.uuid4().hex
    box_total = len(parcels)
    row_ids = []
    for i, parcel in enumerate(parcels):
        row = db.execute(
            """INSERT INTO shipments
                 (group_id, box_number, box_total,
                  source, shopify_store_id, shopify_order_id, shopify_order_name,
                  backoffice_db_id, backoffice_invoice_id, backoffice_invoice_number,
                  destination, parcels, items, status, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s)
               RETURNING id""",
            (
                group_id, i + 1, box_total,
                source,
                data.get("store_id"),
                data.get("order_id"),
                data.get("order_name"),
                data.get("db_id"),
                data.get("invoice_id"),
                data.get("invoice_number"),
                json.dumps(destination),
                json.dumps([parcel]),
                json.dumps(items if i == 0 else []),
                session["user_id"],
            ),
            returning=True,
        )
        row_ids.append(row["id"])

    try:
        es_list = easyship.create_shipments(destination, parcels, items)
    except EasyshipError as e:
        for rid in row_ids:
            db.execute(
                "UPDATE shipments SET status='error', error_message=%s, updated_at=now() WHERE id=%s",
                (str(e), rid),
            )
        return api_error(str(e), 502)

    for rid, es in zip(row_ids, es_list):
        sid = es["easyship_shipment_id"]
        db.execute(
            """UPDATE shipments SET easyship_shipment_id=%s, easyship_shipment_ids=%s,
               status='rated', error_message=NULL, updated_at=now() WHERE id=%s""",
            (sid, json.dumps([sid]), rid),
        )

    rates = combine_rates(es_list)
    if not rates:
        message = "No rates available for this shipment. Check the address and parcel details."
        if len(es_list) > 1:
            message = ("No single courier returned rates for every box. "
                       "Check each box's weight and dimensions.")
        return api_error(message, 422)
    return jsonify({
        "group_id": group_id,
        "shipment_id": row_ids[0],
        "shipment_ids": row_ids,
        "box_count": box_total,
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


# ============================================================ group buy

def _set_group_progress(primary_id, state, boxes=None, message=None, extra=None):
    progress = {"state": state}
    if boxes is not None:
        progress["boxes"] = boxes
    if message:
        progress["message"] = message
    if extra:
        progress.update(extra)
    db.execute(
        "UPDATE shipments SET progress=%s, updated_at=now() WHERE id=%s",
        (json.dumps(progress), primary_id),
    )


def _group_boxes_snapshot(rows, live_state=None, errors=None):
    """Per-box status list built from DB rows plus in-flight Easyship state."""
    live_state = live_state or {}
    errors = errors or {}
    boxes = []
    for row in rows:
        box = {"box": row["box_number"], "shipment_id": row["id"]}
        sid = row["easyship_shipment_id"]
        if row["status"] in ("label_created", "fulfilled"):
            box.update(status="ready", tracking=row["tracking_number"])
        elif sid in live_state and live_state[sid]:
            ls = live_state[sid].get("label_state")
            if ls in LABEL_READY_STATES:
                numbers = easyship.extract_tracking_numbers(live_state[sid])
                box.update(status="ready", tracking=numbers[0] if numbers else None)
            elif ls == "failed":
                box["status"] = "failed"
            elif ls in (None, "not_created"):
                box["status"] = "purchasing"
            else:
                box["status"] = "generating"
        else:
            box["status"] = "purchasing" if row["status"] in ("rated", "error") else row["status"]
        if sid in errors and box["status"] in ("purchasing", "failed"):
            box["error"] = errors[sid][:200]
        boxes.append(box)
    return boxes


@bp.post("/group/<group_id>/buy")
@login_required
def group_buy(group_id):
    data = request.get_json(silent=True) or {}
    courier_service_id = data.get("courier_service_id")
    rows = _group_rows(group_id)
    if not rows:
        return api_error("Shipment group not found", 404)
    primary = rows[0]
    courier_service_id = courier_service_id or primary["courier_service_id"]
    if not courier_service_id:
        return api_error("courier_service_id is required")
    rate = data.get("rate") or primary["rate"] or {}

    progress = primary["progress"] or {}
    if progress.get("state") == "buying":
        from datetime import datetime, timedelta, timezone
        if datetime.now(timezone.utc) - primary["updated_at"] < timedelta(minutes=5):
            return api_error("Label purchase already in progress", 409)

    targets = [r for r in rows if r["status"] in ("rated", "error") and r["easyship_shipment_id"]]
    if not targets:
        return api_error("Nothing to purchase — all boxes already have labels or were voided")

    for r in rows:
        db.execute(
            "UPDATE shipments SET courier_service_id=%s, rate=%s, updated_at=now() WHERE id=%s",
            (courier_service_id, json.dumps(rate), r["id"]),
        )
    _set_group_progress(primary["id"], "buying", boxes=_group_boxes_snapshot(rows))

    app = current_app._get_current_object()
    threading.Thread(
        target=_group_buy_worker,
        args=(app, group_id, courier_service_id, rate, session["user_id"]),
        daemon=True,
    ).start()
    return jsonify({"started": True, "box_count": len(rows)})


def _group_buy_worker(app, group_id, courier_service_id, rate, user_id):
    with app.app_context():
        primary_id = None
        try:
            rows = _group_rows(group_id)
            primary_id = rows[0]["id"]
            _group_buy_impl(group_id, courier_service_id, rate, user_id)
        except Exception as e:  # never leave the group stuck in 'buying'
            if primary_id:
                _set_group_progress(primary_id, "error", message=str(e))


def _per_box_cost(es, courier_service_id, rate, box_total):
    for r in es.get("rates") or []:
        if (r.get("courier_service") or {}).get("id") == courier_service_id:
            return r.get("total_charge")
    total = rate.get("total_charge")
    if total and box_total:
        return round(float(total) / box_total, 2)
    return None


def _finalize_row(row, es, courier_service_id, rate, box_total):
    """A box's label is ready: save its label file and complete its row."""
    numbers = easyship.extract_tracking_numbers(es)
    tracking = numbers[0] if numbers else None
    docs = easyship.extract_label_documents(es)
    if not docs:
        try:
            docs = easyship.extract_label_documents(
                easyship.get_shipment(row["easyship_shipment_id"], pdf_4x6=True)
            )
        except EasyshipError:
            pass
    label_bytes, label_format = easyship.merge_label_documents(docs)
    label_path = None
    if label_bytes:
        os.makedirs(config.LABELS_DIR, exist_ok=True)
        label_path = os.path.join(config.LABELS_DIR, f"{row['id']}.{label_format or 'pdf'}")
        with open(label_path, "wb") as f:
            f.write(label_bytes)

    courier = es.get("courier_service") or {}
    weight = sum(float(p.get("weight") or 0) for p in row["parcels"] or [])
    db.execute(
        """UPDATE shipments SET
             courier_name=%s, courier_umbrella_name=%s,
             shipping_cost=%s, total_weight_lb=%s,
             tracking_number=%s, tracking_numbers=%s, label_path=%s, label_format=%s,
             label_created_at=now(),
             status='label_created', error_message=NULL, updated_at=now()
           WHERE id=%s""",
        (
            courier.get("name") or rate.get("courier_name"),
            courier.get("umbrella_name") or rate.get("umbrella_name"),
            _per_box_cost(es, courier_service_id, rate, box_total),
            round(weight, 2),
            tracking,
            json.dumps(numbers) if numbers else None,
            label_path,
            label_format or "pdf",
            row["id"],
        ),
    )


def _group_buy_impl(group_id, courier_service_id, rate, user_id):
    rows = _group_rows(group_id)
    primary_id = rows[0]["id"]
    box_total = len(rows)
    targets = {r["easyship_shipment_id"]: r for r in rows
               if r["status"] in ("rated", "error") and r["easyship_shipment_id"]}
    sids = list(targets.keys())

    state = {}
    box_errors = {}
    last_error = None
    results = easyship.buy_labels(sids, courier_service_id)
    for sid in sids:
        res = results.get(sid)
        if isinstance(res, EasyshipError):
            last_error = res
            box_errors[sid] = str(res)
            state[sid] = None
        else:
            state[sid] = res

    finalized = set()

    def maybe_finalize():
        """Complete rows for boxes whose labels are ready — but the group
        result (writebacks, printing, done state) waits for ALL boxes."""
        for sid, row in targets.items():
            if sid in finalized:
                continue
            s = state.get(sid)
            if s and s.get("label_state") in LABEL_READY_STATES:
                _finalize_row(row, s, courier_service_id, rate, box_total)
                finalized.add(sid)

    def pending():
        return [
            sid for sid in sids
            if sid not in finalized and not (
                state.get(sid) and state[sid].get("label_state") == "failed"
            )
        ]

    try:
        timeout_s = int(db.get_setting("label_timeout_seconds") or 120)
    except ValueError:
        timeout_s = 120
    deadline = time.monotonic() + max(timeout_s, 30)
    rebuy_next = {}

    maybe_finalize()
    _set_group_progress(primary_id, "buying",
                        boxes=_group_boxes_snapshot(_group_rows(group_id), state, box_errors))

    while pending() and time.monotonic() < deadline:
        time.sleep(3)
        refreshed = easyship.get_shipments(pending())
        for sid, res in refreshed.items():
            if isinstance(res, EasyshipError):
                last_error = res
            else:
                state[sid] = res

        # A purchase request that was lost (rate limit / gateway) leaves the
        # shipment at not_created — polling alone would wait forever, so
        # re-issue it. One label max per shipment: can never double-charge.
        now = time.monotonic()
        rebuy_ids = [
            sid for sid in pending()
            if (not state.get(sid) or state[sid].get("label_state") in (None, "not_created"))
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

        maybe_finalize()
        _set_group_progress(primary_id, "buying",
                            boxes=_group_boxes_snapshot(_group_rows(group_id), state, box_errors))

    rows = _group_rows(group_id)
    incomplete = [r for r in rows if r["status"] not in ("label_created", "fulfilled")]
    if incomplete:
        failed = [sid for sid in sids
                  if state.get(sid) and state[sid].get("label_state") == "failed"]
        if failed:
            message = f"Label generation failed at Easyship for {len(failed)} of {box_total} box(es)"
            progress_state = "error"
        else:
            message = (
                f"{last_error or 'Easyship did not finish in time.'} "
                f"{len(incomplete)} of {box_total} label(s) not confirmed — click Resume/Print "
                "label again to finish; completed boxes are never re-charged."
            )
            progress_state = "retry"
        for r in incomplete:
            db.execute(
                "UPDATE shipments SET status=%s, error_message=%s, updated_at=now() WHERE id=%s",
                ("error" if progress_state == "error" else "rated", message, r["id"]),
            )
        _set_group_progress(primary_id, progress_state,
                            boxes=_group_boxes_snapshot(_group_rows(group_id), state, box_errors),
                            message=message)
        return

    # Every box has its label — now (and only now) update the order and print.
    audit("label.buy", {
        "group_id": group_id,
        "boxes": box_total,
        "shipment_ids": [r["id"] for r in rows],
    }, user_id=user_id)
    _set_group_progress(primary_id, "finalizing",
                        boxes=_group_boxes_snapshot(rows, state),
                        message="All labels ready — updating order and printing…")
    writebacks = run_group_writebacks(group_id)
    printed = _print_group(group_id)
    _set_group_progress(primary_id, "done",
                        boxes=_group_boxes_snapshot(_group_rows(group_id), state),
                        extra={"printed": printed, "writebacks": writebacks})


# ============================================================ group status / label / print

@bp.get("/group/<group_id>")
@login_required
def group_status(group_id):
    rows = _group_rows(group_id)
    if not rows:
        return api_error("Shipment group not found", 404)
    return jsonify({
        "group_id": group_id,
        "shipments": [_row_to_json(r) for r in rows],
        "progress": rows[0]["progress"],
    })


def _group_label_bytes(group_id):
    rows = _group_rows(group_id)
    docs = []
    for row in rows:
        if row["label_path"] and os.path.exists(row["label_path"]):
            with open(row["label_path"], "rb") as f:
                data = f.read()
            docs.append((data, easyship.sniff_label_format(data, row["label_format"] or "pdf")))
    return easyship.merge_label_documents(docs)


@bp.get("/group/<group_id>/label")
@login_required
def group_label(group_id):
    data, fmt = _group_label_bytes(group_id)
    if not data:
        return api_error("No labels stored for this shipment", 404)
    import io
    response = send_file(
        io.BytesIO(data),
        mimetype=LABEL_MIMETYPES.get(fmt, "application/pdf"),
        download_name=f"labels-{group_id[:8]}.{fmt}",
        as_attachment=False,
    )
    response.headers["Content-Disposition"] = f'inline; filename="labels-{group_id[:8]}.{fmt}"'
    return response


def _print_group(group_id):
    """Network-print all labels of the group as one job when configured.
    Returns 'ok', an error string, or None in browser mode."""
    if (db.get_setting("print_mode") or "browser") != "network":
        return None
    try:
        import printer
        data, _fmt = _group_label_bytes(group_id)
        if not data:
            return "error: no label files stored"
        printer.network_print(data)
        return "ok"
    except Exception as e:
        return f"error: {e}"


@bp.post("/group/<group_id>/print")
@login_required
def group_print(group_id):
    try:
        import printer
        data, _fmt = _group_label_bytes(group_id)
        if not data:
            return api_error("No labels stored for this shipment", 404)
        printer.network_print(data)
    except Exception as e:
        return api_error(str(e))
    audit("label.print", {"group_id": group_id})
    return jsonify({"ok": True})


# ============================================================ writebacks

def run_group_writebacks(group_id):
    """Write tracking for the WHOLE group once: box 1's number to the order's
    tracking field, the rest appended (BackOffice Notes / Shopify numbers)."""
    rows = _group_rows(group_id)
    ready = [r for r in rows if r["tracking_number"]]
    if not ready:
        return {"skipped": "no tracking numbers yet"}
    primary = rows[0]
    numbers = [r["tracking_number"] for r in rows if r["tracking_number"]]
    results = {}
    errors = []

    if primary["source"] == "shopify" and not primary["writeback_shopify_at"]:
        try:
            import shopify_client
            fulfillment = shopify_client.fulfill_order(
                primary["shopify_store_id"], primary["shopify_order_id"],
                numbers[0], primary["courier_name"],
                all_numbers=numbers,
            )
            for r in rows:
                db.execute(
                    """UPDATE shipments SET writeback_shopify_at=now(),
                       shopify_fulfillment_id=%s, updated_at=now() WHERE id=%s""",
                    ((fulfillment or {}).get("id"), r["id"]),
                )
            results["shopify"] = "ok"
        except Exception as e:
            results["shopify"] = f"error: {e}"
            errors.append(f"Shopify: {e}")

    if primary["source"] == "backoffice" and not primary["writeback_backoffice_at"]:
        try:
            import backoffice
            total_cost = sum(float(r["shipping_cost"] or 0) for r in rows) or None
            backoffice.write_tracking(
                primary["backoffice_db_id"], primary["backoffice_invoice_id"],
                numbers[0], total_cost,
                extra_numbers=numbers[1:],
            )
            for r in rows:
                db.execute(
                    "UPDATE shipments SET writeback_backoffice_at=now(), updated_at=now() WHERE id=%s",
                    (r["id"],),
                )
            results["backoffice"] = "ok"
        except Exception as e:
            results["backoffice"] = f"error: {e}"
            errors.append(f"BackOffice: {e}")

    rows = _group_rows(group_id)
    done = (
        primary["source"] == "manual"
        or (primary["source"] == "shopify" and rows[0]["writeback_shopify_at"])
        or (primary["source"] == "backoffice" and rows[0]["writeback_backoffice_at"])
    )
    for r in rows:
        if done and r["status"] == "label_created":
            db.execute(
                "UPDATE shipments SET status='fulfilled', error_message=NULL, updated_at=now() WHERE id=%s",
                (r["id"],),
            )
        elif errors:
            db.execute(
                "UPDATE shipments SET error_message=%s, updated_at=now() WHERE id=%s",
                ("; ".join(errors), r["id"]),
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
    if row["group_id"]:
        results = run_group_writebacks(row["group_id"])
    else:
        results = _run_legacy_writebacks(shipment_id)
    updated = _get_with_username(shipment_id)
    return jsonify({**_row_to_json(updated), "writebacks": results})


def _run_legacy_writebacks(shipment_id):
    """Rows created before per-box groups existed."""
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    results = {}
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
                """UPDATE shipments SET writeback_shopify_at=now(), status='fulfilled',
                   shopify_fulfillment_id=%s, updated_at=now() WHERE id=%s""",
                ((fulfillment or {}).get("id"), shipment_id),
            )
            results["shopify"] = "ok"
        except Exception as e:
            results["shopify"] = f"error: {e}"
    if row["source"] == "backoffice" and not row["writeback_backoffice_at"]:
        try:
            import backoffice
            backoffice.write_tracking(
                row["backoffice_db_id"], row["backoffice_invoice_id"],
                row["tracking_number"], row["shipping_cost"],
                extra_numbers=(row["tracking_numbers"] or [])[1:],
            )
            db.execute(
                "UPDATE shipments SET writeback_backoffice_at=now(), status='fulfilled', updated_at=now() WHERE id=%s",
                (shipment_id,),
            )
            results["backoffice"] = "ok"
        except Exception as e:
            results["backoffice"] = f"error: {e}"
    return results


# ============================================================ list / detail

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
    sql += " ORDER BY s.created_at DESC, s.box_number ASC LIMIT %s"
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


# ============================================================ void / undo

@bp.post("/<int:shipment_id>/void")
@login_required
def void(shipment_id):
    """Undo a shipment: cancels the label(s) at Easyship and removes tracking
    from Shopify / BackOffice. For a multi-box group this undoes ALL boxes.
    Calling it again on a voided shipment retries any undo step that failed."""
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    if not row:
        return api_error("Shipment not found", 404)
    if row["status"] not in ("label_created", "fulfilled", "rated", "error", "voided"):
        return api_error(f"Shipment is {row['status']} — cannot void")

    if row["group_id"]:
        rows = db.query("SELECT * FROM shipments WHERE group_id = %s ORDER BY box_number",
                        (row["group_id"],))
    else:
        rows = [row]
    primary = rows[0]

    if row["status"] != "voided":
        all_ids = []
        for r in rows:
            all_ids += r["easyship_shipment_ids"] or ([r["easyship_shipment_id"]] if r["easyship_shipment_id"] else [])
        cancel_errors = easyship.cancel_all(all_ids)
        if cancel_errors:
            return api_error("; ".join(cancel_errors), 502)

    undo = {}
    errors = []
    numbers = [r["tracking_number"] for r in rows if r["tracking_number"]]

    if primary["writeback_shopify_at"]:
        try:
            import shopify_client
            fulfillment_gid = primary["shopify_fulfillment_id"]
            if not fulfillment_gid and numbers:
                fulfillment_gid = shopify_client.find_fulfillment_by_tracking(
                    primary["shopify_store_id"], primary["shopify_order_id"], numbers[0]
                )
            if fulfillment_gid:
                shopify_client.cancel_fulfillment(primary["shopify_store_id"], fulfillment_gid)
            for r in rows:
                db.execute(
                    """UPDATE shipments SET writeback_shopify_at=NULL,
                       shopify_fulfillment_id=NULL, updated_at=now() WHERE id=%s""",
                    (r["id"],),
                )
            undo["shopify"] = "fulfillment cancelled" if fulfillment_gid else "no matching fulfillment found"
        except Exception as e:
            undo["shopify"] = f"error: {e}"
            errors.append(f"Shopify undo: {e}")

    if primary["writeback_backoffice_at"]:
        try:
            import backoffice
            backoffice.clear_tracking(
                primary["backoffice_db_id"], primary["backoffice_invoice_id"],
                numbers[0] if numbers else primary["tracking_number"],
                extra_numbers=numbers[1:],
            )
            for r in rows:
                db.execute(
                    "UPDATE shipments SET writeback_backoffice_at=NULL, updated_at=now() WHERE id=%s",
                    (r["id"],),
                )
            undo["backoffice"] = "tracking number cleared"
        except Exception as e:
            undo["backoffice"] = f"error: {e}"
            errors.append(f"BackOffice undo: {e}")

    for r in rows:
        db.execute(
            "UPDATE shipments SET status='voided', error_message=%s, updated_at=now() WHERE id=%s",
            ("; ".join(errors) if errors else None, r["id"]),
        )
    audit("label.void", {
        "shipment_id": shipment_id,
        "group_id": row["group_id"],
        "boxes": len(rows),
        "undo": undo,
    })
    return jsonify({"ok": not errors, "undo": undo, "errors": errors, "boxes": len(rows)})

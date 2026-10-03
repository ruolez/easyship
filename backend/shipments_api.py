import json
import os
import threading
import time
import uuid

from flask import Blueprint, current_app, jsonify, request, send_file, session
from werkzeug.security import check_password_hash

import config
import db
import po_box
import profit
import providers
import tag_rules
from auth import admin_required, login_required
from providers import labels
from providers.base import LabelStatus, ProviderError
from util import api_error, audit, central_time

bp = Blueprint("shipments", __name__, url_prefix="/api/shipments")

LABEL_MIMETYPES = {"pdf": "application/pdf", "png": "image/png", "zpl": "text/plain"}


# Parcel dims are JSON strings straight from <input type=number>; the regex
# guard keeps a stray non-numeric value from aborting the whole query, and
# ::float8::text prints "12" / "12.5" the way the UI always labelled sizes.
def _dim(key):
    return (f"CASE WHEN s.parcels->0->>'{key}' ~ '^[0-9]*\\.?[0-9]+$' "
            f"THEN (s.parcels->0->>'{key}')::float8 END")


LIST_FROM = f"""
    FROM shipments s
    JOIN users u ON u.id = s.created_by
    LEFT JOIN shopify_stores ss ON ss.id = s.shopify_store_id
    LEFT JOIN backoffice_dbs bd ON bd.id = s.backoffice_db_id
    LEFT JOIN provider_instances pi ON pi.key = s.provider
    CROSS JOIN LATERAL (SELECT {_dim('length')} AS l, {_dim('width')} AS w, {_dim('height')} AS h) d
"""

# The Parcels list filters and sorts on these in SQL; they mirror the values
# _row_to_json derives in Python (service_name, courier_umbrella_name,
# provider_label), so a filter option always matches the rows it came from.
SERVICE_NAME_SQL = ("COALESCE(NULLIF(ss.name, ''), NULLIF(bd.name, ''), "
                    "CASE WHEN s.source = 'manual' THEN 'Manual' ELSE s.source END)")
CARRIER_SQL = "COALESCE(s.courier_umbrella_name, s.rate->>'umbrella_name')"
ACCOUNT_SQL = "COALESCE(pi.label, s.provider_label, s.provider, '')"
BOX_VOLUME_SQL = "CASE WHEN d.l > 0 AND d.w > 0 AND d.h > 0 THEN d.l * d.w * d.h END"
BOX_SIZE_SQL = ("CASE WHEN d.l > 0 AND d.w > 0 AND d.h > 0 "
                "THEN d.l::text || '×' || d.w::text || '×' || d.h::text END")

LIST_SELECT = f"""
    SELECT s.*, u.username AS created_by_username,
           ss.name AS store_name, bd.name AS db_name,
           pi.label AS provider_current_label,
           {BOX_SIZE_SQL} AS box_size
    {LIST_FROM}
"""

# Column sorts for the Parcels list. Missing numerics sort as -1 and missing
# text as '' (what the page did client-side), so no NULLS FIRST/LAST juggling.
SORT_SQL = {
    "ref": "LOWER(COALESCE(s.shopify_order_name, s.backoffice_invoice_number, '#' || s.id::text))",
    "user": "LOWER(u.username)",
    "store": f"LOWER({SERVICE_NAME_SQL})",
    "address": """LOWER(concat_ws(', ', NULLIF(s.destination->>'contact', ''),
        CASE WHEN s.destination->>'company' IS DISTINCT FROM s.destination->>'contact'
             THEN NULLIF(s.destination->>'company', '') END,
        NULLIF(s.destination->>'address1', ''), NULLIF(s.destination->>'address2', ''),
        NULLIF(s.destination->>'city', ''),
        NULLIF(concat_ws(' ', NULLIF(s.destination->>'state', ''), NULLIF(s.destination->>'zip', '')), '')))""",
    "boxes": "s.box_total",
    "size": f"COALESCE({BOX_VOLUME_SQL}, -1)",
    "weight": "COALESCE(s.total_weight_lb, -1)",
    "account": f"LOWER({ACCOUNT_SQL})",
    "courier": "LOWER(COALESCE(s.courier_name, ''))",
    "carrier": f"LOWER(COALESCE({CARRIER_SQL}, ''))",
    "cost": "COALESCE(s.shipping_cost, -1)",
    "tracking": "COALESCE(s.tracking_number, '')",
    "status": "s.status",
    "created": "s.created_at",
}
# The id tiebreak keeps pages disjoint when rows share every sort value.
DEFAULT_ORDER = "s.created_at DESC, s.box_number ASC, s.id ASC"
LIST_PAGE_MAX = 500
LIST_PAGE_DEFAULT = 100

REMOVED_PROVIDER = "This shipping integration has been removed — the label can no longer be managed here"


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
        "options": row.get("options") or {},
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
        "box_size": row.get("box_size") or "",
        "items": row["items"],
        "provider": row.get("provider") or "easyship",
        # Live alias while the instance exists, the snapshot after it's deleted.
        "provider_label": (row.get("provider_current_label") or row.get("provider_label")
                           or row.get("provider") or ""),
        "provider_shipment_id": row["easyship_shipment_id"],
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
    options = {"signature": tag_rules.normalize_signature((data.get("options") or {}).get("signature"))}
    preferred_service = (data.get("preferred_service") or "").strip()
    preferred_service_id = str(data.get("preferred_service_id") or "").strip()

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
                  destination, parcels, items, options, status, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s)
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
                json.dumps(options),
                session["user_id"],
            ),
            returning=True,
        )
        row_ids.append(row["id"])

    # The packer picks one provider up front; rate against just that one. Fall
    # back to every provider this user may ship with when none is specified.
    requested_provider = (data.get("provider") or "").strip()
    active_providers = providers.enabled_for_user(session["user_id"], session.get("role"))
    def _rates_refused(message, status=400):
        for rid in row_ids:
            db.execute(
                "UPDATE shipments SET status='error', error_message=%s, updated_at=now() WHERE id=%s",
                (message, rid),
            )
        return api_error(message, status)
    if not active_providers:
        return _rates_refused(
            "No shipping integrations are assigned to your account — ask an admin", 403)
    if requested_provider:
        chosen = [p for p in active_providers if p.name == requested_provider]
        if not chosen:
            return _rates_refused(
                f"Selected shipping provider '{requested_provider}' is not enabled for your account")
        active_providers = chosen

    draft_ids_by_provider = {}
    all_rates = []
    had_rates = False
    provider_errors = []
    warnings = []
    for provider in active_providers:
        try:
            drafts, rates, provider_warnings = provider.create_draft_shipments(
                destination, parcels, items, options)
        except ProviderError as e:
            provider_errors.append(f"{provider.label}: {e}")
            continue
        warnings.extend(provider_warnings)
        draft_ids_by_provider[provider.name] = [d.provider_shipment_id for d in drafts]
        excluded = provider.get_excluded_service_ids()
        for r in rates:
            had_rates = True
            if r.provider_service_id not in excluded:
                ui = r.to_ui()
                by = tag_rules.preferred_by(
                    r.courier_name, r.provider_service_id, preferred_service, preferred_service_id)
                ui["preferred"] = bool(by)
                ui["preferred_by"] = by
                all_rates.append(ui)

    if not draft_ids_by_provider:
        message = "; ".join(provider_errors) or "No shipping provider is enabled"
        for rid in row_ids:
            db.execute(
                "UPDATE shipments SET status='error', error_message=%s, updated_at=now() WHERE id=%s",
                (message, rid),
            )
        return api_error(message, 502)

    # Stash each provider's per-box draft ids so a chosen rate can be bought later.
    for i, rid in enumerate(row_ids):
        drafts_for_box = {name: ids[i] for name, ids in draft_ids_by_provider.items() if i < len(ids)}
        db.execute(
            """UPDATE shipments SET provider_drafts=%s, status='rated',
               error_message=NULL, updated_at=now() WHERE id=%s""",
            (json.dumps(drafts_for_box), rid),
        )
    # With a single provider quoting there is no ship-time choice, so record its
    # active shipment id now — a rated row then resumes/voids exactly as before.
    if len(draft_ids_by_provider) == 1:
        (only_name, only_ids), = draft_ids_by_provider.items()
        only_label = next(p.label for p in active_providers if p.name == only_name)
        for i, rid in enumerate(row_ids):
            if i < len(only_ids):
                sid = only_ids[i]
                db.execute(
                    """UPDATE shipments SET provider=%s, provider_label=%s, easyship_shipment_id=%s,
                       easyship_shipment_ids=%s, updated_at=now() WHERE id=%s""",
                    (only_name, only_label, sid, json.dumps([sid]), rid),
                )

    # A provider that failed while another quoted would otherwise vanish silently.
    warnings.extend(provider_errors)

    to_po_box = po_box.is_po_box(destination)
    hidden_carriers = []
    if to_po_box:
        all_rates, hidden_carriers = po_box.split_rates(all_rates)
        note = f" (hidden: {', '.join(hidden_carriers)})" if hidden_carriers else ""
        warnings.append(f"PO Box destination — only USPS-deliverable services are shown{note}.")

    if not all_rates:
        if hidden_carriers:
            return api_error(
                "This address is a PO Box — none of the quoted services deliver to PO Boxes "
                f"(quoted: {', '.join(hidden_carriers)}). Use a USPS service or ship to a street address.",
                422,
            )
        if had_rates:
            return api_error(
                "Every available courier service is excluded — adjust exclusions in Settings.", 422
            )
        message = "No rates available for this shipment. Check the address and parcel details."
        if box_total > 1:
            message = ("No single courier returned rates for every box. "
                       "Check each box's weight and dimensions.")
        return api_error(message, 422)

    all_rates.sort(key=lambda r: r["total_charge"])
    if (preferred_service or preferred_service_id) and not any(r["preferred"] for r in all_rates):
        if preferred_service:
            warnings.append(f"Preferred service \"{preferred_service}\" was not offered for this shipment.")
        else:
            warnings.append("The Auto Mode preset service was not offered for this shipment.")

    # Profit check: the order's economics are fetched here, by the server, and
    # snapshotted with the offered rates so the buy gate judges the real price.
    thresholds = profit.load_thresholds()
    economics = None
    if thresholds["enabled"] and source in profit.GATED_SOURCES:
        economics = profit.fetch_economics(source, data)
        for ui in all_rates:
            ui["profit"] = profit.evaluate(economics, ui["total_charge"], ui.get("currency"), thresholds)
        db.execute(
            "UPDATE shipments SET profit_check=%s, updated_at=now() WHERE id=%s",
            (json.dumps(profit.snapshot(economics, all_rates)), row_ids[0]),
        )
    return jsonify({
        "group_id": group_id,
        "shipment_id": row_ids[0],
        "shipment_ids": row_ids,
        "box_count": box_total,
        "rates": all_rates,
        "options": options,
        "warnings": warnings,
        "economics": economics,
        "po_box": to_po_box,
    })


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
    """Per-box status list built from DB rows plus in-flight provider state."""
    live_state = live_state or {}
    errors = errors or {}
    boxes = []
    for row in rows:
        box = {"box": row["box_number"], "shipment_id": row["id"]}
        sid = row["easyship_shipment_id"]
        if row["status"] in ("label_created", "fulfilled"):
            box.update(status="ready", tracking=row["tracking_number"])
        elif sid in live_state and live_state[sid]:
            status = live_state[sid].label_status
            if status == LabelStatus.READY:
                numbers = live_state[sid].tracking_numbers
                box.update(status="ready", tracking=numbers[0] if numbers else None)
            elif status == LabelStatus.FAILED:
                box["status"] = "failed"
            elif status == LabelStatus.NOT_CREATED:
                box["status"] = "purchasing"
            else:
                box["status"] = "generating"
        else:
            box["status"] = "purchasing" if row["status"] in ("rated", "error") else row["status"]
        if sid in errors and box["status"] in ("purchasing", "failed"):
            box["error"] = errors[sid][:600]
        boxes.append(box)
    return boxes


@bp.post("/group/<group_id>/buy")
@login_required
def group_buy(group_id):
    data = request.get_json(silent=True) or {}
    rows = _group_rows(group_id)
    if not rows:
        return api_error("Shipment group not found", 404)
    primary = rows[0]
    courier_service_id = data.get("courier_service_id") or primary["courier_service_id"]
    if not courier_service_id:
        return api_error("courier_service_id is required")
    rate = data.get("rate") or primary["rate"] or {}
    provider_name = (data.get("provider") or (rate or {}).get("provider")
                     or primary["provider"] or "easyship")
    chosen = [p for p in providers.enabled_for_user(session["user_id"], session.get("role"))
              if p.name == provider_name]
    if not chosen:
        return api_error(
            f"Shipping provider '{provider_name}' is not enabled for your account", 403)
    provider_label = chosen[0].label

    progress = primary["progress"] or {}
    if progress.get("state") == "buying":
        from datetime import datetime, timedelta, timezone
        if datetime.now(timezone.utc) - primary["updated_at"] < timedelta(minutes=5):
            return api_error("Label purchase already in progress", 409)

    def _draft_id(row):
        return (row["provider_drafts"] or {}).get(provider_name) or row["easyship_shipment_id"]

    targets = [r for r in rows if r["status"] in ("rated", "error") and _draft_id(r)]
    if not targets:
        return api_error("Nothing to purchase — all boxes already have labels or were voided")

    # Profit gate: a rate below the thresholds needs the bypass password. Once
    # cleared (either way) a Resume of the same rate is never asked again.
    thresholds = profit.load_thresholds()
    gate = profit.gate_for_buy(primary, provider_name, courier_service_id, thresholds)
    if gate and not profit.already_cleared(primary, provider_name, courier_service_id):
        if gate["below_threshold"]:
            supplied = data.get("bypass_password") or ""
            if not supplied:
                return jsonify({
                    "error": "This order is below the profit threshold — the bypass password is required",
                    "code": "profit_gate", "bypass": "required", "profit": gate,
                }), 403
            stored = db.get_setting(profit.SETTING_PASSWORD) or ""
            if not stored or not check_password_hash(stored, supplied):
                audit("profit.bypass_denied", {
                    "group_id": group_id, "provider": provider_name,
                    "courier_service_id": courier_service_id,
                })
                return jsonify({
                    "error": "Bypass password is incorrect",
                    "code": "profit_gate", "bypass": "wrong_password", "profit": gate,
                }), 403
            audit("profit.bypass", {
                "group_id": group_id, "source": primary["source"],
                "order": primary["shopify_order_name"] or primary["backoffice_invoice_number"],
                "provider": provider_name, "courier_service_id": courier_service_id,
                "courier_name": (rate or {}).get("courier_name"),
                **{k: gate[k] for k in ("revenue", "items_cost", "label_cost", "profit",
                                        "margin_pct", "reasons", "thresholds")},
            })
        profit.mark_cleared(primary["id"], provider_name, courier_service_id,
                            bypassed=gate["below_threshold"])

    for r in rows:
        # Never rewrite a finalized box — its easyship_shipment_id already holds
        # the purchased label/transaction id, which a Resume must not clobber.
        if r["status"] in ("label_created", "fulfilled"):
            continue
        draft_id = _draft_id(r)
        if draft_id:
            db.execute(
                """UPDATE shipments SET provider=%s, provider_label=%s, courier_service_id=%s, rate=%s,
                   easyship_shipment_id=%s, easyship_shipment_ids=%s, updated_at=now() WHERE id=%s""",
                (provider_name, provider_label, courier_service_id, json.dumps(rate),
                 draft_id, json.dumps([draft_id]), r["id"]),
            )
        else:
            db.execute(
                """UPDATE shipments SET provider=%s, provider_label=%s, courier_service_id=%s, rate=%s,
                   updated_at=now() WHERE id=%s""",
                (provider_name, provider_label, courier_service_id, json.dumps(rate), r["id"]),
            )

    _cancel_other_drafts(rows, provider_name)
    rows = _group_rows(group_id)
    _set_group_progress(primary["id"], "buying", boxes=_group_boxes_snapshot(rows))

    app = current_app._get_current_object()
    threading.Thread(
        target=_group_buy_worker,
        args=(app, group_id, provider_name, courier_service_id, rate, session["user_id"]),
        daemon=True,
    ).start()
    return jsonify({"started": True, "box_count": len(rows)})


def _cancel_other_drafts(rows, chosen_provider):
    """After a rate is picked, best-effort cancel the drafts created in the
    other providers so no unused shipments linger. No-op with one provider."""
    by_provider = {}
    for r in rows:
        for name, sid in (r["provider_drafts"] or {}).items():
            if name != chosen_provider and sid:
                by_provider.setdefault(name, []).append(sid)
    if not by_provider:
        return
    app = current_app._get_current_object()

    def worker():
        with app.app_context():
            for name, ids in by_provider.items():
                provider = providers.get_provider(name)
                if provider is None:
                    continue
                try:
                    provider.cancel_all(ids)
                except Exception:
                    pass

    threading.Thread(target=worker, daemon=True).start()


def _group_buy_worker(app, group_id, provider_name, courier_service_id, rate, user_id):
    with app.app_context():
        primary_id = None
        try:
            rows = _group_rows(group_id)
            primary_id = rows[0]["id"]
            _group_buy_impl(group_id, provider_name, courier_service_id, rate, user_id)
        except Exception as e:  # never leave the group stuck in 'buying'
            if primary_id:
                _set_group_progress(primary_id, "error", message=str(e))


def _split_cost(rate, box_total):
    """Fallback per-box charge when the provider didn't attribute a cost to the
    box: split the chosen rate's total evenly."""
    total = rate.get("total_charge")
    if total and box_total:
        return round(float(total) / box_total, 2)
    return None


def _finalize_row(provider, row, state, rate, box_total):
    """A box's label is ready: save its label file and complete its row."""
    numbers = state.tracking_numbers
    tracking = numbers[0] if numbers else None
    docs = provider.fetch_labels(state)
    label_bytes, label_format = labels.merge_label_documents(docs)
    label_path = None
    if label_bytes:
        os.makedirs(config.LABELS_DIR, exist_ok=True)
        label_path = os.path.join(config.LABELS_DIR, f"{row['id']}.{label_format or 'pdf'}")
        with open(label_path, "wb") as f:
            f.write(label_bytes)

    weight = sum(float(p.get("weight") or 0) for p in row["parcels"] or [])
    cost = state.cost if state.cost is not None else _split_cost(rate, box_total)
    # Persist the provider's post-purchase id. For Easyship this is unchanged
    # (the shipment id); for Shippo it's the transaction id, which void needs to
    # issue the refund against.
    active_id = state.provider_shipment_id
    db.execute(
        """UPDATE shipments SET
             courier_name=%s, courier_umbrella_name=%s,
             shipping_cost=%s, total_weight_lb=%s,
             tracking_number=%s, tracking_numbers=%s, label_path=%s, label_format=%s,
             easyship_shipment_id=%s, easyship_shipment_ids=%s,
             label_created_at=now(),
             status='label_created', error_message=NULL, updated_at=now()
           WHERE id=%s""",
        (
            state.courier_name or rate.get("courier_name"),
            state.courier_umbrella_name or rate.get("umbrella_name"),
            cost,
            round(weight, 2),
            tracking,
            json.dumps(numbers) if numbers else None,
            label_path,
            label_format or "pdf",
            active_id,
            json.dumps([active_id]) if active_id else None,
            row["id"],
        ),
    )


def _group_buy_impl(group_id, provider_name, courier_service_id, rate, user_id):
    provider = providers.get_provider(provider_name)
    rows = _group_rows(group_id)
    primary_id = rows[0]["id"]
    box_total = len(rows)
    if provider is None:
        for r in rows:
            db.execute(
                "UPDATE shipments SET status='error', error_message=%s, updated_at=now() WHERE id=%s",
                (REMOVED_PROVIDER, r["id"]),
            )
        _set_group_progress(primary_id, "error", message=REMOVED_PROVIDER)
        return
    targets = {r["easyship_shipment_id"]: r for r in rows
               if r["status"] in ("rated", "error") and r["easyship_shipment_id"]}
    sids = list(targets.keys())

    state = {}
    box_errors = {}
    last_error = None
    def record_state(sid, res):
        """Store a box's live state, keeping the provider's failure reason so
        the UI can show why a label was rejected instead of a bare 'failed'."""
        state[sid] = res
        if res.label_status == LabelStatus.FAILED and res.error_message:
            box_errors[sid] = res.error_message
            current_app.logger.warning(
                "%s label failed for shipment %s: %s", provider.label, sid, res.error_message)
        elif res.label_status != LabelStatus.FAILED:
            box_errors.pop(sid, None)

    results = provider.buy_labels(sids, courier_service_id)
    for sid in sids:
        res = results.get(sid)
        if isinstance(res, ProviderError):
            last_error = res
            box_errors[sid] = str(res)
            state[sid] = None
            current_app.logger.warning(
                "%s buy_labels error for shipment %s: %s", provider.label, sid, res)
        else:
            record_state(sid, res)

    finalized = set()

    def maybe_finalize():
        """Complete rows for boxes whose labels are ready — but the group
        result (writebacks, printing, done state) waits for ALL boxes."""
        for sid, row in targets.items():
            if sid in finalized:
                continue
            s = state.get(sid)
            if s and s.label_status == LabelStatus.READY:
                _finalize_row(provider, row, s, rate, box_total)
                finalized.add(sid)

    def pending():
        return [
            sid for sid in sids
            if sid not in finalized and not (
                state.get(sid) and state[sid].label_status == LabelStatus.FAILED
            )
        ]

    try:
        timeout_s = int(db.get_setting("label_timeout_seconds") or 180)
    except ValueError:
        timeout_s = 180
    try:
        rebuy_interval_s = max(int(db.get_setting("label_rebuy_interval_seconds") or 6), 3)
    except ValueError:
        rebuy_interval_s = 6
    deadline = time.monotonic() + max(timeout_s, 30)
    rebuy_next = {}

    maybe_finalize()
    _set_group_progress(primary_id, "buying",
                        boxes=_group_boxes_snapshot(_group_rows(group_id), state, box_errors))

    while pending() and time.monotonic() < deadline:
        time.sleep(3)
        refreshed = provider.poll_shipments(pending(), courier_service_id)
        for sid, res in refreshed.items():
            if isinstance(res, ProviderError):
                last_error = res
            else:
                record_state(sid, res)

        # A purchase request that was lost (rate limit / gateway) leaves the
        # shipment at not_created — polling alone would wait forever, so
        # re-issue it. One label max per shipment: can never double-charge.
        now = time.monotonic()
        rebuy_ids = [
            sid for sid in pending()
            if (not state.get(sid) or state[sid].label_status == LabelStatus.NOT_CREATED)
            and now >= rebuy_next.get(sid, 0)
        ]
        if rebuy_ids:
            for sid, res in provider.buy_labels(rebuy_ids, courier_service_id).items():
                rebuy_next[sid] = time.monotonic() + rebuy_interval_s
                if isinstance(res, ProviderError):
                    last_error = res
                    box_errors[sid] = str(res)
                else:
                    record_state(sid, res)

        maybe_finalize()
        _set_group_progress(primary_id, "buying",
                            boxes=_group_boxes_snapshot(_group_rows(group_id), state, box_errors))

    rows = _group_rows(group_id)
    incomplete = [r for r in rows if r["status"] not in ("label_created", "fulfilled")]
    if incomplete:
        failed = [sid for sid in sids
                  if state.get(sid) and state[sid].label_status == LabelStatus.FAILED]
        if failed:
            reasons = sorted({box_errors[sid] for sid in failed if box_errors.get(sid)})
            message = f"Label generation failed at {provider.label} for {len(failed)} of {box_total} box(es)"
            if reasons:
                message += ": " + " | ".join(reasons)
            progress_state = "error"
        else:
            message = (
                f"{last_error or (provider.label + ' did not finish in time.')} "
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
            docs.append((data, labels.sniff_label_format(data, row["label_format"] or "pdf")))
    return labels.merge_label_documents(docs)


@bp.get("/group/<group_id>/label")
@login_required
def group_label(group_id):
    data, fmt = _group_label_bytes(group_id)
    if not data:
        return api_error("No labels stored for this shipment", 404)
    if request.args.get("format") == "zpl":
        return _zpl_response(data, fmt, f"labels-{group_id[:8]}")
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
        data, fmt = _group_label_bytes(group_id)
        if not data:
            return "error: no label files stored"
        printer.print_label(data, fmt)
        return "ok"
    except Exception as e:
        return f"error: {e}"


@bp.post("/group/<group_id>/print")
@login_required
def group_print(group_id):
    try:
        import printer
        data, fmt = _group_label_bytes(group_id)
        if not data:
            return api_error("No labels stored for this shipment", 404)
        printer.print_label(data, fmt)
    except Exception as e:
        return api_error(str(e))
    audit("label.print", {"group_id": group_id})
    return jsonify({"ok": True})


# ============================================================ writebacks

def _ensure_shopify_order(rows):
    """The order gid for the group. When the ship page never got it (Shopify
    was unreachable at scan time) it is looked up from the scanned order
    number and saved, so the real order name shows everywhere afterwards."""
    import shopify_client
    primary = rows[0]
    if primary["shopify_order_id"]:
        return primary["shopify_order_id"]
    if not primary["shopify_store_id"]:
        raise shopify_client.ShopifyError(
            "No Shopify store linked to this shipment — use Send to Shopify in Parcels to pick the store and order")
    number = (primary["shopify_order_name"] or "").strip()
    if not number:
        raise shopify_client.ShopifyError(
            "No Shopify order number recorded — use Send to Shopify in Parcels to enter it")
    order = shopify_client.resolve_order(primary["shopify_store_id"], number)
    if not order:
        raise shopify_client.ShopifyError(f"Order {number} not found in the Shopify store")
    for r in rows:
        db.execute(
            "UPDATE shipments SET shopify_order_id=%s, shopify_order_name=%s, updated_at=now() WHERE id=%s",
            (order["id"], order["name"], r["id"]),
        )
    return order["id"]


def run_group_writebacks(group_id):
    """Write tracking for the WHOLE group once: box 1's number to the order's
    tracking field, the rest appended (BackOffice Notes / Shopify numbers)."""
    rows = _group_rows(group_id)
    ready = [r for r in rows if r["tracking_number"]]
    if not ready:
        return {"skipped": "no tracking numbers yet"}
    primary = rows[0]
    # Every number of every box (a box's shipment can carry several parcels),
    # deduped in box order.
    numbers = []
    for r in rows:
        for n in (r["tracking_numbers"] or ([r["tracking_number"]] if r["tracking_number"] else [])):
            if n and n not in numbers:
                numbers.append(n)
    results = {}
    errors = []

    if primary["source"] == "shopify" and not primary["writeback_shopify_at"]:
        try:
            import shopify_client
            order_gid = _ensure_shopify_order(rows)
            fulfillment = shopify_client.fulfill_order(
                primary["shopify_store_id"], order_gid,
                numbers[0], primary["courier_name"],
                all_numbers=numbers,
                umbrella_name=primary["courier_umbrella_name"],
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
            # An unavailable Shopify already names itself; keep that message
            # verbatim so the packer sees it is a retry-later situation.
            errors.append(str(e) if isinstance(e, shopify_client.ShopifyUnavailable) else f"Shopify: {e}")

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


@bp.post("/<int:shipment_id>/shopify-link")
@login_required
def link_shopify_order(shipment_id):
    """Attach a store + order number to a Shopify shipment that was bought
    without them, then push its tracking. Covers labels bought while Shopify
    was unreachable and rows that lost their order on the way."""
    import shopify_client
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    number = (data.get("order_number") or "").strip()
    if not store_id or not number:
        return api_error("store_id and order_number are required")
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    if not row:
        return api_error("Shipment not found", 404)
    if row["source"] != "shopify":
        return api_error("Only Shopify shipments can be linked to a Shopify order")
    if row["status"] not in ("label_created", "fulfilled"):
        return api_error("No label yet — nothing to send")
    if row["writeback_shopify_at"]:
        return api_error("This shipment's tracking is already on a Shopify order")
    if not db.query("SELECT 1 FROM shopify_stores WHERE id = %s", (store_id,), one=True):
        return api_error("Shopify store not found", 404)
    try:
        order = shopify_client.resolve_order(store_id, number)
    except shopify_client.ShopifyError as e:
        return api_error(str(e), 502)
    if not order:
        return api_error(f"Order {number} not found in that store", 404)
    rows = _group_rows(row["group_id"]) if row["group_id"] else [row]
    for r in rows:
        db.execute(
            """UPDATE shipments SET shopify_store_id=%s, shopify_order_id=%s, shopify_order_name=%s,
               updated_at=now() WHERE id=%s""",
            (store_id, order["id"], order["name"], r["id"]),
        )
    audit("shipment.link_shopify", {
        "shipment_ids": [r["id"] for r in rows],
        "store_id": store_id, "order_name": order["name"],
    })
    results = run_group_writebacks(row["group_id"]) if row["group_id"] else _run_legacy_writebacks(shipment_id)
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
                umbrella_name=row["courier_umbrella_name"],
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

def _int_arg(name, default, lo, hi):
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(int(raw), hi))
    except ValueError:
        raise ValueError(f"{name} must be a whole number")


def _list_where(args):
    """WHERE clause + params for the Parcels list, shared by the page query
    and the totals query so both always describe the same set of rows."""
    arg = lambda k: (args.get(k) or "").strip()
    sql = "TRUE"
    params = []
    q = arg("q")
    if q:
        sql += """ AND (s.tracking_number ILIKE %s OR s.shopify_order_name ILIKE %s
                   OR s.backoffice_invoice_number ILIKE %s OR s.destination->>'company' ILIKE %s
                   OR s.destination->>'contact' ILIKE %s OR s.destination->>'city' ILIKE %s)"""
        like = f"%{q}%"
        params += [like] * 6
    for key, expr in (("status", "s.status"), ("user", "u.username"), ("provider", "s.provider"),
                      ("store", SERVICE_NAME_SQL), ("service", "s.courier_name"),
                      ("carrier", CARRIER_SQL), ("size", BOX_SIZE_SQL)):
        if arg(key):
            sql += f" AND {expr} = ANY(%s)"
            params.append(arg(key).split(","))
    if arg("from"):
        sql += " AND (s.created_at AT TIME ZONE 'America/Chicago')::date >= %s"
        params.append(arg("from"))
    if arg("to"):
        sql += " AND (s.created_at AT TIME ZONE 'America/Chicago')::date <= %s"
        params.append(arg("to"))
    return sql, params


@bp.get("")
@login_required
def list_shipments():
    try:
        limit = _int_arg("limit", LIST_PAGE_DEFAULT, 1, LIST_PAGE_MAX)
        offset = _int_arg("offset", 0, 0, 10**9)
    except ValueError as e:
        return api_error(str(e))
    order = DEFAULT_ORDER
    sort = SORT_SQL.get((request.args.get("sort") or "").strip())
    if sort:
        order = f"{sort} {'DESC' if request.args.get('dir') == 'desc' else 'ASC'}, {DEFAULT_ORDER}"
    where, params = _list_where(request.args)
    agg = db.query(
        f"""SELECT COUNT(*) AS total,
                   COUNT(DISTINCT COALESCE(s.group_id, '#' || s.id::text)) AS shipments,
                   COALESCE(SUM(s.shipping_cost), 0) AS shipping_cost
            {LIST_FROM} WHERE {where}""",
        params, one=True)
    total = agg["total"]
    rows = []
    if total:
        # Rows voided or deleted while paging can leave the client past the
        # end; answer with the last page and echo the offset we actually used.
        if offset >= total:
            offset = ((total - 1) // limit) * limit
        rows = db.query(f"{LIST_SELECT} WHERE {where} ORDER BY {order} LIMIT %s OFFSET %s",
                        params + [limit, offset])
    return jsonify({
        "rows": [_row_to_json(r) for r in rows],
        "total": total,
        "shipments": agg["shipments"],
        "shipping_cost": float(agg["shipping_cost"]),
        "offset": offset,
        "limit": limit,
    })


@bp.get("/creators")
@login_required
def creators():
    rows = db.query(
        """SELECT DISTINCT u.username FROM shipments s
           JOIN users u ON u.id = s.created_by ORDER BY u.username"""
    )
    return jsonify([r["username"] for r in rows])


@bp.get("/providers")
@login_required
def shipped_providers():
    """Every shipping account that ever bought a label, as filter options —
    including accounts since disabled or deleted, which /api/providers/enabled
    would hide. Live alias first, then the snapshot kept on the parcel."""
    rows = db.query(
        """SELECT DISTINCT s.provider AS value,
                  COALESCE(pi.label, s.provider_label, s.provider) AS label
           FROM shipments s
           LEFT JOIN provider_instances pi ON pi.key = s.provider
           WHERE s.provider IS NOT NULL
           ORDER BY label"""
    )
    return jsonify([{"value": r["value"], "label": r["label"]} for r in rows])


@bp.get("/filter-options")
@login_required
def filter_options():
    """Distinct values for the Parcels list's Store / Service / Carrier / Size
    filters, drawn from every parcel rather than the page on screen."""
    def values(select, where="", order="ORDER BY 1"):
        return [r["value"] for r in db.query(
            f"SELECT DISTINCT {select} {LIST_FROM} WHERE {where or 'TRUE'} {order}")]
    return jsonify({
        "stores": values(f"{SERVICE_NAME_SQL} AS value"),
        "services": values("s.courier_name AS value", "s.courier_name IS NOT NULL AND s.courier_name <> ''"),
        "carriers": values(f"{CARRIER_SQL} AS value", f"{CARRIER_SQL} IS NOT NULL"),
        "sizes": values(f"{BOX_SIZE_SQL} AS value, {BOX_VOLUME_SQL} AS volume",
                        f"{BOX_SIZE_SQL} IS NOT NULL", "ORDER BY volume, value"),
    })


@bp.get("/<int:shipment_id>")
@login_required
def get_shipment(shipment_id):
    row = _get_with_username(shipment_id)
    if not row:
        return api_error("Shipment not found", 404)
    return jsonify(_row_to_json(row))


def _zpl_response(data, fmt, name):
    """The label rendered as ZPL (any source format), for Zebra Browser Print."""
    import io
    import printer
    try:
        zpl = labels.to_zpl(data, fmt, dpi=printer.printer_dpi())
    except Exception as e:
        return api_error(f"Could not convert {fmt.upper()} label to ZPL — {e}", 500)
    response = send_file(io.BytesIO(zpl), mimetype="text/plain",
                         download_name=f"{name}.zpl", as_attachment=False)
    response.headers["Content-Disposition"] = f'inline; filename="{name}.zpl"'
    return response


@bp.get("/<int:shipment_id>/label")
@login_required
def get_label(shipment_id):
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    if not row or not row["label_path"]:
        return api_error("No label stored for this shipment", 404)
    if not os.path.exists(row["label_path"]):
        return api_error("Label file missing from storage", 410)
    with open(row["label_path"], "rb") as f:
        data = f.read()
    fmt = labels.sniff_label_format(
        data, row["label_format"] if row["label_format"] in LABEL_MIMETYPES else "pdf")
    if request.args.get("format") == "zpl":
        return _zpl_response(data, fmt, f"label-{shipment_id}")
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
    """Diagnostic: the raw shipment object as the provider returns it right now."""
    row = db.query("SELECT * FROM shipments WHERE id = %s", (shipment_id,), one=True)
    if not row:
        return api_error("Shipment not found", 404)
    if not row["easyship_shipment_id"]:
        return api_error("Shipment was never sent to a provider")
    provider = providers.get_provider(row.get("provider") or "easyship")
    if provider is None:
        return api_error(REMOVED_PROVIDER, 409)
    try:
        return jsonify(provider.get_raw_shipment(row["easyship_shipment_id"]))
    except ProviderError as e:
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
        provider = providers.get_provider(primary.get("provider") or "easyship")
        if provider is None:
            return api_error(REMOVED_PROVIDER, 409)
        all_ids = []
        for r in rows:
            all_ids += r["easyship_shipment_ids"] or ([r["easyship_shipment_id"]] if r["easyship_shipment_id"] else [])
        cancel_errors = provider.cancel_all(all_ids)
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
                shopify_undo = shopify_client.remove_tracking(
                    primary["shopify_store_id"], primary["shopify_order_id"],
                    fulfillment_gid, numbers,
                )
            for r in rows:
                db.execute(
                    """UPDATE shipments SET writeback_shopify_at=NULL,
                       shopify_fulfillment_id=NULL, updated_at=now() WHERE id=%s""",
                    (r["id"],),
                )
            undo["shopify"] = shopify_undo if fulfillment_gid else "no matching fulfillment found"
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

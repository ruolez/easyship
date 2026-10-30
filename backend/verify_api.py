"""Verify page API: open an order by its number, scan its products, and the
admin side (history, wrong barcodes, case barcodes)."""
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, session

import barcodes
import db
import dbadmin
import shipper
import shopify_client
import verify
import verify_gate
import verify_store
from auth import admin_required, login_required, section_required, user_sections
from backoffice import BackofficeError
from util import CENTRAL, api_error, audit

bp = Blueprint("verify", __name__, url_prefix="/api/verify")

OVERRIDE = "verify_override"


def _can_override():
    return OVERRIDE in user_sections(session["user_id"], session.get("role"))


def _can_see_all():
    return session.get("role") == "admin" or _can_override()


def _prefixes(source):
    return verify.parse_prefixes(db.get_setting(verify.SKIP_PREFIX_KEYS[source]))


def _cases():
    return barcodes.case_lookup(verify_store.list_cases())


def _store_name(store_id):
    row = db.query("SELECT name FROM shopify_stores WHERE id = %s", (store_id,), one=True)
    return row["name"] if row else None


def _state(verification_id, **extra):
    v = verify_store.get(verification_id)
    lines = [{**l, "needs_tap": verify.needs_tap(l), "remaining": verify.remaining(l)} for l in v["lines"]]
    return jsonify({
        "id": v["id"],
        "order_name": v["order_name"],
        "source": v["source"],
        "source_name": _store_name(v["shopify_store_id"]) if v["source"] == "shopify" else v["source_db"],
        "shopify_store_id": v["shopify_store_id"],
        "shopify_order_id": v["shopify_order_id"],
        "customer": v["customer"],
        "status": v["status"],
        "forced": v["forced"],
        "started_at": v["started_at"].isoformat(),
        "started_by": v["started_by_name"],
        "verified_at": v["verified_at"].isoformat() if v["verified_at"] else None,
        "verified_by": v["verified_by_name"],
        "writebacks": {
            "shopify": {"state": v["shopify_tag_state"], "error": v["shopify_tag_error"]},
            "dbadmin": {"state": v["dbadmin_state"], "error": v["dbadmin_error"]},
        },
        "lines": lines,
        "progress": verify.progress(v["lines"]),
        "can_override": _can_override(),
        **extra,
    })


def _run_writebacks(verification_id):
    """After an order is verified: tag the Shopify order "Checked", or put
    the checker on the DB_Admin quotation. A failure is kept for Retry and
    never undoes the verification."""
    v = verify_store.get(verification_id)
    if v["status"] != "verified":
        return
    if v["source"] == "shopify" and v["shopify_tag_state"] in ("pending", "failed"):
        try:
            shopify_client.change_tags(v["shopify_store_id"], v["shopify_order_id"], add=[verify.CHECKED_TAG])
            verify_store.set_writeback(verification_id, "shopify", "done")
        except shopify_client.ShopifyError as e:
            verify_store.set_writeback(verification_id, "shopify", "failed", str(e))
    if v["source"] == "backoffice" and v["dbadmin_state"] in ("pending", "failed"):
        try:
            dbadmin.write_checker(v["order_name"], v["verified_by_name"])
            verify_store.set_writeback(verification_id, "dbadmin", "done")
        except dbadmin.DbAdminError as e:
            verify_store.set_writeback(verification_id, "dbadmin", "failed", str(e))
        except Exception as e:  # pymssql errors during the UPDATE
            verify_store.set_writeback(verification_id, "dbadmin", "failed", f"DB_Admin update failed: {e}")


def _after_change(verification_id, completed, **extra):
    if completed:
        audit("verify.complete", {"verification_id": verification_id})
        _run_writebacks(verification_id)
    return _state(verification_id, **extra)


def _load_order(number):
    """The scanned number → (order fields, verification lines). A Shopify
    store prefix picks Shopify; anything else is looked up in DB_Admin."""
    stores = db.query("SELECT id, name, prefix FROM shopify_stores WHERE is_active") or []
    try:
        store = verify.detect_store(number, stores)
    except ValueError as e:
        raise verify_store.VerifyError(str(e))
    if store:
        try:
            resolved = shopify_client.resolve_order(store["id"], number)
            if not resolved:
                raise verify_store.VerifyError(f"Order {number} was not found in {store['name']}", 404)
            nodes = shopify_client.get_picklist_orders(store["id"], [resolved["id"]])
        except shopify_client.ShopifyError as e:
            raise verify_store.VerifyError(f"Shopify ({store['name']}): {e}", 502)
        if not nodes:
            raise verify_store.VerifyError(f"Order {number} was not found in {store['name']}", 404)
        node = nodes[0]
        if node.get("cancelledAt"):
            raise verify_store.VerifyError(f"Order {node['name']} is cancelled in Shopify", 409)
        order = {
            "order_key": verify.shopify_order_key(store["id"], node["name"]),
            "source": "shopify",
            "order_name": node["name"],
            "shopify_store_id": store["id"],
            "shopify_order_id": node["id"],
            "customer": (node.get("customer") or {}).get("displayName")
                        or (node.get("shippingAddress") or {}).get("name"),
        }
        return order, verify.build_lines(verify.shopify_items(node), _prefixes("shopify"))

    try:
        status = dbadmin.find_quotation(number)
        if not status:
            raise verify_store.VerifyError(
                f"{number}: no Shopify store uses this prefix and DB_Admin has no open quotation with this number",
                404)
        try:
            quotation_id = int(str(status["Dop1"]).strip())
        except (TypeError, ValueError):
            raise verify_store.VerifyError(f"Quotation {number}: DB_Admin has no QuotationID (Dop1) for it", 409)
        header, rows = dbadmin.read_quotation(status["SourceDB"], quotation_id, number)
    except dbadmin.DbAdminError as e:
        raise verify_store.VerifyError(str(e), 502)
    except verify_store.VerifyError:
        raise
    except Exception as e:  # pymssql errors while querying
        raise verify_store.VerifyError(f"DB_Admin / BackOffice query failed: {e}", 502)
    order = {
        "order_key": verify.backoffice_order_key(header["QuotationNumber"]),
        "source": "backoffice",
        "order_name": (header["QuotationNumber"] or "").strip(),
        "source_db": (status["SourceDB"] or "").strip(),
        "quotation_id": quotation_id,
        "customer": (header.get("BusinessName") or status.get("BusinessName") or "").strip() or None,
    }
    return order, verify.build_lines(verify.quotation_items(rows), _prefixes("backoffice"))


@bp.errorhandler(verify_store.VerifyError)
def _verify_error(e):
    return api_error(str(e), e.status)


@bp.post("/open")
@section_required("verify")
def open_order():
    number = ((request.get_json(silent=True) or {}).get("number") or "").strip()
    if not number:
        return api_error("Scan or type the order number")
    order, lines = _load_order(number)
    existing = verify_store.live_by_key(order["order_key"])
    if not any(verify.is_active(l) for l in lines):
        if existing:
            return _state(existing["id"], changes=[], reopened=False)
        raise verify_store.VerifyError(f"Nothing on {order['order_name']} needs verifying — "
                                       "it may be fulfilled already or only hold skipped items", 409)
    verification_id, info = verify_store.open_order(order, lines, session["user_id"])
    if not existing:
        audit("verify.start", {"verification_id": verification_id, "order": order["order_name"]})
    completed = verify_store.live_by_key(order["order_key"])
    if completed and completed["status"] == "verified" and (completed["shopify_tag_state"] == "pending"
                                                            or completed["dbadmin_state"] == "pending"):
        _run_writebacks(verification_id)
    return _state(verification_id, **info)


@bp.get("/<int:verification_id>")
@section_required("verify")
def get_state(verification_id):
    return _state(verification_id)


def _body():
    return request.get_json(silent=True) or {}


@bp.post("/<int:verification_id>/scan")
@section_required("verify")
def scan(verification_id):
    data = _body()
    code = (data.get("code") or "").strip()
    if not code:
        return api_error("Empty scan")
    out = verify_store.scan(verification_id, code, _cases(), session["user_id"], data.get("client_id"))
    return _after_change(verification_id, out.pop("completed"), scan=out)


def _line_change(verification_id, line_id, kind, qty=None):
    completed = verify_store.set_line_qty(verification_id, line_id, kind, session["user_id"], qty,
                                          _body().get("client_id"))
    return _after_change(verification_id, completed)


@bp.post("/<int:verification_id>/lines/<int:line_id>/count")
@section_required("verify")
def count(verification_id, line_id):
    qty = _body().get("qty")
    if isinstance(qty, str) and qty.strip().isdigit():
        qty = int(qty.strip())
    return _line_change(verification_id, line_id, "count", qty)


@bp.post("/<int:verification_id>/lines/<int:line_id>/check-all")
@section_required("verify")
def check_all(verification_id, line_id):
    return _line_change(verification_id, line_id, "check_all")


@bp.post("/<int:verification_id>/lines/<int:line_id>/confirm")
@section_required("verify")
def confirm(verification_id, line_id):
    return _line_change(verification_id, line_id, "confirm")


@bp.post("/<int:verification_id>/lines/<int:line_id>/force")
@section_required(OVERRIDE)
def force_line(verification_id, line_id):
    audit("verify.force_line", {"verification_id": verification_id, "line_id": line_id})
    return _line_change(verification_id, line_id, "force_line")


@bp.post("/<int:verification_id>/force")
@section_required(OVERRIDE)
def force_order(verification_id):
    completed = verify_store.force_order(verification_id, session["user_id"])
    audit("verify.force_order", {"verification_id": verification_id})
    return _after_change(verification_id, completed)


@bp.post("/<int:verification_id>/undo")
@section_required("verify")
def undo(verification_id):
    undone = verify_store.undo(verification_id, session["user_id"])
    return _state(verification_id, undone=undone)


@bp.post("/<int:verification_id>/reset")
@section_required("verify")
def reset(verification_id):
    verify_store.reset(verification_id, session["user_id"])
    audit("verify.reset", {"verification_id": verification_id})
    return jsonify({"ok": True})


@bp.post("/<int:verification_id>/writebacks/retry")
@section_required("verify")
def retry_writebacks(verification_id):
    _run_writebacks(verification_id)
    return _state(verification_id)


@bp.get("/<int:verification_id>/events")
@section_required("verify")
def get_events(verification_id):
    v = verify_store.get(verification_id)
    if not _can_see_all() and session["user_id"] not in (v["started_by"], v["verified_by"]):
        return api_error("You can only see your own verifications", 403)
    rows = verify_store.events(verification_id)
    for row in rows:
        row["created_at"] = row["created_at"].isoformat()
        row["undone_at"] = row["undone_at"].isoformat() if row["undone_at"] else None
    return jsonify(rows)


def _day_bounds(value, end=False):
    day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=CENTRAL)
    return day + timedelta(days=1) if end else day


@bp.get("/history")
@section_required("verify")
def history():
    args = request.args
    filters = {
        "q": (args.get("q") or "").strip(),
        "status": args.get("status") if args.get("status") in ("in_progress", "verified", "reset") else None,
        "source": args.get("source") if args.get("source") in ("shopify", "backoffice") else None,
        "user_id": args.get("user_id", type=int),
        "forced": args.get("forced") == "1",
    }
    try:
        filters["since"] = _day_bounds(args["from"]) if args.get("from") else None
        filters["until"] = _day_bounds(args["to"], end=True) if args.get("to") else None
    except ValueError:
        return api_error("Dates must be YYYY-MM-DD")
    if not _can_see_all():
        today = datetime.now(CENTRAL).replace(hour=0, minute=0, second=0, microsecond=0)
        filters.update(user_id=session["user_id"], since=max(filters["since"] or today, today))
    rows = verify_store.history(filters)
    for row in rows:
        for key in ("started_at", "verified_at"):
            row[key] = row[key].isoformat() if row[key] else None
    users = db.query("SELECT id, username FROM users ORDER BY username") if _can_see_all() else []
    return jsonify({"rows": rows, "limit": verify_store.HISTORY_LIMIT, "users": users or [],
                    "can_see_all": _can_see_all()})


@bp.get("/status")
@login_required
def status():
    """The Scan page's check before Ship: the same answer Buy will give."""
    return jsonify(verify_gate.check(_shipment_from_args(request.args)))


def _shipment_from_args(args):
    return {
        "source": args.get("source"),
        "shopify_store_id": args.get("store_id", type=int),
        "shopify_order_id": args.get("order_id") or None,
        "shopify_order_name": args.get("order_name") or None,
        "backoffice_db_id": args.get("db_id", type=int),
        "backoffice_invoice_id": args.get("invoice_id", type=int),
        "backoffice_invoice_number": args.get("invoice_number") or None,
    }


# ---------- Wrong barcodes ----------

@bp.get("/wrong-barcodes")
@section_required("verify")
def wrong_barcodes():
    if not _can_see_all():
        return api_error("Admin or Verify override access required", 403)
    rows = verify_store.wrong_barcodes(include_fixed=request.args.get("fixed") == "1")
    for row in rows:
        for key in ("created_at", "fixed_at"):
            row[key] = row[key].isoformat() if row[key] else None
    return jsonify(rows)


@bp.post("/wrong-barcodes/fix")
@section_required("verify")
def fix_wrong_barcodes():
    if not _can_see_all():
        return api_error("Admin or Verify override access required", 403)
    ids = [int(i) for i in _body().get("ids") or [] if str(i).isdigit()]
    if not ids:
        return api_error("Nothing selected")
    verify_store.fix_wrong_barcodes(ids, session["user_id"])
    audit("verify.wrong_barcodes_fixed", {"count": len(ids)})
    return jsonify({"ok": True})


# ---------- Case barcodes (admin) ----------

@bp.get("/case-barcodes")
@section_required("verify")
def list_case_barcodes():
    rows = verify_store.list_cases()
    for row in rows:
        row["created_at"] = row["created_at"].isoformat()
    return jsonify(rows)


def _case_conflict(barcode, exclude_id=None):
    key = barcodes.barcode_key(barcode)
    for row in verify_store.list_cases():
        if row["id"] != exclude_id and barcodes.barcode_key(row["barcode"]) == key:
            return row
    return None


@bp.post("/case-barcodes")
@admin_required
def add_case_barcode():
    data = _body()
    barcode = (data.get("barcode") or "").strip()
    unit = (data.get("unit_barcode") or "").strip()
    qty = data.get("quantity")
    qty = int(qty) if str(qty or "").strip().isdigit() else 0
    if not barcodes.barcode_key(barcode) or not barcodes.barcode_key(unit):
        return api_error("Case barcode and product barcode are required")
    if barcodes.barcode_key(barcode) == barcodes.barcode_key(unit):
        return api_error("The case barcode must differ from the product barcode")
    if qty < 2:
        return api_error("Units per case must be 2 or more")
    taken = _case_conflict(barcode)
    if taken:
        return api_error(f"Case barcode {taken['barcode']} is already set up ({taken['quantity']} × {taken['unit_barcode']})", 409)
    row = db.execute(
        "INSERT INTO case_barcodes (barcode, unit_barcode, quantity, created_by) VALUES (%s, %s, %s, %s) RETURNING id",
        (barcode, unit, qty, session["user_id"]), returning=True,
    )
    audit("verify.case_add", {"barcode": barcode, "unit_barcode": unit, "quantity": qty})
    return jsonify({"ok": True, "id": row["id"]})


@bp.delete("/case-barcodes/<int:case_id>")
@admin_required
def delete_case_barcode(case_id):
    row = db.query("SELECT * FROM case_barcodes WHERE id = %s", (case_id,), one=True)
    if not row:
        return api_error("Case barcode not found", 404)
    db.execute("DELETE FROM case_barcodes WHERE id = %s", (case_id,))
    audit("verify.case_delete", {"barcode": row["barcode"], "unit_barcode": row["unit_barcode"],
                                 "quantity": row["quantity"]})
    return jsonify({"ok": True})


@bp.post("/case-barcodes/import")
@admin_required
def import_case_barcodes():
    """Copy the old Shipper app's case barcodes. Codes already set up here are
    left as they are."""
    try:
        rows = shipper.list_case_barcodes()
    except (shipper.ShipperError, BackofficeError) as e:
        return api_error(str(e), 502)
    if rows is None:
        return api_error("The Shipper database is not configured — set it up in Settings first")
    known = {barcodes.barcode_key(r["barcode"]) for r in verify_store.list_cases()}
    added = skipped = 0
    for row in rows:
        key = barcodes.barcode_key(row["barcode"])
        qty = int(row["quantity"] or 0)
        if not key or key in known or not barcodes.barcode_key(row["unit_barcode"]) or qty < 1:
            skipped += 1
            continue
        db.execute("INSERT INTO case_barcodes (barcode, unit_barcode, quantity, created_by) VALUES (%s, %s, %s, %s)",
                   (row["barcode"].strip(), row["unit_barcode"].strip(), qty, session["user_id"]))
        known.add(key)
        added += 1
    audit("verify.case_import", {"added": added, "skipped": skipped})
    return jsonify({"ok": True, "added": added, "skipped": skipped})

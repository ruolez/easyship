import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request, session

import backoffice
import db
import order_risk
import picklist_rules
import picklists
from auth import section_required
from shopify_client import (PICKLIST_DAYS, ShopifyError, get_order_fraud, get_picklist_orders,
                            list_picklist_orders)
from util import CENTRAL, api_error, audit, central_time

bp = Blueprint("picklists", __name__, url_prefix="/api/picklists")

SECTION = "picklists"
# A pick list still `creating` this long after it started never finished.
STALE_CREATING = timedelta(minutes=5)
STORE_WORKERS = 8

log = logging.getLogger(__name__)


def _config():
    raw_db = (db.get_setting("picklist_backoffice_db_id") or "").strip()
    return {
        "db_id": int(raw_db) if raw_db.isdigit() else None,
        "status": int(db.get_setting("picklist_quote_status") or 2),
        "prefix": (db.get_setting("picklist_title_prefix") or "PL").strip(),
        "tag": (db.get_setting("picklist_number_tag") or "19").strip(),
    }


def _db_id_or_error():
    db_id = _config()["db_id"]
    if not db_id:
        return None, api_error("Choose the BackOffice database for pick lists in Settings → Pick lists")
    return db_id, None


def _store_names(store_ids):
    rows = db.query("SELECT id, name FROM shopify_stores WHERE id = ANY(%s)", (list(store_ids),))
    return {r["id"]: r["name"] for r in rows or []}


def _per_store(store_ids, fetch):
    """fetch(store_id) for every store at once → [(store_id, result, error)].
    Each store runs in its own app context (its own DB connection); Shopify
    retries happen inside fetch, so an error here is what is left after them.
    One store failing never stops the others."""
    app = current_app._get_current_object()

    def run(store_id):
        with app.app_context():
            try:
                return store_id, fetch(store_id), None
            except ShopifyError as e:
                return store_id, None, str(e)
            except Exception as e:  # noqa: BLE001 — reported to the user, never a 500 for every store
                log.exception("Pick list fetch failed for store %s", store_id)
                return store_id, None, f"Unexpected error: {e}"

    if not store_ids:
        return []
    with ThreadPoolExecutor(max_workers=min(STORE_WORKERS, len(store_ids))) as pool:
        return list(pool.map(run, store_ids))


@bp.get("/orders")
@section_required(SECTION)
def eligible_orders():
    """Every eligible order of the chosen stores, each with the saved rules it
    matches so the page toggles rules without asking again. A store Shopify
    cannot answer for is reported and the others still load."""
    try:
        store_ids = [int(s) for s in (request.args.get("store_ids") or "").split(",") if s.strip()]
    except ValueError:
        return api_error("store_ids must be a comma-separated list of ids")
    rules = picklist_rules.load_rules()
    reserved = picklists.active_order_keys(store_ids) if store_ids else set()
    names = _store_names(store_ids) if store_ids else {}
    orders, errors = [], []
    for store_id, nodes, error in _per_store(store_ids, list_picklist_orders):
        if error:
            errors.append({"store_id": store_id, "store_name": names.get(store_id), "error": error})
            continue
        for node in nodes:
            order = picklists.from_shopify_order(node, store_id)
            if picklists.ineligible_reason(order, reserved):
                continue
            order["rule_ids"] = picklist_rules.matched_rule_ids(rules, order)
            orders.append(order)
    orders.sort(key=lambda o: (o["created_at"], o["name"]))
    return jsonify({
        "orders": orders,
        "errors": errors,
        "days": PICKLIST_DAYS,
        "rules": [{"id": r["id"], "name": r["name"], "action": r["action"]} for r in rules],
    })


def _address(addr):
    addr = addr or {}
    return {
        "name": " ".join(p for p in (addr.get("firstName"), addr.get("lastName")) if p),
        "address1": addr.get("address1"),
        "address2": addr.get("address2"),
        "city": addr.get("city"),
        "state": addr.get("provinceCode"),
        "zip": addr.get("zip"),
        "country": addr.get("countryCodeV2"),
        "phone": addr.get("phone"),
    } if addr else None


@bp.get("/orders/risk")
@section_required(SECTION)
def order_fraud():
    store_id = request.args.get("store_id", type=int)
    order_gid = (request.args.get("order_id") or "").strip()
    if not store_id or not order_gid:
        return api_error("store_id and order_id are required")
    try:
        node = get_order_fraud(store_id, order_gid)
    except ShopifyError as e:
        return api_error(str(e), 502)
    risk = order_risk.order_risk(node)
    if risk and risk["ip"]:
        risk["ip_location"] = order_risk.ip_location(risk["ip"])
    customer = node.get("customer") or {}
    total = (node.get("totalPriceSet") or {}).get("shopMoney") or {}
    spent = customer.get("amountSpent") or {}
    return jsonify({
        "name": node.get("name"),
        "created_at": (node.get("createdAt") or "")[:10],
        "email": node.get("email"),
        "phone": node.get("phone"),
        "tags": node.get("tags") or [],
        "financial_status": node.get("displayFinancialStatus"),
        "fulfillment_status": node.get("displayFulfillmentStatus"),
        "total": total.get("amount"),
        "currency": total.get("currencyCode"),
        "items": node.get("subtotalLineItemsQuantity"),
        "shipping_method": (node.get("shippingLine") or {}).get("title"),
        "customer": {
            "name": customer.get("displayName"),
            "orders_count": customer.get("numberOfOrders"),
            "since": (customer.get("createdAt") or "")[:10] or None,
            "amount_spent": spent.get("amount"),
            "tags": customer.get("tags") or [],
        } if customer else None,
        "products": picklists.order_products(node),
        "shipping_address": _address(node.get("shippingAddress")),
        "billing_address": _address(node.get("billingAddress")),
        "risk": risk,
    })


@bp.get("/sales-reps")
@section_required(SECTION)
def sales_reps():
    db_id, error = _db_id_or_error()
    if error:
        return error
    try:
        return jsonify(backoffice.list_sales_reps(db_id))
    except backoffice.BackofficeError as e:
        return api_error(str(e), 502)


@bp.get("/customers")
@section_required(SECTION)
def customers():
    db_id, error = _db_id_or_error()
    if error:
        return error
    try:
        return jsonify(backoffice.search_customers(db_id, request.args.get("q") or ""))
    except backoffice.BackofficeError as e:
        return api_error(str(e), 502)


def _selection(data):
    """[(store_id, order_gid)] from the request body, duplicates dropped."""
    picked = []
    for item in data.get("orders") or []:
        try:
            key = (int(item["store_id"]), str(item["order_id"]).strip())
        except (KeyError, TypeError, ValueError):
            continue
        if key[1] and key not in picked:
            picked.append(key)
    return picked


def _build(selection, db_id):
    """Re-read the chosen orders live and work out the pick list: the orders
    that go on it, those left out (with why), the lines grouped by barcode
    and the BackOffice items behind them. Raises ShopifyError / BackofficeError
    — a store Shopify cannot answer for stops the build, since its orders
    would otherwise vanish from the pick list unnoticed."""
    by_store = {}
    for store_id, gid in selection:
        by_store.setdefault(store_id, []).append(gid)
    reserved = picklists.active_order_keys(by_store)
    fetched = _per_store(list(by_store), lambda store_id: get_picklist_orders(store_id, by_store[store_id]))
    failed = [(store_id, error) for store_id, _, error in fetched if error]
    if failed:
        names = _store_names([s for s, _ in failed])
        raise ShopifyError("; ".join(f"{names.get(s, f'Store {s}')}: {e}" for s, e in failed))
    eligible, excluded = [], []
    for store_id, nodes, _ in fetched:
        gids = by_store[store_id]
        found = {n["id"]: n for n in nodes}
        for gid in gids:
            if gid not in found:
                excluded.append({"store_id": store_id, "id": gid, "name": gid.rsplit("/", 1)[-1],
                                 "reasons": ["Order not found in Shopify"]})
                continue
            order = picklists.from_shopify_order(found[gid], store_id)
            reason = picklists.ineligible_reason(order, reserved)
            if reason:
                excluded.append({**picklists.summary(order), "reasons": [reason]})
            else:
                eligible.append(order)
    items = backoffice.find_items_by_upc(db_id, {line["barcode"] for o in eligible for line in o["lines"]})
    included, blocked = picklists.split_blocked(eligible, set(items))
    return included, excluded + blocked, picklists.group_lines(included, items), items


def _totals(orders, lines):
    return {"orders": len(orders), "units": sum(o["units"] for o in orders), "lines": len(lines)}


@bp.post("/preview")
@section_required(SECTION)
def preview():
    db_id, error = _db_id_or_error()
    if error:
        return error
    selection = _selection(request.get_json(silent=True) or {})
    if not selection:
        return api_error("Select at least one order")
    try:
        included, excluded, lines, _items = _build(selection, db_id)
    except ShopifyError as e:
        return api_error(str(e), 502)
    except backoffice.BackofficeError as e:
        return api_error(str(e), 502)
    return jsonify({
        "orders": [picklists.summary(o) for o in included],
        "excluded": excluded,
        "lines": lines,
        "totals": _totals(included, lines),
    })


@bp.post("")
@section_required(SECTION)
def create():
    """Reserve the orders, then write the BackOffice quotation; a failed
    quotation releases the orders again so nothing is left half-done."""
    cfg = _config()
    db_id, error = _db_id_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    selection = _selection(data)
    customer_id = data.get("customer_id")
    rep_id = data.get("sales_rep_id")
    if not selection:
        return api_error("Select at least one order")
    if not isinstance(customer_id, int) or not isinstance(rep_id, int):
        return api_error("Choose a customer and a sales rep")
    try:
        rep = next((r for r in backoffice.list_sales_reps(db_id) if r["id"] == rep_id), None)
        if not rep:
            return api_error("That sales rep is not in BackOffice")
        included, excluded, lines, items = _build(selection, db_id)
    except ShopifyError as e:
        return api_error(str(e), 502)
    except backoffice.BackofficeError as e:
        return api_error(str(e), 502)
    if not included:
        return jsonify({"error": "None of the selected orders can go on a pick list", "excluded": excluded}), 409
    customer = {"id": customer_id, "name": str(data.get("customer_name") or "").strip()}
    try:
        picklist_id = picklists.reserve(session["user_id"], db_id, customer, rep, included, lines)
    except picklists.ReservationConflict as e:
        return jsonify({"error": str(e), "orders": e.order_names}), 409

    names = _store_names({o["store_id"] for o in included})
    totals = _totals(included, lines)
    now = datetime.now(CENTRAL).replace(tzinfo=None, second=0, microsecond=0)
    try:
        quotation_id, number, business_name = backoffice.create_quotation(
            db_id,
            now=now,
            number_prefix=picklists.number_prefix(now, cfg["tag"]),
            customer_id=customer_id,
            sales_rep_id=rep_id,
            status=cfg["status"],
            title=f"{cfg['prefix']} {picklist_id}".strip(),
            notes=f"EasyShip pick list {picklist_id}: {totals['orders']} orders, {totals['units']} units",
            memo="\n".join(f"{names.get(o['store_id'], '')} {o['name']}".strip() for o in included),
            lines=[(items[g["barcode"]], g["qty"]) for g in lines],
        )
    except backoffice.BackofficeError as e:
        picklists.release(picklist_id)
        audit("picklist.failed", {"picklist": picklist_id, "error": str(e)})
        return api_error(str(e), 502)
    picklists.activate(picklist_id, quotation_id, number, business_name or customer["name"])
    audit("picklist.create", {"picklist": picklist_id, "quotation": number, "orders": totals["orders"],
                              "units": totals["units"], "customer_id": customer_id, "sales_rep_id": rep_id})
    return jsonify({"id": picklist_id, "quotation_number": number, "totals": totals, "excluded": excluded})


def _row(p):
    return {
        "id": p["id"],
        "status": p["status"],
        "quotation_id": p["quotation_id"],
        "quotation_number": p["quotation_number"],
        "customer_id": p["customer_id"],
        "customer_name": p["customer_name"],
        "sales_rep_name": p["sales_rep_name"],
        "order_count": p["order_count"],
        "unit_count": p["unit_count"],
        "line_count": p["line_count"],
        "created_by": p.get("created_by_name"),
        "created_at": central_time(p["created_at"]),
        "cancelled_at": central_time(p["cancelled_at"]),
    }


@bp.get("")
@section_required(SECTION)
def history():
    return jsonify([_row(p) for p in picklists.list_picklists()])


@bp.get("/<int:picklist_id>")
@section_required(SECTION)
def detail(picklist_id):
    p = picklists.get_picklist(picklist_id)
    if not p:
        return api_error("Pick list not found", 404)
    return jsonify({**_row(p), **picklists.picklist_detail(picklist_id)})


@bp.post("/<int:picklist_id>/cancel")
@section_required(SECTION)
def cancel(picklist_id):
    """Delete the BackOffice quotation first; only once it is gone are the
    orders freed, so they never come back while the quotation still exists."""
    p = picklists.get_picklist(picklist_id)
    if not p:
        return api_error("Pick list not found", 404)
    if p["status"] == "cancelled":
        return api_error("This pick list is already cancelled")
    if p["status"] == "creating" and datetime.now(timezone.utc) - p["created_at"] < STALE_CREATING:
        return api_error("This pick list is still being created — try again in a few minutes", 409)
    deleted = False
    if p["quotation_id"]:
        try:
            deleted = backoffice.delete_quotation(p["backoffice_db_id"], p["quotation_id"], p["quotation_number"])
        except backoffice.BackofficeError as e:
            return api_error(str(e), 502)
    picklists.cancel(picklist_id, session["user_id"])
    audit("picklist.cancel", {"picklist": picklist_id, "quotation": p["quotation_number"],
                              "quotation_deleted": deleted})
    return jsonify({"ok": True, "quotation_deleted": deleted})

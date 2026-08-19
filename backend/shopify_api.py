from flask import Blueprint, jsonify, request

import tag_rules
from auth import admin_required, login_required
from shopify_client import ShopifyError, find_order_by_number, get_order, list_open_orders
from util import api_error

bp = Blueprint("shopify", __name__, url_prefix="/api/shopify")


@bp.get("/lookup")
@login_required
def lookup():
    store_id = request.args.get("store_id", type=int)
    number = (request.args.get("number") or "").strip()
    if not store_id or not number:
        return api_error("store_id and number are required")
    try:
        order_gid = find_order_by_number(store_id, number)
        if not order_gid:
            return api_error(f"Order {number} not found", 404)
        return jsonify(_with_tag_rules(get_order(store_id, order_gid)))
    except ShopifyError as e:
        return api_error(str(e), 502)


@bp.get("/orders")
@admin_required
def orders():
    """The open-orders list behind the Orders page (admin only); packers reach
    orders through Scan, which uses /lookup and the order detail route."""
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return api_error("store_id is required")
    try:
        return jsonify(list_open_orders(store_id))
    except ShopifyError as e:
        return api_error(str(e), 502)


@bp.get("/orders/<path:order_gid>")
@login_required
def order_detail(order_gid):
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return api_error("store_id is required")
    try:
        return jsonify(_with_tag_rules(get_order(store_id, order_gid)))
    except ShopifyError as e:
        return api_error(str(e), 502)


def _with_tag_rules(order):
    """What the order's tags imply for shipping, resolved server-side so the
    rules never need to leave Settings."""
    order["tag_rules"] = tag_rules.resolve(tag_rules.load_rules(), order.get("tags"))
    return order

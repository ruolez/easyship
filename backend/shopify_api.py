from flask import Blueprint, jsonify, request

from auth import login_required
from shopify_client import ShopifyError, get_order, list_open_orders
from util import api_error

bp = Blueprint("shopify", __name__, url_prefix="/api/shopify")


@bp.get("/orders")
@login_required
def orders():
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
        return jsonify(get_order(store_id, order_gid))
    except ShopifyError as e:
        return api_error(str(e), 502)

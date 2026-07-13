from flask import Blueprint, jsonify, request

import shipper
from auth import login_required
from backoffice import BackofficeError
from util import api_error

bp = Blueprint("shipper", __name__, url_prefix="/api/shipper")


@bp.get("/check")
@login_required
def check():
    source = request.args.get("source")
    number = (request.args.get("number") or "").strip()
    if source not in ("shopify", "backoffice") or not number:
        return api_error("source (shopify|backoffice) and number are required")
    try:
        if source == "shopify":
            return jsonify(shipper.check_shopify(number))
        db_id = request.args.get("db_id", type=int)
        if not db_id:
            return api_error("db_id is required for backoffice source")
        return jsonify(shipper.check_backoffice(db_id, number))
    except (shipper.ShipperError, BackofficeError) as e:
        return jsonify({"status": "unavailable", "checked_at": None, "detail": str(e)})

from flask import Blueprint, jsonify, request

from auth import login_required
from backoffice import BackofficeError, get_invoice, list_open_invoices
from util import api_error

bp = Blueprint("backoffice", __name__, url_prefix="/api/backoffice")


@bp.get("/invoices")
@login_required
def invoices():
    days = request.args.get("days", default=14, type=int)
    q = (request.args.get("q") or "").strip()
    try:
        return jsonify(list_open_invoices(days=days, q=q))
    except BackofficeError as e:
        return api_error(str(e), 502)


@bp.get("/invoices/<int:invoice_id>")
@login_required
def invoice_detail(invoice_id):
    try:
        return jsonify(get_invoice(invoice_id))
    except BackofficeError as e:
        return api_error(str(e), 502)

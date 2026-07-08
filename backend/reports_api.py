from flask import Blueprint, jsonify

import db
from auth import login_required

bp = Blueprint("reports", __name__, url_prefix="/api/reports")


@bp.get("/summary")
@login_required
def summary():
    by_user = db.query(
        """SELECT u.username, COUNT(*) AS labels, COALESCE(SUM(s.shipping_cost), 0) AS cost
           FROM shipments s JOIN users u ON u.id = s.created_by
           WHERE s.status IN ('label_created', 'fulfilled')
           GROUP BY u.username ORDER BY labels DESC"""
    )
    by_courier = db.query(
        """SELECT courier_name, COUNT(*) AS labels, COALESCE(SUM(shipping_cost), 0) AS cost
           FROM shipments WHERE status IN ('label_created', 'fulfilled')
           GROUP BY courier_name ORDER BY labels DESC"""
    )
    by_day = db.query(
        """SELECT (created_at AT TIME ZONE 'America/Chicago')::date AS day,
                  COUNT(*) AS labels, COALESCE(SUM(shipping_cost), 0) AS cost
           FROM shipments WHERE status IN ('label_created', 'fulfilled')
             AND created_at > now() - interval '30 days'
           GROUP BY day ORDER BY day DESC"""
    )
    return jsonify({
        "by_user": [{**r, "cost": float(r["cost"])} for r in by_user],
        "by_courier": [{**r, "cost": float(r["cost"])} for r in by_courier],
        "by_day": [{**r, "day": str(r["day"]), "cost": float(r["cost"])} for r in by_day],
    })

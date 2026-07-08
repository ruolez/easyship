import json
from zoneinfo import ZoneInfo

from flask import jsonify, session

import db

CENTRAL = ZoneInfo("America/Chicago")


def central_time(dt):
    if dt is None:
        return None
    return dt.astimezone(CENTRAL).strftime("%m/%d/%Y %I:%M %p")


def api_error(message, status=400):
    return jsonify({"error": message}), status


def audit(action, detail=None):
    db.execute(
        "INSERT INTO audit_log (user_id, action, detail) VALUES (%s, %s, %s)",
        (session.get("user_id"), action, json.dumps(detail) if detail is not None else None),
    )

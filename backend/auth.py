from functools import wraps

from flask import Blueprint, jsonify, request, session
from werkzeug.security import check_password_hash

import db
from util import api_error, audit

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return api_error("Not authenticated", 401)
        return f(*args, **kwargs)

    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return api_error("Not authenticated", 401)
        if session.get("role") != "admin":
            return api_error("Admin access required", 403)
        return f(*args, **kwargs)

    return wrapper


# Sections an admin grants per user (users.allowed_sections); admins have all.
SECTIONS = {"picklists": "Pick lists", "verify": "Verify", "verify_override": "Verify override"}


def sanitize_sections(values):
    """Known section keys only, in SECTIONS order."""
    wanted = {str(v) for v in values or []}
    return [key for key in SECTIONS if key in wanted]


def user_sections(user_id, role):
    """Read fresh on every call, so revoking access takes effect at once."""
    if role == "admin":
        return list(SECTIONS)
    row = db.query(
        "SELECT allowed_sections FROM users WHERE id = %s AND is_active", (user_id,), one=True
    )
    return sanitize_sections((row or {}).get("allowed_sections"))


def section_required(section):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return api_error("Not authenticated", 401)
            if section not in user_sections(session["user_id"], session.get("role")):
                return api_error(f"You don't have access to {SECTIONS[section]}", 403)
            return f(*args, **kwargs)

        return wrapper

    return decorator


@bp.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    user = db.query(
        "SELECT * FROM users WHERE username = %s AND is_active", (username,), one=True
    )
    if not user or not check_password_hash(user["password_hash"], password):
        return api_error("Invalid username or password", 401)
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user["role"]
    audit("auth.login")
    return jsonify({"id": user["id"], "username": user["username"], "role": user["role"]})


@bp.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@bp.get("/me")
@login_required
def me():
    return jsonify({
        "id": session["user_id"],
        "username": session["username"],
        "role": session["role"],
        "sections": user_sections(session["user_id"], session["role"]),
    })

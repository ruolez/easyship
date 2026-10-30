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


def user_ships(user_id, role):
    """Whether the user may use the shipping pages (Scan, Ship, Parcels,
    Manifests). Read fresh on every call, like sections."""
    if role == "admin":
        return True
    row = db.query("SELECT can_ship FROM users WHERE id = %s AND is_active", (user_id,), one=True)
    return bool(row and row["can_ship"])


SECTION_PAGES = {"picklists": "/picklists.html", "verify": "/verify.html"}


def home_page(ships, sections):
    """Where the user lands after signing in: Scan for shippers, otherwise
    their first section with a page of its own."""
    if ships:
        return "/index.html"
    return next((SECTION_PAGES[s] for s in sections if s in SECTION_PAGES), "/settings.html")


def ship_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return api_error("Not authenticated", 401)
        if not user_ships(session["user_id"], session.get("role")):
            return api_error("You don't have access to shipping", 403)
        return f(*args, **kwargs)

    return wrapper


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
    ships = user_ships(user["id"], user["role"])
    return jsonify({"id": user["id"], "username": user["username"], "role": user["role"],
                    "home": home_page(ships, user_sections(user["id"], user["role"]))})


@bp.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@bp.get("/me")
@login_required
def me():
    ships = user_ships(session["user_id"], session["role"])
    sections = user_sections(session["user_id"], session["role"])
    return jsonify({
        "id": session["user_id"],
        "username": session["username"],
        "role": session["role"],
        "sections": sections,
        "ships": ships,
        "home": home_page(ships, sections),
    })

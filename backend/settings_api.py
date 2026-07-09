import requests
from flask import Blueprint, jsonify, request, session
from werkzeug.security import generate_password_hash

import config
import db
from auth import admin_required, login_required
from util import api_error, audit

bp = Blueprint("settings", __name__, url_prefix="/api")

MASK = "••••••••"

SETTING_KEYS = [
    "easyship_mode",
    "easyship_sandbox_token",
    "easyship_production_token",
    "default_item_category",
    "origin_company",
    "origin_contact",
    "origin_address1",
    "origin_address2",
    "origin_city",
    "origin_state",
    "origin_zip",
    "origin_phone",
    "origin_email",
    "placeholder_email",
    "print_mode",
    "printer_host",
    "printer_port",
]

SECRET_KEYS = {"easyship_sandbox_token", "easyship_production_token"}


@bp.get("/settings")
@admin_required
def get_settings():
    out = {}
    for key in SETTING_KEYS:
        value = db.get_setting(key)
        if key in SECRET_KEYS:
            out[key] = MASK if value else ""
        else:
            out[key] = value or ""
    if not out["easyship_mode"]:
        out["easyship_mode"] = "sandbox"
    if not out["print_mode"]:
        out["print_mode"] = "browser"
    return jsonify(out)


@bp.put("/settings")
@admin_required
def put_settings():
    data = request.get_json(silent=True) or {}
    for key, value in data.items():
        if key not in SETTING_KEYS:
            continue
        if key in SECRET_KEYS and value == MASK:
            continue
        db.set_setting(key, (value or "").strip())
    audit("settings.update", {"keys": [k for k in data if k in SETTING_KEYS]})
    return jsonify({"ok": True})


@bp.get("/settings/easyship-mode")
@login_required
def easyship_mode():
    return jsonify({"mode": db.get_setting("easyship_mode") or "sandbox"})


@bp.get("/settings/client")
@login_required
def client_settings():
    """Non-secret settings any logged-in user's UI needs."""
    return jsonify({
        "mode": db.get_setting("easyship_mode") or "sandbox",
        "placeholder_email": db.get_setting("placeholder_email") or "",
        "print_mode": db.get_setting("print_mode") or "browser",
    })


@bp.post("/settings/test/printer")
@admin_required
def test_printer():
    import printer
    data = request.get_json(silent=True) or {}
    try:
        printer.network_print(
            printer.TEST_ZPL,
            host=(data.get("host") or "").strip() or None,
            port=(data.get("port") or "").strip() or None,
        )
    except printer.PrinterError as e:
        return api_error(str(e))
    return jsonify({"ok": True})


FALLBACK_CATEGORIES = [
    "accessory_no_battery", "accessory_with_battery", "audio_video", "bags_luggages",
    "books_collectibles", "cameras", "computers_laptops", "documents",
    "dry_food_supplements", "fashion", "health_beauty", "home_appliances",
    "home_decor", "jewelry", "mobile_phones", "pet_accessory", "sport_leisure",
    "tablets", "toys", "watches",
]


@bp.get("/settings/easyship-categories")
@login_required
def easyship_categories():
    try:
        import easyship_client
        categories = easyship_client.list_item_categories()
        if categories:
            return jsonify(categories)
    except Exception:
        pass
    return jsonify([{"slug": s, "name": s.replace("_", " ").title()} for s in FALLBACK_CATEGORIES])


@bp.post("/settings/test/easyship")
@admin_required
def test_easyship():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode") or db.get_setting("easyship_mode", "sandbox")
    token_key = f"easyship_{mode}_token"
    token = data.get("token")
    if not token or token == MASK:
        token = db.get_setting(token_key)
    if not token:
        return api_error(f"No {mode} token configured")
    try:
        resp = requests.get(
            f"{config.EASYSHIP_BASE_URLS[mode]}/account",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except requests.RequestException as e:
        return api_error(f"Connection failed: {e}")
    if resp.status_code == 200:
        account = resp.json().get("account", {})
        return jsonify({"ok": True, "mode": mode, "account": account.get("name") or "connected"})
    return api_error(f"Easyship returned {resp.status_code}: {resp.text[:300]}")


# ---------- Shopify stores ----------

@bp.get("/shopify-stores")
@login_required
def list_stores():
    rows = db.query(
        "SELECT id, name, shop_domain, is_active, created_at FROM shopify_stores ORDER BY id"
    )
    return jsonify([
        {**r, "created_at": r["created_at"].isoformat()} for r in rows
    ])


@bp.post("/shopify-stores")
@admin_required
def create_store():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    domain = (data.get("shop_domain") or "").strip().lower()
    token = (data.get("access_token") or "").strip()
    if not name or not domain or not token:
        return api_error("Name, shop domain and access token are required")
    row = db.execute(
        "INSERT INTO shopify_stores (name, shop_domain, access_token) VALUES (%s, %s, %s) RETURNING id",
        (name, domain, token),
        returning=True,
    )
    audit("store.create", {"name": name, "domain": domain})
    return jsonify({"id": row["id"]})


@bp.put("/shopify-stores/<int:store_id>")
@admin_required
def update_store(store_id):
    data = request.get_json(silent=True) or {}
    store = db.query("SELECT * FROM shopify_stores WHERE id = %s", (store_id,), one=True)
    if not store:
        return api_error("Store not found", 404)
    name = (data.get("name") or store["name"]).strip()
    domain = (data.get("shop_domain") or store["shop_domain"]).strip().lower()
    token = (data.get("access_token") or "").strip()
    if not token or token == MASK:
        token = store["access_token"]
    is_active = bool(data.get("is_active", store["is_active"]))
    db.execute(
        "UPDATE shopify_stores SET name=%s, shop_domain=%s, access_token=%s, is_active=%s WHERE id=%s",
        (name, domain, token, is_active, store_id),
    )
    audit("store.update", {"id": store_id, "name": name})
    return jsonify({"ok": True})


@bp.delete("/shopify-stores/<int:store_id>")
@admin_required
def delete_store(store_id):
    used = db.query(
        "SELECT 1 FROM shipments WHERE shopify_store_id = %s LIMIT 1", (store_id,), one=True
    )
    if used:
        db.execute("UPDATE shopify_stores SET is_active = FALSE WHERE id = %s", (store_id,))
    else:
        db.execute("DELETE FROM shopify_stores WHERE id = %s", (store_id,))
    audit("store.delete", {"id": store_id})
    return jsonify({"ok": True})


@bp.post("/shopify-stores/<int:store_id>/test")
@admin_required
def test_store(store_id):
    store = db.query("SELECT * FROM shopify_stores WHERE id = %s", (store_id,), one=True)
    if not store:
        return api_error("Store not found", 404)
    url = f"https://{store['shop_domain']}/admin/api/{config.SHOPIFY_API_VERSION}/graphql.json"
    try:
        resp = requests.post(
            url,
            headers={"X-Shopify-Access-Token": store["access_token"]},
            json={"query": "{ shop { name } }"},
            timeout=15,
        )
    except requests.RequestException as e:
        return api_error(f"Connection failed: {e}")
    if resp.status_code == 200 and "errors" not in resp.json():
        return jsonify({"ok": True, "shop": resp.json()["data"]["shop"]["name"]})
    return api_error(f"Shopify returned {resp.status_code}: {resp.text[:300]}")


# ---------- Users ----------

@bp.get("/users")
@admin_required
def list_users():
    rows = db.query(
        "SELECT id, username, role, is_active, created_at FROM users ORDER BY id"
    )
    return jsonify([{**r, "created_at": r["created_at"].isoformat()} for r in rows])


@bp.post("/users")
@admin_required
def create_user():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role") if data.get("role") in ("admin", "user") else "user"
    if not username or len(password) < 4:
        return api_error("Username and a password of at least 4 characters are required")
    existing = db.query("SELECT id FROM users WHERE username = %s", (username,), one=True)
    if existing:
        return api_error("Username already exists")
    row = db.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
        (username, generate_password_hash(password), role),
        returning=True,
    )
    audit("user.create", {"username": username, "role": role})
    return jsonify({"id": row["id"]})


@bp.delete("/users/<int:user_id>")
@admin_required
def delete_user(user_id):
    if user_id == session["user_id"]:
        return api_error("You cannot deactivate your own account")
    db.execute("UPDATE users SET is_active = FALSE WHERE id = %s", (user_id,))
    audit("user.deactivate", {"id": user_id})
    return jsonify({"ok": True})


@bp.post("/users/<int:user_id>/activate")
@admin_required
def activate_user(user_id):
    db.execute("UPDATE users SET is_active = TRUE WHERE id = %s", (user_id,))
    audit("user.activate", {"id": user_id})
    return jsonify({"ok": True})


@bp.put("/users/<int:user_id>/password")
@login_required
def change_password(user_id):
    if session.get("role") != "admin" and user_id != session["user_id"]:
        return api_error("You can only change your own password", 403)
    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    if len(password) < 4:
        return api_error("Password must be at least 4 characters")
    db.execute(
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (generate_password_hash(password), user_id),
    )
    audit("user.password_change", {"id": user_id})
    return jsonify({"ok": True})

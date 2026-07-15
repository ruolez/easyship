import requests
from flask import Blueprint, jsonify, request, session
from werkzeug.security import generate_password_hash

import config
import db
import providers
from auth import admin_required, login_required
from providers.base import ProviderError
from util import api_error, audit

bp = Blueprint("settings", __name__, url_prefix="/api")

MASK = "••••••••"

# Non-provider settings. Provider-specific keys (tokens, mode, enabled flag,
# custom fields) come from each provider's descriptor, so a new platform needs
# no edits here.
BASE_SETTING_KEYS = [
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
    "label_timeout_seconds",
    "countdown_seconds",
    "shipper_host",
    "shipper_port",
    "shipper_db",
    "shipper_user",
    "shipper_password",
]

BASE_SECRET_KEYS = {"shipper_password"}


def _provider_setting_keys():
    """(all persistable keys, secret keys) contributed by registered providers."""
    keys, secrets = [], set()
    for d in providers.descriptors():
        keys.append(d["enabled_key"])
        if d.get("modes"):
            keys.append(d["mode_key"])
        for f in d["fields"]:
            keys.append(f["key"])
            if f.get("type") == "secret":
                secrets.add(f["key"])
    return keys, secrets


def _setting_keys():
    pkeys, _ = _provider_setting_keys()
    return BASE_SETTING_KEYS + pkeys


def _secret_keys():
    _, psecrets = _provider_setting_keys()
    return BASE_SECRET_KEYS | psecrets


def _aggregate_mode():
    """Nav-badge environment: sandbox if any enabled provider is in a test mode."""
    for p in providers.enabled_providers():
        if p.is_test_mode():
            return "sandbox"
    return "production"


def _provider_or_404(name):
    return providers.get_provider(name) if name in providers.registered_names() else None


@bp.get("/settings")
@admin_required
def get_settings():
    secret_keys = _secret_keys()
    out = {}
    for key in _setting_keys():
        value = db.get_setting(key)
        if key in secret_keys:
            out[key] = MASK if value else ""
        else:
            out[key] = value or ""
    for d in providers.descriptors():
        if d.get("modes") and not out.get(d["mode_key"]):
            out[d["mode_key"]] = d["modes"][0]["value"]
    if not out.get("print_mode"):
        out["print_mode"] = "browser"
    return jsonify(out)


@bp.put("/settings")
@admin_required
def put_settings():
    data = request.get_json(silent=True) or {}
    keys = set(_setting_keys())
    secret_keys = _secret_keys()
    for key, value in data.items():
        if key not in keys:
            continue
        if key in secret_keys and value == MASK:
            continue
        db.set_setting(key, (value or "").strip())
    audit("settings.update", {"keys": [k for k in data if k in keys]})
    return jsonify({"ok": True})


@bp.get("/settings/easyship-mode")
@login_required
def easyship_mode():
    # Kept for the nav badge; now reports the aggregate environment.
    return jsonify({"mode": _aggregate_mode()})


@bp.get("/settings/client")
@login_required
def client_settings():
    """Non-secret settings any logged-in user's UI needs."""
    return jsonify({
        "mode": _aggregate_mode(),
        "placeholder_email": db.get_setting("placeholder_email") or "",
        "print_mode": db.get_setting("print_mode") or "browser",
        "countdown_seconds": int(db.get_setting("countdown_seconds") or 5),
    })


@bp.get("/providers")
@admin_required
def list_providers():
    """Provider descriptors that drive the Settings shipping section."""
    return jsonify(providers.descriptors())


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


@bp.post("/settings/test/shipper")
@admin_required
def test_shipper():
    import pymssql
    data = request.get_json(silent=True) or {}

    def val(field, key):
        v = (data.get(field) or "").strip()
        return v if v and v != MASK else (db.get_setting(key) or "").strip()

    host = val("host", "shipper_host")
    port = val("port", "shipper_port")
    db_name = val("db", "shipper_db")
    user = val("user", "shipper_user")
    password = val("password", "shipper_password")
    if not (host and db_name and user):
        return api_error("Host, database and username are required")
    try:
        conn = pymssql.connect(
            server=host, port=int(port or 1433), database=db_name,
            user=user, password=password, timeout=10, login_timeout=10,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT TOP 1 id FROM parcels ORDER BY id DESC")
            cur.fetchone()
        conn.close()
    except Exception as e:
        return api_error(f"Connection failed: {e}")
    return jsonify({"ok": True})


FALLBACK_CATEGORIES = [
    "accessory_no_battery", "accessory_with_battery", "audio_video", "bags_luggages",
    "books_collectibles", "cameras", "computers_laptops", "documents",
    "dry_food_supplements", "fashion", "health_beauty", "home_appliances",
    "home_decor", "jewelry", "mobile_phones", "pet_accessory", "sport_leisure",
    "tablets", "toys", "watches",
]


def _item_categories(provider):
    """Live categories with a static fallback so the customs dropdown always fills."""
    if provider:
        try:
            categories = provider.list_item_categories()
            if categories:
                return categories
        except Exception:
            pass
    return [{"slug": s, "name": s.replace("_", " ").title()} for s in FALLBACK_CATEGORIES]


@bp.get("/providers/<name>/item-categories")
@login_required
def provider_item_categories(name):
    return jsonify(_item_categories(_provider_or_404(name)))


@bp.get("/providers/<name>/services")
@admin_required
def provider_services(name):
    provider = _provider_or_404(name)
    if not provider:
        return api_error("Unknown provider", 404)
    excluded = sorted(provider.get_excluded_service_ids())
    try:
        services = provider.list_courier_services()
    except Exception as e:
        return api_error(f"Could not fetch services from {provider.label}: {e}")
    return jsonify({"services": services, "excluded": excluded})


@bp.post("/providers/<name>/excluded-services")
@admin_required
def provider_excluded_services(name):
    provider = _provider_or_404(name)
    if not provider:
        return api_error("Unknown provider", 404)
    data = request.get_json(silent=True) or {}
    ids = data.get("excluded")
    if not isinstance(ids, list):
        return api_error("excluded must be a list of service id values")
    clean = provider.set_excluded_service_ids(ids)
    audit("settings.excluded_services", {"provider": name, "count": len(clean)})
    return jsonify({"ok": True, "excluded": clean})


@bp.post("/providers/<name>/test")
@admin_required
def provider_test(name):
    provider = _provider_or_404(name)
    if not provider:
        return api_error("Unknown provider", 404)
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(provider.test_connection(mode=data.get("mode"), token=data.get("token")))
    except ProviderError as e:
        return api_error(str(e))


# ---------- Back-compat aliases (Easyship) ----------

@bp.get("/settings/easyship-categories")
@login_required
def easyship_categories():
    return jsonify(_item_categories(_provider_or_404("easyship")))


@bp.get("/settings/courier-services")
@admin_required
def courier_services():
    return provider_services("easyship")


@bp.post("/settings/excluded-services")
@admin_required
def save_excluded_services():
    return provider_excluded_services("easyship")


@bp.post("/settings/test/easyship")
@admin_required
def test_easyship():
    return provider_test("easyship")


# ---------- Box sizes ----------

@bp.get("/boxes")
@login_required
def list_boxes():
    rows = db.query(
        "SELECT id, name, length, width, height, is_active FROM boxes WHERE is_active ORDER BY name"
    )
    return jsonify([
        {**r, "length": float(r["length"]), "width": float(r["width"]), "height": float(r["height"])}
        for r in rows
    ])


@bp.post("/boxes")
@admin_required
def create_box():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    try:
        dims = [float(data.get(k)) for k in ("length", "width", "height")]
        if not name or any(d <= 0 for d in dims):
            raise ValueError
    except (TypeError, ValueError):
        return api_error("Name and positive length/width/height (inches) are required")
    row = db.execute(
        "INSERT INTO boxes (name, length, width, height) VALUES (%s, %s, %s, %s) RETURNING id",
        (name, *dims),
        returning=True,
    )
    audit("box.create", {"name": name})
    return jsonify({"id": row["id"]})


@bp.delete("/boxes/<int:box_id>")
@admin_required
def delete_box(box_id):
    db.execute("DELETE FROM boxes WHERE id = %s", (box_id,))
    audit("box.delete", {"id": box_id})
    return jsonify({"ok": True})


# ---------- Shopify stores ----------

@bp.get("/shopify-stores")
@login_required
def list_stores():
    rows = db.query(
        "SELECT id, name, shop_domain, prefix, is_active, created_at FROM shopify_stores ORDER BY id"
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
    prefix = (data.get("prefix") or "").strip()
    row = db.execute(
        "INSERT INTO shopify_stores (name, shop_domain, access_token, prefix) VALUES (%s, %s, %s, %s) RETURNING id",
        (name, domain, token, prefix),
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
    prefix = data.get("prefix")
    prefix = prefix.strip() if prefix is not None else store["prefix"]
    db.execute(
        "UPDATE shopify_stores SET name=%s, shop_domain=%s, access_token=%s, prefix=%s, is_active=%s WHERE id=%s",
        (name, domain, token, prefix, is_active, store_id),
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

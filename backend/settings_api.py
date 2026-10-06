import json

import requests
from flask import Blueprint, jsonify, request, session
from werkzeug.security import generate_password_hash

import config
import db
import profit
import providers
import tag_rules
from auth import admin_required, login_required
from providers.base import ProviderError
from util import api_error, audit, central_time

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
    "printer_dpi",
    "label_timeout_seconds",
    "countdown_seconds",
    "order_tag_rules",
    profit.SETTING_MIN_AMOUNT,
    profit.SETTING_MIN_PCT,
    profit.SETTING_PASSWORD,
    "shipper_host",
    "shipper_port",
    "shipper_db",
    "shipper_user",
    "shipper_password",
]

BASE_SECRET_KEYS = {"shipper_password", profit.SETTING_PASSWORD}


def _provider_setting_keys():
    """(all persistable keys, secret keys) contributed by the configured
    provider instances, including each instance's ship-from override."""
    keys, secrets = [], set()
    for d in providers.descriptors():
        keys.append(d["enabled_key"])
        if d.get("modes"):
            keys.append(d["mode_key"])
        for f in d["fields"]:
            keys.append(f["key"])
            if f.get("type") == "secret":
                secrets.add(f["key"])
        if d.get("origin_override_key"):
            keys.append(d["origin_override_key"])
            keys.extend(f["key"] for f in d.get("origin_fields") or [])
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
    return providers.get_provider(name)


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


def _profit_settings_error(data):
    """Thresholds must be numbers, and a hard block needs a bypass password on
    file or in this save — otherwise no flagged label could ever be bought."""
    limits = {profit.SETTING_MIN_AMOUNT: ("Minimum profit", None),
              profit.SETTING_MIN_PCT: ("Minimum margin", 100)}
    enabled = False
    for key, (label, maximum) in limits.items():
        raw = str((data.get(key) if key in data else db.get_setting(key)) or "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            return f"{label} must be a number"
        if value < 0:
            return f"{label} cannot be negative"
        if maximum is not None and value > maximum:
            return f"{label} cannot exceed {maximum}"
        enabled = True
    if profit.SETTING_PASSWORD in data:
        supplied = str(data[profit.SETTING_PASSWORD] or "").strip()
        has_password = bool(supplied) and (supplied != MASK or bool(db.get_setting(profit.SETTING_PASSWORD)))
    else:
        has_password = bool(db.get_setting(profit.SETTING_PASSWORD))
    if enabled and not has_password:
        return "Set a bypass password before turning on the profit check"
    return None


@bp.put("/settings")
@admin_required
def put_settings():
    data = request.get_json(silent=True) or {}
    keys = set(_setting_keys())
    secret_keys = _secret_keys()
    error = _profit_settings_error(data)
    if error:
        return api_error(error)
    for key, value in data.items():
        if key not in keys:
            continue
        if key in secret_keys and value == MASK:
            continue
        if key == tag_rules.RULES_KEY:
            value = json.dumps(tag_rules.parse_rules(value)) if (value or "").strip() else ""
        if key == profit.SETTING_PASSWORD and (value or "").strip():
            # Only ever compared, never replayed — so it is stored hashed.
            value = generate_password_hash(value.strip())
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
        "profit_gate_enabled": profit.load_thresholds()["enabled"],
    })


AUDIT_PAGE_DEFAULT = 100
AUDIT_PAGE_MAX = 500


@bp.get("/audit")
@admin_required
def list_audit():
    """The audit log, newest first. `group` keeps one family of actions
    (the part before the dot: profit, label, settings…), `action` one exact
    action."""
    try:
        limit = min(max(int(request.args.get("limit") or AUDIT_PAGE_DEFAULT), 1), AUDIT_PAGE_MAX)
        offset = max(int(request.args.get("offset") or 0), 0)
    except ValueError:
        return api_error("limit and offset must be whole numbers")
    clauses, params = [], []
    group = (request.args.get("group") or "").strip()
    action = (request.args.get("action") or "").strip()
    if group:
        clauses.append("split_part(a.action, '.', 1) = %s")
        params.append(group)
    if action:
        clauses.append("a.action = %s")
        params.append(action)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = db.query(f"SELECT COUNT(*) AS total FROM audit_log a{where}", params, one=True)["total"]
    rows = db.query(
        f"""SELECT a.id, a.action, a.detail, a.created_at, u.username
            FROM audit_log a LEFT JOIN users u ON u.id = a.user_id{where}
            ORDER BY a.created_at DESC, a.id DESC LIMIT %s OFFSET %s""",
        params + [limit, offset],
    ) if total else []
    groups = db.query("SELECT DISTINCT split_part(action, '.', 1) AS value FROM audit_log ORDER BY value")
    return jsonify({
        "rows": [{"id": r["id"], "action": r["action"], "detail": r["detail"],
                  "created_at": central_time(r["created_at"]), "username": r["username"]} for r in rows],
        "total": total, "limit": limit, "offset": offset,
        "groups": [g["value"] for g in groups or []],
    })


@bp.get("/providers")
@admin_required
def list_providers():
    """One descriptor per configured provider instance — drives the Settings
    shipping section and the per-user integration pickers."""
    return jsonify(providers.descriptors())


# ---------- Provider instances (named accounts of a platform) ----------

@bp.get("/provider-instances")
@admin_required
def list_provider_instances():
    return jsonify({"platforms": providers.platforms(), "instances": providers.instance_summaries()})


@bp.post("/provider-instances")
@admin_required
def create_provider_instance():
    data = request.get_json(silent=True) or {}
    try:
        row = providers.create_instance((data.get("platform") or "").strip(), data.get("label"))
    except ValueError as e:
        return api_error(str(e))
    audit("provider_instance.create", row)
    return jsonify(row), 201


@bp.put("/provider-instances/<int:instance_id>")
@admin_required
def rename_provider_instance(instance_id):
    data = request.get_json(silent=True) or {}
    try:
        row = providers.rename_instance(instance_id, data.get("label"))
    except LookupError as e:
        return api_error(str(e), 404)
    except ValueError as e:
        return api_error(str(e))
    audit("provider_instance.rename", row)
    return jsonify(row)


@bp.delete("/provider-instances/<int:instance_id>")
@admin_required
def delete_provider_instance(instance_id):
    try:
        row = providers.delete_instance(instance_id)
    except LookupError as e:
        return api_error(str(e), 404)
    except (providers.InstanceEnabled, providers.InstanceInUse) as e:
        return api_error(str(e), 409)
    audit("provider_instance.delete", row)
    return jsonify({"ok": True})


@bp.put("/provider-instances/<int:instance_id>/users")
@admin_required
def set_provider_instance_users(instance_id):
    """Assign users to one account from the integration's own card."""
    data = request.get_json(silent=True) or {}
    user_ids = data.get("user_ids")
    if not isinstance(user_ids, list):
        return api_error("user_ids must be a list of user ids")
    try:
        row = providers.set_instance_users(instance_id, user_ids)
    except LookupError as e:
        return api_error(str(e), 404)
    except (TypeError, ValueError) as e:
        return api_error(str(e))
    audit("provider_instance.users", {"id": row["id"], "key": row["key"], "user_ids": row["user_ids"]})
    return jsonify(row)


@bp.get("/providers/enabled")
@login_required
def enabled_providers():
    """The shipping platforms this user may ship with — drives the nav
    'Shipping with' selector and its environment badge."""
    return jsonify([
        {"name": p.name, "label": p.label, "test": p.is_test_mode()}
        for p in providers.enabled_for_user(session["user_id"], session.get("role"))
    ])


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


@bp.get("/providers/<name>/carriers")
@admin_required
def provider_carriers(name):
    """Options for a carrier-picker field. Always a 200 list headed by an empty
    placeholder, so an unsaved choice never displays as the first real carrier
    and a missing/bad key reads as a message rather than a blank select."""
    provider = _provider_or_404(name)
    if not provider or not hasattr(provider, "list_carriers"):
        return api_error("Unknown provider", 404)
    try:
        carriers = provider.list_carriers()
    except ProviderError as e:
        return jsonify([{"value": "", "label": f"— {e}"}])
    return jsonify([{"value": "", "label": "— choose a carrier —"}, *carriers])


@bp.get("/providers/<name>/services/available")
@login_required
def provider_available_services(name):
    """Services a packer can pick from (Auto Mode preset): the catalog minus
    the admin's exclusions. Providers without a catalog return an empty list."""
    provider = _provider_or_404(name)
    if not provider:
        return api_error("Unknown provider", 404)
    if name not in [p.name for p in providers.enabled_for_user(session["user_id"], session.get("role"))]:
        return api_error("This shipping integration is not enabled for your account", 403)
    try:
        services = provider.list_courier_services()
    except Exception as e:
        return api_error(f"Could not fetch services from {provider.label}: {e}")
    excluded = provider.get_excluded_service_ids()
    available = [s for s in services if str(s.get("id")) not in excluded]
    return jsonify({"services": available, "has_catalog": bool(services)})


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
        """SELECT id, name, length, width, height, is_active FROM boxes
           WHERE is_active ORDER BY length * width * height, length, width, height"""
    )
    return jsonify([
        {**r, "length": float(r["length"]), "width": float(r["width"]), "height": float(r["height"])}
        for r in rows
    ])


@bp.post("/boxes")
@admin_required
def create_box():
    data = request.get_json(silent=True) or {}
    try:
        dims = [float(data.get(k)) for k in ("length", "width", "height")]
        if any(d < 0 for d in dims):
            raise ValueError
    except (TypeError, ValueError):
        return api_error("Non-negative length/width/height (inches) are required")
    # A box is identified by its dimensions — the name is derived, not chosen.
    name = "×".join(f"{d:g}" for d in dims)
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
        """SELECT id, name, shop_domain, prefix, no_company, is_active, created_at
           FROM shopify_stores ORDER BY id"""
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
        """INSERT INTO shopify_stores (name, shop_domain, access_token, prefix, no_company)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (name, domain, token, prefix, bool(data.get("no_company"))),
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
        """UPDATE shopify_stores SET name=%s, shop_domain=%s, access_token=%s, prefix=%s,
           no_company=%s, is_active=%s WHERE id=%s""",
        (name, domain, token, prefix, bool(data.get("no_company", store["no_company"])), is_active, store_id),
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
        "SELECT id, username, role, is_active, allowed_providers, created_at FROM users ORDER BY id"
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
    allowed = providers.sanitize_allowed(data.get("allowed_providers")) if role == "user" else None
    row = db.execute(
        "INSERT INTO users (username, password_hash, role, allowed_providers) VALUES (%s, %s, %s, %s) RETURNING id",
        (username, generate_password_hash(password), role, json.dumps(allowed) if allowed else None),
        returning=True,
    )
    audit("user.create", {"username": username, "role": role, "allowed_providers": allowed})
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


@bp.put("/users/<int:user_id>/providers")
@admin_required
def set_user_providers(user_id):
    """Assign which shipping accounts a user may ship with. Every account
    selected = unrestricted; admins are never restricted. An empty selection
    is refused — there is no 'no accounts' state; deactivate the user instead."""
    row = db.query("SELECT id, role, username FROM users WHERE id = %s", (user_id,), one=True)
    if not row:
        return api_error("User not found", 404)
    if row["role"] == "admin":
        return api_error("Admins always have access to every integration")
    data = request.get_json(silent=True) or {}
    requested = data.get("allowed_providers") or []
    if not any(str(n) in providers.instance_keys() for n in requested):
        return api_error(f"{row['username']} {providers.NO_ACCOUNT_LEFT}")
    allowed = providers.sanitize_allowed(requested)
    db.execute(
        "UPDATE users SET allowed_providers = %s WHERE id = %s",
        (json.dumps(allowed) if allowed else None, user_id),
    )
    audit("user.providers", {"id": user_id, "allowed_providers": allowed})
    return jsonify({"ok": True, "allowed_providers": allowed})


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

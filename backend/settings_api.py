import hmac
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

from flask import Blueprint, jsonify, redirect, request, session
from werkzeug.security import generate_password_hash

import db
import dbadmin
import picklist_rules
import profit
import providers
import shopify_auth
import shopify_client
import tag_rules
from auth import admin_required, login_required, sanitize_sections, ship_required
from providers.base import ProviderError
from shopify_errors import ShopifyError
from util import CENTRAL, api_error, audit, central_time

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
    "dbadmin_host",
    "dbadmin_port",
    "dbadmin_db",
    "dbadmin_user",
    "dbadmin_password",
    "verify_skip_prefixes_shopify",
    "verify_skip_prefixes_backoffice",
    "picklist_backoffice_db_id",
    "picklist_quote_status",
    "picklist_title_prefix",
    "picklist_number_tag",
    "picklist_hidden_tags",
    picklist_rules.RULES_KEY,
    picklist_rules.PRESETS_KEY,
]

BASE_SECRET_KEYS = {"shipper_password", "dbadmin_password", profit.SETTING_PASSWORD}


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
        elif key == picklist_rules.RULES_KEY and value:
            out[key] = json.dumps(picklist_rules.parse_rules(value))
        elif key == picklist_rules.PRESETS_KEY and value:
            out[key] = json.dumps(picklist_rules.load_presets(picklist_rules.load_rules()))
        else:
            out[key] = value or ""
    for d in providers.descriptors():
        if d.get("modes") and not out.get(d["mode_key"]):
            out[d["mode_key"]] = d["modes"][0]["value"]
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


def _picklist_settings_error(data):
    status = str(data.get("picklist_quote_status") or "").strip()
    if status and not status.isdigit():
        return "Pick list quotation status must be a whole number"
    tag = str(data.get("picklist_number_tag") or "").strip()
    if tag and not (tag.isdigit() and len(tag) <= 4):
        return "Pick list number tag must be 1–4 digits"
    if len(str(data.get("picklist_title_prefix") or "").strip()) > 20:
        return "Pick list title prefix must be 20 characters or fewer"
    return None


@bp.put("/settings")
@admin_required
def put_settings():
    data = request.get_json(silent=True) or {}
    keys = set(_setting_keys())
    secret_keys = _secret_keys()
    error = _profit_settings_error(data) or _picklist_settings_error(data)
    if error:
        return api_error(error)
    changed = []
    for key, value in data.items():
        if key not in keys:
            continue
        if key in secret_keys and value == MASK:
            continue
        if key == tag_rules.RULES_KEY:
            value = json.dumps(tag_rules.parse_rules(value)) if (value or "").strip() else ""
        if key == picklist_rules.RULES_KEY:
            value = json.dumps(picklist_rules.parse_rules(value)) if (value or "").strip() else ""
        if key == picklist_rules.PRESETS_KEY:
            # Read against the rules this same save leaves in place.
            if picklist_rules.RULES_KEY in data:
                rules = picklist_rules.parse_rules(data[picklist_rules.RULES_KEY])
            else:
                rules = picklist_rules.load_rules()
            presets = picklist_rules.parse_presets(value, [r["id"] for r in rules])
            value = json.dumps(presets) if (value or "").strip() else ""
        if key == profit.SETTING_PASSWORD and (value or "").strip():
            # Only ever compared, never replayed — so it is stored hashed.
            value = generate_password_hash(value.strip())
        value = (value or "").strip()
        if value != (db.get_setting(key) or ""):
            changed.append(key)
        db.set_setting(key, value)
    if any(key.startswith("dbadmin_") for key in changed):
        dbadmin.forget_sources()
    audit("settings.update", {"keys": changed})
    return jsonify({"ok": True})


@bp.get("/settings/easyship-mode")
@ship_required
def easyship_mode():
    # Kept for the nav badge; now reports the aggregate environment.
    return jsonify({"mode": _aggregate_mode()})


@bp.get("/settings/client")
@ship_required
def client_settings():
    """Non-secret settings any logged-in user's UI needs. The print_* /
    printer_* values are the shared printing setup from before it moved to each
    station: a station that never saved its own prints this way."""
    return jsonify({
        "mode": _aggregate_mode(),
        "placeholder_email": db.get_setting("placeholder_email") or "",
        "print_mode": db.get_setting("print_mode") or "browser",
        "printer_host": db.get_setting("printer_host") or "",
        "printer_port": db.get_setting("printer_port") or "",
        "printer_dpi": db.get_setting("printer_dpi") or "",
        "countdown_seconds": int(db.get_setting("countdown_seconds") or 5),
        "profit_gate_enabled": profit.load_thresholds()["enabled"],
    })


AUDIT_PAGE_DEFAULT = 100
AUDIT_PAGE_MAX = 500


def _like(text):
    """A substring pattern for ILIKE with the user's own % and _ taken literally."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _audit_shipments(rows):
    """Order, carrier and tracking of every shipment the page's rows point at,
    so a "label.buy" reads as an order rather than a list of ids."""
    ids = set()
    for r in rows:
        detail = r["detail"] or {}
        ids.update(i for i in detail.get("shipment_ids") or [] if isinstance(i, int))
        if isinstance(detail.get("shipment_id"), int):
            ids.add(detail["shipment_id"])
    if not ids:
        return {}
    found = db.query(
        """SELECT id, shopify_order_name, backoffice_invoice_number, courier_name, tracking_number,
                  shipping_cost, status FROM shipments WHERE id = ANY(%s)""",
        (sorted(ids),),
    ) or []
    return {str(s["id"]): {
        "order": s["shopify_order_name"] or s["backoffice_invoice_number"],
        "courier": s["courier_name"],
        "tracking": s["tracking_number"],
        "cost": float(s["shipping_cost"]) if s["shipping_cost"] is not None else None,
        "status": s["status"],
    } for s in found}


@bp.get("/audit")
@admin_required
def list_audit():
    """The audit log, newest first. `group` keeps one family of actions
    (the part before the dot: profit, label, settings…), `action` one exact
    action, `user` one username, `q` rows whose action or detail contains
    the text, `days` the last so many days."""
    try:
        limit = min(max(int(request.args.get("limit") or AUDIT_PAGE_DEFAULT), 1), AUDIT_PAGE_MAX)
        offset = max(int(request.args.get("offset") or 0), 0)
    except ValueError:
        return api_error("limit and offset must be whole numbers")
    try:
        days = int(request.args.get("days") or 0)
    except ValueError:
        return api_error("days must be a whole number")
    clauses, params = [], []
    group = (request.args.get("group") or "").strip()
    action = (request.args.get("action") or "").strip()
    user = (request.args.get("user") or "").strip()
    q = (request.args.get("q") or "").strip()
    if group:
        clauses.append("split_part(a.action, '.', 1) = %s")
        params.append(group)
    if action:
        clauses.append("a.action = %s")
        params.append(action)
    if user:
        clauses.append("u.username = %s")
        params.append(user)
    if q:
        clauses.append("(a.action ILIKE %s OR a.detail::text ILIKE %s)")
        params += [_like(q), _like(q)]
    if days > 0:
        clauses.append("a.created_at >= now() - make_interval(days => %s)")
        params.append(days)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    joined = "FROM audit_log a LEFT JOIN users u ON u.id = a.user_id"
    total = db.query(f"SELECT COUNT(*) AS total {joined}{where}", params, one=True)["total"]
    rows = db.query(
        f"""SELECT a.id, a.action, a.detail, a.created_at, u.username
            {joined}{where}
            ORDER BY a.created_at DESC, a.id DESC LIMIT %s OFFSET %s""",
        params + [limit, offset],
    ) if total else []
    rows = rows or []
    groups = db.query("SELECT DISTINCT split_part(action, '.', 1) AS value FROM audit_log ORDER BY value")
    users = db.query(
        """SELECT DISTINCT u.username FROM audit_log a JOIN users u ON u.id = a.user_id
           ORDER BY u.username""")
    return jsonify({
        "rows": [{"id": r["id"], "action": r["action"], "detail": r["detail"],
                  "created_at": central_time(r["created_at"]),
                  "date": r["created_at"].astimezone(CENTRAL).strftime("%Y-%m-%d"),
                  "username": r["username"]} for r in rows],
        "total": total, "limit": limit, "offset": offset,
        "groups": [g["value"] for g in groups or []],
        "users": [u["username"] for u in users or []],
        "shipments": _audit_shipments(rows),
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
        {"name": p.name, "label": p.label, "test": p.is_test_mode(), "cod": p.supports_cod,
         "platform": p.platform, "auto": p.auto_mode}
        for p in providers.enabled_for_user(session["user_id"], session.get("role"))
    ])


@bp.post("/settings/test/printer")
@ship_required
def test_printer():
    """A test label to the network printer the station names — any packer can
    check their own station's printer."""
    import printer
    data = request.get_json(silent=True) or {}
    cfg = printer.station_config({"host": data.get("host"), "port": data.get("port")})
    try:
        printer.network_print(printer.TEST_ZPL, cfg["host"], cfg["port"])
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


@bp.post("/settings/test/dbadmin")
@admin_required
def test_dbadmin():
    data = request.get_json(silent=True) or {}

    def val(field):
        v = (data.get(field) or "").strip()
        return v if v and v != MASK else (db.get_setting(dbadmin.SETTING_KEYS[field]) or "").strip()

    cfg = {field: val(field) for field in dbadmin.SETTING_KEYS}
    if not (cfg["host"] and cfg["db"] and cfg["user"]):
        return api_error("Host, database and username are required")
    try:
        conn = dbadmin.connect_with(cfg, timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM AdminDBs_admin")
                databases = cur.fetchone()[0]
                cur.execute("SELECT TOP 1 id FROM QuotationsStatus ORDER BY id DESC")
                cur.fetchone()
        finally:
            conn.close()
    except Exception as e:
        return api_error(f"Connection failed: {e}")
    return jsonify({"ok": True, "databases": databases})


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
@ship_required
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
@ship_required
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
    # Extra test params only for providers whose fields declare them, so the
    # others keep their (mode, token) signature.
    extra = {"username": data["username"]} if data.get("username") else {}
    try:
        return jsonify(provider.test_connection(mode=data.get("mode"), token=data.get("token"), **extra))
    except ProviderError as e:
        return api_error(str(e))


# ---------- OAuth sign-in (providers with authorize_url, e.g. Endicia direct) ----------
# The provider's sign-in page redirects the admin's browser back to a callback
# URL registered with the provider in advance; the pending sign-in (state,
# instance, mode, redirect URI) waits in the admin's session meanwhile.

OAUTH_SESSION_KEY = "oauth_pending"
OAUTH_STATE_TTL_S = 15 * 60
PAGE_ORIGIN_RE = re.compile(r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?")


def _oauth_provider(name):
    provider = _provider_or_404(name)
    return provider if provider is not None and hasattr(provider, "authorize_url") else None


@bp.post("/providers/<name>/oauth/start")
@admin_required
def provider_oauth_start(name):
    provider = _oauth_provider(name)
    if not provider:
        return api_error("Unknown provider", 404)
    data = request.get_json(silent=True) or {}
    origin = str(data.get("origin") or "").rstrip("/")
    if not PAGE_ORIGIN_RE.fullmatch(origin):
        return api_error("Open Settings from the app's own address and try again")
    mode = data.get("mode") if data.get("mode") in provider.modes else provider.active_mode()
    redirect_uri = origin + provider.oauth_callback_path
    state = secrets.token_urlsafe(24)
    try:
        url = provider.authorize_url(mode, redirect_uri, state)
    except ProviderError as e:
        return api_error(str(e))
    session[OAUTH_SESSION_KEY] = {"state": state, "provider": name, "mode": mode,
                                  "redirect_uri": redirect_uri, "at": time.time()}
    return jsonify({"url": url, "redirect_uri": redirect_uri})


def _finish_oauth(params, name=None):
    """Complete the pending sign-in from the callback's query parameters.
    Returns (provider, mode, account); raises ValueError / ProviderError."""
    pending = session.pop(OAUTH_SESSION_KEY, None)
    state = params.get("state") or ""
    if (not pending or not state or not hmac.compare_digest(pending["state"], state)
            or time.time() - pending["at"] > OAUTH_STATE_TTL_S):
        raise ValueError("This sign-in is stale or was started elsewhere — click Connect again")
    if name and pending["provider"] != name:
        raise ValueError("That sign-in belongs to a different shipping account")
    if params.get("error"):
        raise ValueError(f"Sign-in was not completed: {params.get('error_description') or params['error']}")
    if not params.get("code"):
        raise ValueError("The sign-in page returned no authorization code")
    provider = _oauth_provider(pending["provider"])
    if not provider:
        raise ValueError("That shipping account no longer exists")
    account = provider.complete_sign_in(pending["mode"], params["code"], pending["redirect_uri"])
    audit("settings.oauth_connected", {"provider": provider.name, "mode": pending["mode"], "account": account})
    return provider, pending["mode"], account


@bp.get("/oauth/<platform>/callback")
def provider_oauth_callback(platform):
    if session.get("role") != "admin":
        return redirect("/login.html")
    params = {k: request.args.get(k) for k in ("code", "state", "error", "error_description")}
    try:
        provider, _, account = _finish_oauth(params)
        query = {"oauth": "connected", "provider": provider.label, "account": account}
    except (ValueError, ProviderError) as e:
        query = {"oauth_error": str(e)}
    return redirect(f"/settings.html?{urlencode(query)}#providers")


@bp.post("/providers/<name>/oauth/complete")
@admin_required
def provider_oauth_complete(name):
    """Fallback when the callback URL is not reachable from the browser: the
    admin pastes the address the sign-in page landed on."""
    query = parse_qs(urlparse(str((request.get_json(silent=True) or {}).get("url") or "").strip()).query)
    params = {k: (query.get(k) or [None])[0] for k in ("code", "state", "error", "error_description")}
    try:
        _, mode, account = _finish_oauth(params, name)
    except (ValueError, ProviderError) as e:
        return api_error(str(e))
    return jsonify({"ok": True, "mode": mode, "account": account})


@bp.post("/providers/<name>/oauth/disconnect")
@admin_required
def provider_oauth_disconnect(name):
    provider = _oauth_provider(name)
    if not provider:
        return api_error("Unknown provider", 404)
    mode = (request.get_json(silent=True) or {}).get("mode")
    mode = mode if mode in provider.modes else provider.active_mode()
    provider.disconnect(mode)
    audit("settings.oauth_disconnected", {"provider": name, "mode": mode})
    return jsonify({"ok": True})


# ---------- Back-compat aliases (Easyship) ----------

@bp.get("/settings/easyship-categories")
@ship_required
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
@ship_required
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
# A store connects with a pasted Admin API token or through a Dev Dashboard
# app (client ID + secret); shopify_auth gets and renews the app's tokens.

SHOPIFY_OAUTH_KEY = "shopify_oauth_pending"


def _store_json(r):
    granted = shopify_auth.split_scopes(r["granted_scopes"]) if r["granted_scopes"] is not None else None
    return {
        "id": r["id"], "name": r["name"], "shop_domain": r["shop_domain"], "prefix": r["prefix"],
        "no_company": r["no_company"], "is_active": r["is_active"], "created_at": r["created_at"].isoformat(),
        "auth_method": r["auth_method"], "client_id": r["client_id"],
        "client_secret": MASK if r["client_secret"] else "",
        "connected": r["auth_method"] == "token" or bool(r["token_grant"]),
        "token_grant": r["token_grant"],
        "connected_at": r["connected_at"].isoformat() if r["connected_at"] else None,
        "auth_error": r["auth_error"],
        "missing_scopes": shopify_auth.missing_scopes(granted) if granted is not None else [],
    }


def _kept(value, current):
    value = (value or "").strip()
    return current if not value or value == MASK else value


@bp.get("/shopify-stores")
@login_required
def list_stores():
    rows = db.query("SELECT * FROM shopify_stores ORDER BY id")
    return jsonify([_store_json(r) for r in rows])


@bp.post("/shopify-stores")
@admin_required
def create_store():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    method = data.get("auth_method") or "token"
    domain = shopify_auth.normalize_domain(data.get("shop_domain"))
    token = (data.get("access_token") or "").strip() if method == "token" else ""
    client_id = (data.get("client_id") or "").strip() if method == "app" else ""
    secret = (data.get("client_secret") or "").strip() if method == "app" else ""
    if not name or not domain:
        return api_error("Name and shop domain are required")
    problem = shopify_auth.store_auth_problem(method, domain, token, client_id, secret)
    if problem:
        return api_error(problem)
    row = db.execute(
        """INSERT INTO shopify_stores (name, shop_domain, access_token, prefix, no_company,
               auth_method, client_id, client_secret)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (name, domain, token, (data.get("prefix") or "").strip(), bool(data.get("no_company")),
         method, client_id, secret),
        returning=True,
    )
    audit("store.create", {"name": name, "domain": domain, "auth_method": method})
    return jsonify({"id": row["id"], "connected": method == "token"})


@bp.put("/shopify-stores/<int:store_id>")
@admin_required
def update_store(store_id):
    data = request.get_json(silent=True) or {}
    store = db.query("SELECT * FROM shopify_stores WHERE id = %s", (store_id,), one=True)
    if not store:
        return api_error("Store not found", 404)
    name = (data.get("name") or store["name"]).strip()
    method = data.get("auth_method") or store["auth_method"]
    domain = shopify_auth.normalize_domain(data.get("shop_domain") or store["shop_domain"])
    was_token = store["auth_method"] == "token"
    # A token store's saved token is the pasted one; an app store's is minted,
    # so switching to 'token' always needs a fresh paste.
    token = _kept(data.get("access_token"), store["access_token"] if was_token else "") if method == "token" else ""
    client_id = (data["client_id"] if data.get("client_id") is not None else store["client_id"]).strip()
    secret = _kept(data.get("client_secret"), store["client_secret"])
    problem = shopify_auth.store_auth_problem(method, domain, token, client_id, secret)
    if problem:
        return api_error(problem)
    reset = method == "app" and (was_token or domain != store["shop_domain"] or client_id != store["client_id"]
                                 or secret != store["client_secret"])
    prefix = data.get("prefix")
    prefix = prefix.strip() if prefix is not None else store["prefix"]
    db.execute(
        """UPDATE shopify_stores SET name=%s, shop_domain=%s, access_token=%s, prefix=%s, no_company=%s,
               is_active=%s, auth_method=%s, client_id=%s, client_secret=%s WHERE id=%s""",
        (name, domain, token if method == "token" else store["access_token"], prefix,
         bool(data.get("no_company", store["no_company"])), bool(data.get("is_active", store["is_active"])),
         method, client_id if method == "app" else store["client_id"],
         secret if method == "app" else store["client_secret"], store_id),
    )
    if reset or (method == "token" and not was_token):
        shopify_auth.disconnect(store_id)
        if method == "token":
            db.execute("UPDATE shopify_stores SET access_token = %s WHERE id = %s", (token, store_id))
    audit("store.update", {"id": store_id, "name": name, "auth_method": method})
    return jsonify({"ok": True, "connected": method == "token" or (bool(store["token_grant"]) and not reset)})


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


def _connection_result(store_id):
    shop, scopes = shopify_client.check_connection(store_id)
    return {"ok": True, "shop": shop, "missing_scopes": shopify_auth.missing_scopes(scopes)}


@bp.post("/shopify-stores/<int:store_id>/test")
@admin_required
def test_store(store_id):
    if not db.query("SELECT 1 FROM shopify_stores WHERE id = %s", (store_id,), one=True):
        return api_error("Store not found", 404)
    try:
        return jsonify(_connection_result(store_id))
    except ShopifyError as e:
        return api_error(str(e))


def _app_store(store_id):
    store = db.query("SELECT * FROM shopify_stores WHERE id = %s", (store_id,), one=True)
    return store if store and store["auth_method"] == "app" else None


@bp.post("/shopify-stores/<int:store_id>/connect")
@admin_required
def connect_store(store_id):
    """Client credentials first (no redirect, stores in the app's own Shopify
    organization); any other refusal than bad credentials sends the browser
    to Shopify's install page instead."""
    store = _app_store(store_id)
    if not store:
        return api_error("Only stores that use a Shopify app connect", 404)
    origin = str((request.get_json(silent=True) or {}).get("origin") or "").rstrip("/")
    if not PAGE_ORIGIN_RE.fullmatch(origin):
        return api_error("Open Settings from the app's own address and try again")
    try:
        shopify_auth.mint_client_credentials(store)
    except shopify_auth.ShopifyAuthRefused as e:
        if e.code in shopify_auth.BAD_CREDENTIALS:
            return api_error(f"Shopify rejected the client ID or secret — {e}")
        if e.code == "http_404":
            return api_error(f"Shopify has no store at {store['shop_domain']} — check the shop domain")
    except ShopifyError as e:
        return api_error(str(e))
    else:
        audit("store.connected", {"id": store_id, "grant": "client_credentials"})
        try:
            return jsonify({**_connection_result(store_id), "grant": "client_credentials"})
        except ShopifyError as e:
            return api_error(str(e))
    redirect_uri = origin + shopify_auth.CALLBACK_PATH
    state = secrets.token_urlsafe(24)
    session[SHOPIFY_OAUTH_KEY] = {"state": state, "store_id": store_id, "redirect_uri": redirect_uri,
                                  "at": time.time()}
    return jsonify({"url": shopify_auth.authorize_url(store, redirect_uri, state), "redirect_uri": redirect_uri})


def _finish_shopify_oauth(params, store_id=None):
    """Complete a pending install from Shopify's redirect parameters.
    Returns the store row; raises ValueError / ShopifyError."""
    pending = session.pop(SHOPIFY_OAUTH_KEY, None)
    state = params.get("state") or ""
    if (not pending or not state or not hmac.compare_digest(pending["state"], state)
            or time.time() - pending["at"] > OAUTH_STATE_TTL_S):
        raise ValueError("This Shopify connection is stale or was started elsewhere — click Connect again")
    if store_id is not None and pending["store_id"] != store_id:
        raise ValueError("That Shopify approval belongs to a different store")
    store = _app_store(pending["store_id"])
    if not store:
        raise ValueError("That store no longer connects through a Shopify app")
    if params.get("error"):
        raise ValueError(f"Shopify did not approve: {params.get('error_description') or params['error']}")
    if not shopify_auth.verify_hmac(params, store["client_secret"]):
        raise ValueError("Shopify's answer failed its signature check — click Connect again")
    shop = (params.get("shop") or "").lower()
    if shop != store["shop_domain"]:
        raise ValueError(f"Shopify answered for {shop or 'no store'}, not {store['shop_domain']}")
    if not params.get("code"):
        raise ValueError("Shopify returned no authorization code")
    store = shopify_auth.exchange_code(store, params["code"])
    audit("store.connected", {"id": store["id"], "grant": "authorization_code", "scopes": store["granted_scopes"]})
    return store


@bp.get("/oauth/shopify/callback")
def shopify_oauth_callback():
    if session.get("role") != "admin":
        return redirect("/login.html")
    try:
        store = _finish_shopify_oauth(request.args.to_dict())
        query = {"shopify": "connected", "store": store["name"]}
    except (ValueError, ShopifyError) as e:
        query = {"shopify_error": str(e)}
    return redirect(f"/settings.html?{urlencode(query)}#integrations")


@bp.post("/shopify-stores/<int:store_id>/oauth/complete")
@admin_required
def shopify_oauth_complete(store_id):
    """Fallback when the callback URL is not reachable from the browser: the
    admin pastes the address Shopify's approval landed on."""
    query = parse_qs(urlparse(str((request.get_json(silent=True) or {}).get("url") or "").strip()).query)
    try:
        store = _finish_shopify_oauth({k: v[0] for k, v in query.items()}, store_id)
    except (ValueError, ShopifyError) as e:
        return api_error(str(e))
    return jsonify({"ok": True, "store": store["name"]})


@bp.post("/shopify-stores/<int:store_id>/disconnect")
@admin_required
def disconnect_store(store_id):
    if not _app_store(store_id):
        return api_error("Only stores that use a Shopify app disconnect", 404)
    shopify_auth.disconnect(store_id)
    audit("store.disconnected", {"id": store_id})
    return jsonify({"ok": True})


# ---------- Users ----------

@bp.get("/users")
@admin_required
def list_users():
    rows = db.query(
        "SELECT id, username, role, is_active, can_ship, allowed_providers, allowed_sections, created_at"
        " FROM users ORDER BY id"
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
    ships, allowed, sections = True, None, []
    if role == "user":
        try:
            ships, allowed, sections = _user_access(data)
        except ValueError as e:
            return api_error(str(e))
    row = db.execute(
        """INSERT INTO users (username, password_hash, role, can_ship, allowed_providers, allowed_sections)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (username, generate_password_hash(password), role, ships, json.dumps(allowed) if allowed else None,
         json.dumps(sections) if sections else None),
        returning=True,
    )
    audit("user.create", {"username": username, "role": role, "can_ship": ships, "allowed_providers": allowed,
                          "allowed_sections": sections})
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


def _user_access(data):
    """(ships, allowed_providers, allowed_sections) for a non-admin user from a
    request body. No shipping account checked = the user does not ship (e.g.
    only verifies orders), which needs at least one section."""
    keys = providers.instance_keys()
    requested = [n for n in data.get("allowed_providers") or [] if str(n) in keys]
    sections = sanitize_sections(data.get("allowed_sections"))
    if not requested and not sections:
        raise ValueError("Give the user at least one shipping account or one section — "
                         "to remove all access, deactivate the user")
    if not requested:
        return False, None, sections
    return True, providers.sanitize_allowed(requested), sections


@bp.put("/users/<int:user_id>/access")
@admin_required
def set_user_access(user_id):
    """Shipping accounts and sections, saved together. Every account checked =
    unrestricted; none = the user does not ship. Admins are never restricted."""
    row = db.query("SELECT id, role, username FROM users WHERE id = %s", (user_id,), one=True)
    if not row:
        return api_error("User not found", 404)
    if row["role"] == "admin":
        return api_error("Admins always have access to everything")
    try:
        ships, allowed, sections = _user_access(request.get_json(silent=True) or {})
    except ValueError as e:
        return api_error(str(e))
    db.execute(
        "UPDATE users SET can_ship = %s, allowed_providers = %s, allowed_sections = %s WHERE id = %s",
        (ships, json.dumps(allowed) if allowed else None, json.dumps(sections) if sections else None, user_id),
    )
    detail = {"id": user_id, "can_ship": ships, "allowed_providers": allowed, "allowed_sections": sections}
    audit("user.access", detail)
    return jsonify({"ok": True, **{k: v for k, v in detail.items() if k != "id"}})


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

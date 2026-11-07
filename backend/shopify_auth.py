"""Access tokens for Shopify stores.

A store connects one of two ways (shopify_stores.auth_method):
  * 'token' — an Admin API token pasted from a legacy custom app; used as is.
  * 'app'   — a Dev Dashboard app's client ID + secret. EasyShip gets the
    tokens itself from https://{shop}/admin/oauth/access_token:
      - client credentials grant, when the app and the store share a Shopify
        organization (24 h token, minted again when it runs out);
      - authorization code grant, after the admin approves the install on
        Shopify's page (1 h expiring offline token + 90-day refresh token
        that rotates on every refresh).

fresh_store() is the one place every Shopify call gets its token from.
"""
import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

import db
from shopify_errors import ShopifyError, ShopifyUnavailable

REQUIRED_SCOPES = (
    "read_orders",
    "write_orders",
    "read_customers",
    "read_products",
    "read_inventory",
    "read_merchant_managed_fulfillment_orders",
    "write_merchant_managed_fulfillment_orders",
)
SHOP_RE = re.compile(r"[a-z0-9][a-z0-9\-]*\.myshopify\.com")
CALLBACK_PATH = "/api/oauth/shopify/callback"
TOKEN_MARGIN = timedelta(minutes=5)
# Refusals that mean the client ID / secret themselves are wrong — retrying
# through the install page would fail the same way.
BAD_CREDENTIALS = {"invalid_client", "unauthorized_client"}

NOT_CONNECTED = "{name} isn't connected to Shopify yet — Settings › Integrations › Connect"
RECONNECT = "{name} lost its Shopify connection — Settings › Integrations › Reconnect"

_TOKEN_COLUMNS_CLEARED = """access_token = '', token_grant = NULL, token_expires_at = NULL,
    refresh_token = NULL, refresh_expires_at = NULL, granted_scopes = NULL, connected_at = NULL"""


class ShopifyAuthRefused(ShopifyError):
    """Shopify's token endpoint said no; code is its OAuth error code."""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


def _now():
    return datetime.now(timezone.utc)


# ---- pure helpers ----
def normalize_domain(raw):
    domain = (raw or "").strip().lower()
    domain = re.sub(r"^[a-z]+://", "", domain)
    return domain.split("/", 1)[0]


def store_auth_problem(method, domain, token, client_id, client_secret):
    """What a store's connection settings lack, or None when complete."""
    if method == "token":
        return None if token else "An Admin API access token is required"
    if method != "app":
        return "Unknown connection method"
    if not SHOP_RE.fullmatch(domain):
        return "A Shopify app needs the store's *.myshopify.com domain"
    if not client_id or not client_secret:
        return "The app's client ID and client secret are required"
    return None


def missing_scopes(granted):
    """Required scopes the grant lacks; a write_x grant covers read_x."""
    have = set(granted or ())
    return [s for s in REQUIRED_SCOPES
            if s not in have and not (s.startswith("read_") and "write_" + s[5:] in have)]


def split_scopes(text):
    return [s.strip() for s in (text or "").split(",") if s.strip()]


def verify_hmac(params, secret):
    """Shopify signs its redirect: HMAC-SHA256 with the client secret over
    the other query parameters, sorted, joined as k=v&k=v."""
    given = params.get("hmac") or ""
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k not in ("hmac", "signature"))
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return bool(given) and hmac.compare_digest(digest, given)


def authorize_url(store, redirect_uri, state):
    query = urlencode({"client_id": store["client_id"], "scope": ",".join(REQUIRED_SCOPES),
                       "redirect_uri": redirect_uri, "state": state})
    return f"https://{store['shop_domain']}/admin/oauth/authorize?{query}"


def token_is_fresh(store, now):
    if not store.get("access_token"):
        return False
    expires = store.get("token_expires_at")
    return expires is None or expires - now > TOKEN_MARGIN


def _error_of(resp, data):
    if data.get("error"):
        return str(data["error"]), str(data.get("error_description") or data["error"])
    text = (resp.text or "")[:300]
    found = re.search(r"Oauth error (\w+)", text)
    return (found.group(1) if found else f"http_{resp.status_code}"), text or f"HTTP {resp.status_code}"


# ---- token endpoint ----
def _token_post(shop, body):
    try:
        resp = requests.post(f"https://{shop}/admin/oauth/access_token", data=body,
                             headers={"Accept": "application/json"}, timeout=30)
    except requests.RequestException as e:
        raise ShopifyUnavailable(f"Shopify sign-in unreachable: {e}")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise ShopifyUnavailable(f"Shopify sign-in returned {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if resp.status_code != 200 or not data.get("access_token"):
        code, text = _error_of(resp, data)
        raise ShopifyAuthRefused(f"Shopify refused the sign-in ({code}): {text}", code=code)
    return data


def _credentials(store):
    return {"client_id": store["client_id"], "client_secret": store["client_secret"]}


def _seconds(value):
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _save(store, grant, data):
    now = _now()
    lifetime, refresh_lifetime = _seconds(data.get("expires_in")), _seconds(data.get("refresh_token_expires_in"))
    return db.execute(
        """UPDATE shopify_stores SET access_token = %s, token_grant = %s, token_expires_at = %s,
               refresh_token = %s, refresh_expires_at = %s, granted_scopes = %s,
               connected_at = COALESCE(connected_at, %s), auth_error = NULL
           WHERE id = %s RETURNING *""",
        (data["access_token"], grant, now + timedelta(seconds=lifetime) if lifetime else None,
         data.get("refresh_token"), now + timedelta(seconds=refresh_lifetime) if refresh_lifetime else None,
         data.get("scope"), now, store["id"]),
        returning=True,
    )


def mint_client_credentials(store):
    data = _token_post(store["shop_domain"], {"grant_type": "client_credentials", **_credentials(store)})
    return _save(store, "client_credentials", data)


def exchange_code(store, code):
    data = _token_post(store["shop_domain"], {"code": code, "expiring": "1", **_credentials(store)})
    db.execute(f"UPDATE shopify_stores SET {_TOKEN_COLUMNS_CLEARED} WHERE id = %s", (store["id"],))
    return _save(store, "authorization_code", data)


def _renew(store):
    if store["token_grant"] == "client_credentials":
        return mint_client_credentials(store)
    expires = store.get("refresh_expires_at")
    if not store.get("refresh_token") or (expires is not None and expires <= _now()):
        raise ShopifyAuthRefused("The refresh token has expired", code="invalid_request")
    data = _token_post(store["shop_domain"], {"grant_type": "refresh_token",
                                              "refresh_token": store["refresh_token"], **_credentials(store)})
    return _save(store, "authorization_code", data)


def _read(store_id):
    store = db.query("SELECT * FROM shopify_stores WHERE id = %s", (store_id,), one=True)
    if not store:
        raise ShopifyError("Shopify store not found")
    return store


def fresh_store(store):
    """The store row with an access token good for a while yet — minted or
    refreshed under a per-store lock when an app store's token runs out."""
    if store.get("auth_method") != "app" or (store.get("token_grant") and token_is_fresh(store, _now())):
        return store
    if not store.get("token_grant"):
        raise ShopifyError((RECONNECT if store.get("auth_error") else NOT_CONNECTED).format(name=store["name"]))
    lock = f"shopify-token:{store['id']}"
    db.query("SELECT pg_advisory_lock(hashtext(%s))", (lock,))
    try:
        store = _read(store["id"])  # another worker may have just renewed it
        if not store.get("token_grant"):
            raise ShopifyError(RECONNECT.format(name=store["name"]))
        if token_is_fresh(store, _now()):
            return store
        try:
            return _renew(store)
        except ShopifyAuthRefused as e:
            mark_failed(store["id"], str(e))
            raise ShopifyError(RECONNECT.format(name=store["name"])) from e
    finally:
        db.query("SELECT pg_advisory_unlock(hashtext(%s))", (lock,))


def invalidate(store_id):
    """Shopify refused the token: renew it on the next call."""
    db.execute("UPDATE shopify_stores SET token_expires_at = now() WHERE id = %s AND auth_method = 'app'",
               (store_id,))


def mark_failed(store_id, error):
    db.execute(f"UPDATE shopify_stores SET {_TOKEN_COLUMNS_CLEARED}, auth_error = %s WHERE id = %s",
               (error[:500], store_id))


def disconnect(store_id):
    db.execute(f"UPDATE shopify_stores SET {_TOKEN_COLUMNS_CLEARED}, auth_error = NULL WHERE id = %s",
               (store_id,))

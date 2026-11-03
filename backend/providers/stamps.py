"""Endicia direct — the Stamps.com/Endicia REST API ("SERA") implementation of
the ShippingProvider interface. Endicia merged into Stamps.com on Jan 31 2026;
an Endicia login is a Stamps.com account, and SERA is that account's API.

SERA's model differs from the label platforms; the adapter absorbs it:
  * OAuth 2.0 authorization code only. An admin signs in once on Stamps.com's
    hosted page (Settings → Connect), the callback route hands the code to
    `complete_sign_in`, and the long-lived refresh token is kept in settings
    per mode. Access tokens are refreshed under a Postgres advisory lock so
    gunicorn workers never race a rotating refresh token;
  * rates are stateless and per package (`POST /v1/rates`), so each box's
    request lives in `stamps_drafts` between rating and buying, and the group
    is offered only the services every box got, priced as the sum;
  * every POST takes an `Idempotency-Key` that SERA replays for 24 h. A box's
    purchase key and frozen request body are saved BEFORE the label request,
    so a lost response is re-sent with the same key — never bought twice. An
    unconfirmed purchase older than the replay window is refused instead;
  * labels come back synchronously (4x6, base64), so there is no polling; the
    label id replaces the draft id once bought, and void / manifest use it.
"""
import base64
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

import db
from providers import labels
from providers.base import (
    DraftShipment,
    LabelStatus,
    ManifestResult,
    ProviderError,
    Rate,
    ShipmentState,
    ShippingProvider,
    missing_origin_fields,
    origin_descriptor,
    parcel_dimensions,
)
from providers.shipstation import _map_parallel
from util import CENTRAL

BASE_URLS = {
    "sandbox": "https://api.testing.stampsendicia.com/sera",
    "production": "https://api.stampsendicia.com/sera",
}
SIGNIN_URLS = {
    "sandbox": "https://signin.testing.stampsendicia.com",
    "production": "https://signin.stampsendicia.com",
}
CALLBACK_PATH = "/api/oauth/stamps/callback"
DRAFT_PREFIX = "sed_"
CARRIER = "usps"
UMBRELLA_NAME = "USPS"
PLAIN_PACKAGE = "package"
LABEL_FORMATS = ("pdf", "zpl", "png")
CONFIRMATION = {"signature": "signature", "adult": "adult_signature"}
# Refresh this long before the access token runs out, so a token never
# expires between being read in the request and used in a worker thread.
TOKEN_MARGIN_S = 300
# SERA replays an idempotent POST for 24 h; past this an unconfirmed purchase
# cannot be safely re-sent.
REPLAY_WINDOW_HOURS = 23
SIGN_IN_STATUSES = (400, 401, 403)

NOT_CONNECTED = "Endicia is not connected for {env} — click Connect Endicia account in Settings"
SIGN_IN_EXPIRED = "The Endicia sign-in has expired or was revoked — click Connect Endicia account in Settings"
UNCONFIRMED_PURCHASE = ("a label purchase was sent but never confirmed — resume the purchase to finish it, "
                        "or check the Stamps.com print history before voiding")
STALE_PURCHASE = ("An earlier purchase of this box was never confirmed and is too old to re-send safely — "
                  "check the Stamps.com print history for it, then get rates again")

ORIGIN_REQUIRED = {
    "origin_company": "Company",
    "origin_address1": "Address 1",
    "origin_city": "City",
    "origin_state": "State",
    "origin_zip": "ZIP",
    "origin_phone": "Phone",
}

SERVICE_NAMES = {
    "usps_first_class_mail": "USPS First-Class Mail",
    "usps_ground_advantage": "USPS Ground Advantage",
    "usps_priority_mail": "USPS Priority Mail",
    "usps_priority_mail_express": "USPS Priority Mail Express",
    "usps_parcel_select": "USPS Parcel Select Ground",
    "usps_media_mail": "USPS Media Mail",
    "usps_first_class_mail_international": "USPS First-Class Mail International",
    "usps_priority_mail_international": "USPS Priority Mail International",
    "usps_priority_mail_express_international": "USPS Priority Mail Express International",
}


def _env(mode):
    return "the test environment" if mode == "sandbox" else "production"


def _mode(key):
    mode = db.get_setting(f"{key}_mode") or "sandbox"
    return mode if mode in BASE_URLS else "sandbox"


def _client(key, mode):
    client_id = (db.get_setting(f"{key}_{mode}_client_id") or "").strip()
    if not client_id:
        raise ProviderError(f"Enter the Stamps.com {mode} client ID and save before connecting")
    return client_id, (db.get_setting(f"{key}_{mode}_client_secret") or "").strip()


# ---- tokens ----
def _token_post(mode, body):
    try:
        resp = requests.post(f"{SIGNIN_URLS[mode]}/oauth/token", json=body,
                             headers={"Accept": "application/json"}, timeout=30)
    except requests.RequestException as e:
        raise ProviderError(f"Stamps.com sign-in service unreachable: {e}", status=None)
    if resp.status_code >= 400:
        raise ProviderError(_extract_error(resp, "Stamps.com sign-in"), status=resp.status_code)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not data.get("access_token"):
        raise ProviderError("Stamps.com sign-in returned no access token", status=None)
    return data


def _save_tokens(key, mode, data):
    try:
        lifetime = int(float(data.get("expires_in") or 3600))
    except (TypeError, ValueError):
        lifetime = 3600
    db.set_setting(f"{key}_{mode}_access_token", data["access_token"])
    db.set_setting(f"{key}_{mode}_token_expires_at", str(time.time() + lifetime))
    if data.get("refresh_token"):  # rotated, or kept when the server reuses it
        db.set_setting(f"{key}_{mode}_refresh_token", data["refresh_token"])


def _fresh_token(key, mode):
    token = db.get_setting(f"{key}_{mode}_access_token")
    try:
        expires_at = float(db.get_setting(f"{key}_{mode}_token_expires_at") or 0)
    except ValueError:
        expires_at = 0
    return token if token and expires_at - time.time() > TOKEN_MARGIN_S else None


def _forget_tokens(key, mode, keep_refresh=False):
    names = ("access_token", "token_expires_at") if keep_refresh else (
        "access_token", "token_expires_at", "refresh_token", "account")
    for name in names:
        db.set_setting(f"{key}_{mode}_{name}", "")


def _auth(key, mode=None):
    """(base_url, access_token) for the instance's mode, read in the request
    context so parallel worker threads can authenticate."""
    mode = mode or _mode(key)
    token = _fresh_token(key, mode)
    if token:
        return BASE_URLS[mode], token
    if not db.get_setting(f"{key}_{mode}_refresh_token"):
        raise ProviderError(NOT_CONNECTED.format(env=_env(mode)))
    lock = f"stamps-token:{key}:{mode}"
    db.query("SELECT pg_advisory_lock(hashtext(%s))", (lock,))
    try:
        token = _fresh_token(key, mode)  # another worker may have just refreshed
        if token:
            return BASE_URLS[mode], token
        client_id, secret = _client(key, mode)
        body = {"grant_type": "refresh_token", "client_id": client_id,
                "refresh_token": db.get_setting(f"{key}_{mode}_refresh_token")}
        if secret:
            body["client_secret"] = secret
        try:
            data = _token_post(mode, body)
        except ProviderError as e:
            if e.status in SIGN_IN_STATUSES:
                _forget_tokens(key, mode)
                raise ProviderError(SIGN_IN_EXPIRED, status=401)
            raise
        _save_tokens(key, mode, data)
        return BASE_URLS[mode], data["access_token"]
    finally:
        db.query("SELECT pg_advisory_unlock(hashtext(%s))", (lock,))


# ---- HTTP ----
def _extract_error(resp, source="Endicia"):
    """SERA errors carry error_code + error_message (shape not fully
    documented); the sign-in service answers OAuth-style error/description."""
    try:
        data = resp.json()
    except ValueError:
        return f"{source} error ({resp.status_code}): {(resp.text or '').strip()[:300] or 'request failed'}"
    entries = data.get("errors") if isinstance(data, dict) and isinstance(data.get("errors"), list) else [data]
    parts = []
    for err in entries:
        if not isinstance(err, dict):
            if err:
                parts.append(str(err))
            continue
        message = (err.get("error_message") or err.get("message") or err.get("error_description")
                   or err.get("detail") or err.get("title") or err.get("error") or "")
        field = err.get("field") or err.get("field_name")
        if message:
            parts.append(f"{field}: {message}" if field else str(message))
    detail = " | ".join(parts) or str(data)[:300]
    return f"{source} error ({resp.status_code}): {detail}"


def _request(method, path, auth, json_body=None, params=None, idempotency_key=None, timeout=45):
    base_url, token = auth
    url = path if path.startswith("http") else f"{base_url}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    # GETs, and POSTs carrying an idempotency key (SERA replays those), ride
    # through transient network errors and gateway 5xx.
    retry_recoverable = method.upper() == "GET" or bool(idempotency_key)
    resp = None
    last_exc = None
    for attempt in range(4):
        try:
            resp = requests.request(method, url, json=json_body, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_exc, resp = e, None
            if retry_recoverable and attempt < 3:
                time.sleep(min(1.5 * (attempt + 1), 10))
                continue
            break
        if resp.status_code == 429 and attempt < 3:
            time.sleep(min(1.5 * (attempt + 1), 15))
            continue
        if retry_recoverable and resp.status_code >= 500 and attempt < 3:
            time.sleep(min(1.5 * (attempt + 1), 10))
            continue
        break
    if resp is None:
        raise ProviderError(f"Endicia request failed: {last_exc}", status=None)
    if resp.status_code >= 400:
        raise ProviderError(_extract_error(resp), status=resp.status_code)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


def _download(url, auth):
    """A document SERA links to; signed URLs need no token, others may."""
    for headers in ({}, {"Authorization": f"Bearer {auth[1]}"}):
        try:
            resp = requests.request("GET", url, headers=headers, timeout=30)
        except requests.RequestException:
            return None
        if resp.ok and resp.content:
            return resp.content
        if resp.status_code not in (401, 403):
            return None
    return None


# ---- request bodies ----
def _compact(address):
    return {k: v for k, v in address.items() if v not in (None, "")}


def _origin_address(origin):
    missing = missing_origin_fields(origin, ORIGIN_REQUIRED)
    if missing:
        raise ProviderError(
            "Origin address is incomplete — fill in on the Settings page: " + ", ".join(missing)
        )
    company = origin.get("origin_company") or ""
    return _compact({
        "name": origin.get("origin_contact") or company,
        "company_name": company,
        "address_line1": origin.get("origin_address1"),
        "address_line2": origin.get("origin_address2"),
        "city": origin.get("origin_city"),
        "state_province": (origin.get("origin_state") or "").strip().upper(),
        "postal_code": (origin.get("origin_zip") or "").strip(),
        "country_code": "US",
        "phone": origin.get("origin_phone"),
        "email": origin.get("origin_email"),
    })


def _dest_address(dest):
    return _compact({
        "name": dest.get("contact") or dest.get("company") or "Recipient",
        "company_name": dest.get("company"),
        "address_line1": dest.get("address1"),
        "address_line2": dest.get("address2"),
        "city": dest.get("city"),
        "state_province": (dest.get("state") or "").strip().upper(),
        "postal_code": (dest.get("zip") or "").strip(),
        "country_code": dest.get("country") or "US",
        "phone": dest.get("phone"),
        "email": dest.get("email"),
    })


def _package(parcel):
    """Weight in pounds, dimensions in inches (SERA takes both as is) — no
    dimensions unless every side is given, so a 0×0×0 box prices by weight."""
    try:
        weight = float(str(parcel.get("weight") or "0").strip())
    except ValueError:
        weight = 0.0
    package = {"packaging_type": PLAIN_PACKAGE, "weight": round(weight, 3), "weight_unit": "pound"}
    dims = parcel_dimensions(parcel)
    if dims:
        package.update(dims, dimension_unit="inch")
    return package


def _ship_date():
    return datetime.now(CENTRAL).strftime("%Y-%m-%d")


def _rate_body(origin, dest, parcel, options):
    body = {"from_address": origin, "to_address": dest, "package": _package(parcel), "ship_date": _ship_date()}
    confirmation = CONFIRMATION.get((options or {}).get("signature") or "none")
    if confirmation:
        body["delivery_confirmation_type"] = confirmation
    return body


def _draft_body(origin, dest, parcel, options):
    """What a box's label request needs beyond the service: the rate body plus
    the order reference, which only the label endpoint takes."""
    body = _rate_body(origin, dest, parcel, options)
    reference = str((options or {}).get("reference") or "").strip()
    if reference:
        body["references"] = {"reference1": reference[:50]}
    return body


def _label_body(draft_body, service_type, label_format, test_label):
    return {
        **draft_body,
        "service_type": service_type,
        "ship_date": _ship_date(),
        "label_options": {"label_size": "4x6", "label_format": label_format, "label_output_type": "base64"},
        "is_test_label": test_label,
    }


# ---- rates ----
def _service_name(service_type):
    if service_type in SERVICE_NAMES:
        return SERVICE_NAMES[service_type]
    words = (service_type or "").split("_")
    return " ".join(w.upper() if w in ("usps", "ups", "dhl") else w.capitalize() for w in words if w)


def _delivery_days(value):
    """(min, max) from SERA's '2' or '1-2'."""
    numbers = [int(n) for n in re.findall(r"\d+", str(value or ""))]
    if not numbers:
        return None, None
    return min(numbers), max(numbers)


def _rate_total(rate):
    try:
        return float((rate.get("shipment_cost") or {}).get("total_amount"))
    except (TypeError, ValueError):
        return None


def _usable(rate):
    # USPS services come back once per packaging (flat-rate boxes included);
    # the label is always bought as a plain package, so only that price is honest.
    return (
        (rate.get("carrier") or CARRIER) == CARRIER
        and (rate.get("packaging_type") or PLAIN_PACKAGE) == PLAIN_PACKAGE
        and bool(rate.get("service_type"))
        and _rate_total(rate) is not None
    )


def _cheapest_by_service(rates):
    out = {}
    for r in rates or []:
        if not isinstance(r, dict) or not _usable(r):
            continue
        sid = r["service_type"]
        if sid not in out or _rate_total(r) < _rate_total(out[sid]):
            out[sid] = r
    return out


def _combine_per_box(rate_lists, provider):
    """Services every box got, priced as the sum of the per-box labels,
    delivery as the slowest box; cheapest first and flagged best value."""
    per_box = [_cheapest_by_service(rs) for rs in rate_lists]
    if not per_box:
        return []
    combined = []
    for sid in set.intersection(*(set(m) for m in per_box)):
        rs = [m[sid] for m in per_box]
        days = [_delivery_days(r.get("estimated_delivery_days")) for r in rs]
        mins = [d[0] for d in days if d[0] is not None]
        maxes = [d[1] for d in days if d[1] is not None]
        combined.append(Rate(
            provider=provider,
            provider_service_id=sid,
            courier_name=_service_name(sid),
            umbrella_name=UMBRELLA_NAME,
            total_charge=round(sum(_rate_total(r) for r in rs), 2),
            currency=((rs[0].get("shipment_cost") or {}).get("currency") or "usd").upper(),
            min_delivery_time=max(mins) if mins else None,
            max_delivery_time=max(maxes) if maxes else None,
            value_for_money_rank=None,
        ))
    combined.sort(key=lambda r: r.total_charge)
    if combined:
        combined[0].value_for_money_rank = 1
    return combined


# ---- label states ----
def _stored_label(label):
    """The purchase response without its (large, re-printable) documents."""
    return {k: v for k, v in label.items() if k not in ("labels", "forms")}


def _ready_state(label):
    tracking = label.get("tracking_number")
    cost = _rate_total(label)
    return ShipmentState(
        provider_shipment_id=label.get("label_id"),
        label_status=LabelStatus.READY,
        tracking_numbers=[tracking] if tracking else [],
        courier_name=_service_name(label.get("service_type")),
        courier_umbrella_name=UMBRELLA_NAME,
        cost=round(cost, 2) if cost is not None else None,
        raw=label,
    )


def _failed_state(error):
    return ShipmentState(provider_shipment_id=None, label_status=LabelStatus.FAILED, error_message=str(error))


def _not_created_state():
    return ShipmentState(provider_shipment_id=None, label_status=LabelStatus.NOT_CREATED)


def _documents(payload, fmt, auth=None):
    """[(bytes, format)] from a label or reprint response: inline base64 data,
    else the linked file."""
    docs = []
    for entry in (payload.get("labels") or []) + (payload.get("forms") or []):
        if not isinstance(entry, dict):
            continue
        data = None
        if entry.get("label_data"):
            try:
                data = base64.b64decode(entry["label_data"])
            except (ValueError, TypeError):
                data = None
        elif entry.get("href") and auth:
            data = _download(entry["href"], auth)
        if data:
            docs.append((data, labels.sniff_label_format(data, fmt)))
    return docs


class StampsProvider(ShippingProvider):
    platform = "stamps"
    label = "Endicia (direct)"
    modes = ("sandbox", "production")
    oauth_callback_path = CALLBACK_PATH

    def _label_format(self):
        fmt = (self.setting("label_format") or "pdf").lower()
        return fmt if fmt in LABEL_FORMATS else "pdf"

    def _test_labels(self):
        return self.setting("test_labels") == "true"

    def _forget_on_auth_error(self, results):
        """A refused access token is dropped so the next call refreshes it."""
        if any(isinstance(r, ProviderError) and r.status == 401 for r in results):
            _forget_tokens(self.name, _mode(self.name), keep_refresh=True)

    # ---- sign-in ----
    def authorize_url(self, mode, redirect_uri, state):
        mode = mode if mode in BASE_URLS else self.active_mode()
        client_id, _ = _client(self.name, mode)
        query = urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
                           "scope": "offline_access", "state": state})
        return f"{SIGNIN_URLS[mode]}/authorize?{query}"

    def complete_sign_in(self, mode, code, redirect_uri):
        """Exchange the authorization code for tokens; returns the account name."""
        mode = mode if mode in BASE_URLS else self.active_mode()
        client_id, secret = _client(self.name, mode)
        body = {"grant_type": "authorization_code", "client_id": client_id, "code": code,
                "redirect_uri": redirect_uri}
        if secret:
            body["client_secret"] = secret
        data = _token_post(mode, body)
        if not data.get("refresh_token"):
            raise ProviderError("Stamps.com returned no refresh token — the client must allow offline_access")
        _save_tokens(self.name, mode, data)
        account = _request("GET", "/v1/account", (BASE_URLS[mode], data["access_token"])).get("account") or {}
        name = ((account.get("customer_details") or {}).get("username")
                or str(account.get("user_id") or account.get("customer_id") or "Stamps.com account"))
        self.set_account_name(mode, name)
        return name

    def set_account_name(self, mode, name):
        db.set_setting(self.setting_key(f"{mode}_account"), name)

    def disconnect(self, mode):
        _forget_tokens(self.name, mode if mode in BASE_URLS else self.active_mode())

    def signed_in_accounts(self):
        """{mode: account name, '' when not connected} — never the tokens."""
        return {m: (self.setting(f"{m}_account") or "connected") if self.setting(f"{m}_refresh_token") else ""
                for m in self.modes}

    # ---- drafts (the local half of an Endicia "shipment") ----
    def _save_drafts(self, bodies):
        ids = []
        for body in bodies:
            draft_id = DRAFT_PREFIX + uuid.uuid4().hex[:12]
            db.execute("INSERT INTO stamps_drafts (draft_id, provider, body) VALUES (%s, %s, %s)",
                       (draft_id, self.name, json.dumps(body)))
            ids.append(draft_id)
        # Unbought drafts are never billed; keep the table from growing forever.
        db.execute(
            """DELETE FROM stamps_drafts
               WHERE label_id IS NULL AND buy_key IS NULL AND created_at < now() - interval '30 days'""",
        )
        return ids

    def _draft_for(self, box_id):
        """A box's draft by its draft id, or by its label id once bought."""
        column = "draft_id" if (box_id or "").startswith(DRAFT_PREFIX) else "label_id"
        return db.query(f"SELECT * FROM stamps_drafts WHERE {column} = %s AND provider = %s",
                        (box_id, self.name), one=True)

    # ---- rating / drafting ----
    def create_draft_shipments(self, destination, parcels, items, options=None):
        auth = _auth(self.name)
        origin = _origin_address(self.origin())
        dest = _dest_address(destination)
        bodies = [_draft_body(origin, dest, p, options) for p in parcels]

        def rate_call(index):
            body = {k: v for k, v in bodies[index].items() if k != "references"}
            resp = _request("POST", "/v1/rates", auth, json_body=body, params={"carriers": CARRIER},
                            idempotency_key=str(uuid.uuid4()), timeout=60)
            return resp if isinstance(resp, list) else (resp.get("rates") or [])

        results = _map_parallel(list(range(len(parcels))), rate_call)
        failed = [r for r in results.values() if isinstance(r, ProviderError)]
        if failed:
            self._forget_on_auth_error(failed)
            raise ProviderError("Endicia rating failed: " + " | ".join(dict.fromkeys(str(e) for e in failed)))
        quotes = _combine_per_box([results[i] for i in range(len(parcels))], self.name)
        if not quotes:
            raise ProviderError("Endicia returned no USPS rates for these boxes")
        drafts = [DraftShipment(d) for d in self._save_drafts(bodies)]
        return drafts, quotes, []

    def get_excluded_service_ids(self):
        raw = self.setting("excluded_service_ids")
        if not raw:
            return set()
        try:
            return {str(i) for i in json.loads(raw) if i}
        except (ValueError, TypeError):
            return set()

    def set_excluded_service_ids(self, ids):
        clean = sorted({str(i) for i in ids if i})
        db.set_setting(self.setting_key("excluded_service_ids"), json.dumps(clean))
        return clean

    # ---- label lifecycle ----
    def _claim_purchase(self, draft, service_type):
        """(idempotency key, frozen label body) for a box — a new key saved
        before anything is sent, or the in-flight one to re-send. Returns a
        FAILED state instead when re-sending is no longer safe."""
        if draft.get("buy_key"):
            if draft.get("service_id") != service_type:
                raise ProviderError(
                    f"This box is already being bought as {_service_name(draft.get('service_id'))} — "
                    "resume that purchase or get rates again")
            started = draft.get("buy_started_at")
            if started and datetime.now(timezone.utc) - started > timedelta(hours=REPLAY_WINDOW_HOURS):
                return _failed_state(STALE_PURCHASE)
            return draft["buy_key"], draft["buy_body"]
        key = str(uuid.uuid4())
        body = _label_body(draft["body"], service_type, self._label_format(), self._test_labels())
        claimed = db.execute(
            """UPDATE stamps_drafts SET buy_key = %s, buy_body = %s, service_id = %s, buy_started_at = now()
               WHERE draft_id = %s AND buy_key IS NULL RETURNING draft_id""",
            (key, json.dumps(body), service_type, draft["draft_id"]), returning=True,
        )
        if not claimed:  # another worker claimed it first — re-send its key
            return self._claim_purchase(self._draft_for(draft["draft_id"]), service_type)
        return key, body

    def buy_labels(self, provider_shipment_ids, service_id):
        out, sends, draft_ids = {}, {}, {}
        for bid in provider_shipment_ids:
            try:
                draft = self._draft_for(bid)
                if not draft:
                    raise ProviderError("Endicia quote not found — get rates again")
                if draft.get("label_id"):
                    out[bid] = _ready_state(draft.get("label") or {"label_id": draft["label_id"]})
                    continue
                claim = self._claim_purchase(draft, service_id)
                if isinstance(claim, ShipmentState):
                    out[bid] = claim
                    continue
                sends[bid], draft_ids[bid] = claim, draft["draft_id"]
            except ProviderError as e:
                out[bid] = e
        if not sends:
            return out
        auth = _auth(self.name)

        def purchase(bid):
            key, body = sends[bid]
            return _request("POST", "/v1/labels", auth, json_body=body, idempotency_key=key, timeout=90)

        for bid, res in _map_parallel(list(sends), purchase).items():
            if isinstance(res, ProviderError):
                if res.recoverable:
                    out[bid] = res  # the key stays: the buy loop re-sends it and SERA replays
                else:
                    # Rejected, so nothing was charged: a later attempt gets a new key.
                    db.execute("""UPDATE stamps_drafts SET buy_key = NULL, buy_body = NULL, service_id = NULL,
                                  buy_started_at = NULL WHERE draft_id = %s""", (draft_ids[bid],))
                    out[bid] = _failed_state(res)
                continue
            if not res.get("label_id"):
                out[bid] = ProviderError("Endicia returned no label id", status=None)
                continue
            db.execute("UPDATE stamps_drafts SET label_id = %s, label = %s WHERE draft_id = %s",
                       (res["label_id"], json.dumps(_stored_label(res)), draft_ids[bid]))
            out[bid] = _ready_state(res)
        self._forget_on_auth_error(out.values())
        return out

    def poll_shipments(self, provider_shipment_ids, service_id=None):
        """Labels are synchronous, so this only reads the drafts: a box with no
        label yet is NOT_CREATED and the buy loop re-sends its saved key."""
        out = {}
        for bid in provider_shipment_ids:
            draft = self._draft_for(bid)
            if not draft:
                out[bid] = ProviderError("Endicia quote not found — get rates again")
            elif draft.get("label_id"):
                out[bid] = _ready_state(draft.get("label") or {"label_id": draft["label_id"]})
            else:
                out[bid] = _not_created_state()
        return out

    def fetch_labels(self, state):
        raw = state.raw or {}
        fmt = self._label_format()
        docs = _documents(raw, fmt)
        if docs or not raw.get("label_id"):
            return docs
        auth = _auth(self.name)
        reprint = _request("GET", f"/v1/labels/{raw['label_id']}", auth, params={
            "label_size": "4x6", "label_format": fmt, "label_output_type": "base64"}, timeout=60)
        return _documents(reprint, fmt, auth)

    def cancel_all(self, provider_shipment_ids):
        errors = []
        auth = None
        for bid in dict.fromkeys(i for i in provider_shipment_ids if i):
            draft = self._draft_for(bid)
            label_id = (draft or {}).get("label_id") or (None if bid.startswith(DRAFT_PREFIX) else bid)
            if not label_id:
                if draft and draft.get("buy_key"):
                    errors.append(f"{bid}: {UNCONFIRMED_PURCHASE}")
                elif draft:
                    db.execute("DELETE FROM stamps_drafts WHERE draft_id = %s AND label_id IS NULL "
                               "AND buy_key IS NULL", (bid,))
                continue
            try:
                auth = auth or _auth(self.name)
                _request("PUT", f"/v1/labels/{label_id}/void", auth)
            except ProviderError as e:
                if "already" in str(e).lower():
                    continue
                errors.append(f"{label_id}: {e}")
        return errors

    def get_raw_shipment(self, provider_shipment_id):
        draft = self._draft_for(provider_shipment_id)
        if not draft:
            raise ProviderError("Endicia draft not found")
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in draft.items()}

    # ---- manifests (USPS SCAN form) ----
    def supports_manifests(self):
        return True

    def create_manifest(self, provider_shipment_ids):
        ids = list(dict.fromkeys(i for i in provider_shipment_ids if i))
        label_ids = []
        for bid in ids:
            draft = self._draft_for(bid)
            label_id = (draft or {}).get("label_id") or (None if bid.startswith(DRAFT_PREFIX) else bid)
            if label_id and label_id not in label_ids:
                label_ids.append(label_id)
        if not label_ids:
            raise ProviderError("No labels to manifest")
        auth = _auth(self.name)
        resp = _request("POST", "/v1/manifests", auth,
                        json_body={"label_ids": label_ids, "label_options": {"label_format": "pdf"}},
                        idempotency_key=str(uuid.uuid4()), timeout=90)
        manifests = resp if isinstance(resp, list) else [resp]
        results = []
        for i, m in enumerate(manifests):
            docs = [(data, "pdf") for data in (_download(e["href"], auth) for e in m.get("labels") or []
                                                if isinstance(e, dict) and e.get("href")) if data]
            document = labels.merge_label_documents(docs) if docs else None
            results.append(ManifestResult(
                provider_manifest_id=m.get("manifest_id"),
                ref_number=None,
                shipment_count=int(m.get("number_of_items_in_manifest") or len(label_ids)),
                # SERA does not say which labels each manifest covers; the
                # first carries them all so none is left looking unmanifested.
                provider_shipment_ids=ids if i == 0 else [],
                document=document if document and document[0] else None,
                raw=m,
            ))
        return results

    # ---- settings surface ----
    def list_item_categories(self):
        return []

    def list_courier_services(self):
        data = _request("GET", "/v1/carrier-services", _auth(self.name))
        services = {}
        for option in data.get("shipping_options") or []:
            if option.get("carrier") != CARRIER:
                continue
            for s in option.get("services") or []:
                sid = s.get("service_type")
                if sid:
                    services[sid] = {"id": sid, "umbrella_name": UMBRELLA_NAME,
                                     "name": s.get("service_name") or _service_name(sid)}
        return sorted(services.values(), key=lambda s: s["name"].lower())

    def active_mode(self):
        return _mode(self.name)

    def is_test_mode(self):
        return self.active_mode() == "sandbox" or self._test_labels()

    def test_connection(self, mode=None, token=None):
        mode = mode if mode in BASE_URLS else self.active_mode()
        auth = _auth(self.name, mode)
        account = _request("GET", "/v1/account", auth).get("account") or {}
        balance = _request("GET", "/v1/balance", auth)
        balance = balance.get("account_balance") or balance.get("balance") or balance
        name = (account.get("customer_details") or {}).get("username") or self.setting(f"{mode}_account") or "account"
        self.set_account_name(mode, name)
        summary = f"Signed in as {name}"
        try:
            summary += f" · balance ${float(balance.get('amount_available')):,.2f}"
        except (TypeError, ValueError, AttributeError):
            pass
        if self._test_labels():
            summary += " — test labels ON (no charge)"
        return {"ok": True, "account": summary}

    def descriptor(self):
        return {
            "name": self.name,
            "key": self.name,
            "platform": self.platform,
            "platform_label": self.platform_label,
            "label": self.label,
            "enabled": self.setting("enabled") == "true",
            "enabled_key": self.setting_key("enabled"),
            "mode_key": self.setting_key("mode"),
            "mode": self.active_mode(),
            "modes": [
                {"value": "sandbox", "label": "Test (Stamps.com staging)"},
                {"value": "production", "label": "Production (live — labels cost money)"},
            ],
            "fields": [
                {"key": self.setting_key("sandbox_client_id"), "label": "Staging client ID",
                 "type": "text", "mode": "sandbox"},
                {"key": self.setting_key("sandbox_client_secret"), "label": "Staging client secret",
                 "type": "secret", "mode": "sandbox"},
                {"key": self.setting_key("production_client_id"), "label": "Production client ID",
                 "type": "text", "mode": "production"},
                {"key": self.setting_key("production_client_secret"), "label": "Production client secret",
                 "type": "secret", "mode": "production",
                 "hint": "Issued by Stamps.com developer support for this app's callback URL."},
                {"key": self.setting_key("label_format"), "label": "Label format", "type": "select",
                 "options": [
                     {"value": "pdf", "label": "PDF (4x6)"},
                     {"value": "zpl", "label": "ZPL"},
                     {"value": "png", "label": "PNG"},
                 ]},
                {"key": self.setting_key("test_labels"), "label": "Test labels", "type": "select",
                 "options": [
                     {"value": "false", "label": "Off — live labels (cost money)"},
                     {"value": "true", "label": "On — test labels, no charge (not valid for shipping)"},
                 ],
                 "hint": "Test labels print with a VOID mark. Staging labels are always free."},
            ],
            "oauth": {
                "service": "Stamps.com",
                "connect_label": "Connect Endicia account",
                "start_endpoint": f"/api/providers/{self.name}/oauth/start",
                "complete_endpoint": f"/api/providers/{self.name}/oauth/complete",
                "disconnect_endpoint": f"/api/providers/{self.name}/oauth/disconnect",
                "callback_path": CALLBACK_PATH,
                "accounts": self.signed_in_accounts(),
            },
            "test_endpoint": f"/api/providers/{self.name}/test",
            "supports": {"service_exclusions": True},
            "services_endpoint": f"/api/providers/{self.name}/services",
            "excluded_endpoint": f"/api/providers/{self.name}/excluded-services",
            **origin_descriptor(self.name),
        }

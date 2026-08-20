"""ShipStation API v2 implementation of the ShippingProvider interface.

ShipStation's model (rate a shipment -> rates -> buy label) mirrors the other
platforms, with these differences the adapter absorbs:
  * one API key (`API-Key` header), no sandbox — but `POST /v2/labels` accepts
    `test_label: true` (no charge), exposed as a Settings toggle;
  * rating needs explicit `carrier_ids`, so the connected carriers are fetched
    (and cached briefly) and all of them are quoted;
  * native lb/in units — no conversion;
  * rate ids are per request, so a stable "carrier_id:service_code" id
    intersects services across boxes; the label is bought with the shipment
    inline (the only purchase path that honors `test_label`) and tagged with
    the draft shipment id as `external_shipment_id`, which is what makes buying
    idempotent: every purchase is guarded by `GET /v2/labels?external_shipment_id=`.

One ShipStation shipment per box (mirrors the other providers) so the
group/box machinery is reused unchanged.
"""
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial

import requests

import db
from providers import labels
from providers.base import (
    DraftShipment,
    LabelStatus,
    ProviderError,
    Rate,
    ShipmentState,
    ShippingProvider,
)

BASE_URL = "https://api.shipstation.com"
EXCLUDED_KEY = "shipstation_excluded_service_ids"
MASK = "••••••••"
LABEL_FORMATS = ("pdf", "zpl", "png")
CONFIRMATION = {"adult": "adult_signature", "signature": "signature"}
CARRIER_CACHE_TTL = 600
AMOUNT_FIELDS = ("shipping_amount", "other_amount", "insurance_amount", "confirmation_amount", "tax_amount")
ADDRESS_FIELDS = (
    "name", "phone", "email", "company_name", "address_line1", "address_line2", "address_line3",
    "city_locality", "state_province", "postal_code", "country_code", "address_residential_indicator",
)
PACKAGE_FIELDS = ("package_code", "weight", "dimensions", "insured_value", "label_messages")

ORIGIN_REQUIRED = {
    "origin_company": "Company",
    "origin_address1": "Address 1",
    "origin_city": "City",
    "origin_state": "State",
    "origin_zip": "ZIP",
    "origin_phone": "Phone",
    "origin_email": "Email",
}

_carrier_cache = {}
_carrier_lock = threading.Lock()


def _token():
    token = db.get_setting("shipstation_api_key")
    if not token:
        raise ProviderError("No ShipStation API key configured — set it in Settings")
    return token


def _auth():
    """(base_url, token) captured in the request context so parallel worker
    threads — which have no Flask context — can still authenticate."""
    return BASE_URL, _token()


def _label_format():
    val = (db.get_setting("shipstation_label_format") or "pdf").lower()
    return val if val in LABEL_FORMATS else "pdf"


def _test_labels():
    return db.get_setting("shipstation_test_labels") == "true"


def _extract_error(resp):
    try:
        data = resp.json()
    except ValueError:
        return f"ShipStation error ({resp.status_code}): {(resp.text or '')[:300]}"
    errors = data.get("errors") if isinstance(data, dict) else None
    parts = []
    for err in errors or []:
        if not isinstance(err, dict):
            continue
        message = err.get("message") or ""
        field = err.get("field_name")
        code = err.get("error_code")
        text = f"{field}: {message}" if field and message else message
        if code and code not in ("unspecified",):
            text = f"{code}: {text}" if text else code
        if text:
            parts.append(text)
    if parts:
        return f"ShipStation error ({resp.status_code}): " + " | ".join(parts)
    if isinstance(data, dict) and data.get("message"):
        return f"ShipStation error ({resp.status_code}): {data['message']}"
    return f"ShipStation error ({resp.status_code}): {str(data)[:300]}"


def _retry_after(resp, attempt):
    try:
        wait = float(resp.headers.get("Retry-After") or 0)
    except ValueError:
        wait = 0
    return min(wait if wait > 0 else 1.5 * (attempt + 1), 30)


def _request(method, path, json_body=None, params=None, timeout=45, auth=None):
    base_url, token = auth or _auth()
    url = f"{base_url}{path}"
    # GETs are idempotent, so ride through transient gateway timeouts / 5xx.
    # Label purchase and void writes are NEVER auto-retried — the buy loop
    # re-issues those only after the external_shipment_id guard finds no label.
    retry_recoverable = method.upper() == "GET"
    resp = None
    last_exc = None
    for attempt in range(4):
        try:
            resp = requests.request(
                method, url, json=json_body, params=params,
                headers={"API-Key": token, "Content-Type": "application/json"},
                timeout=timeout,
            )
        except requests.RequestException as e:
            if retry_recoverable and attempt < 3:
                last_exc = e
                time.sleep(min(1.5 * (attempt + 1), 10))
                continue
            raise ProviderError(f"ShipStation request failed: {e}", status=None)
        if resp.status_code == 429 and attempt < 3:
            time.sleep(_retry_after(resp, attempt))
            continue
        if retry_recoverable and resp.status_code >= 500 and attempt < 3:
            time.sleep(min(1.5 * (attempt + 1), 10))
            continue
        break
    if resp is None:
        raise ProviderError(f"ShipStation request failed: {last_exc}", status=None)
    if resp.status_code >= 400:
        raise ProviderError(_extract_error(resp), status=resp.status_code)
    if resp.status_code == 204 or not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


def _compact(address):
    """Drop empty values — ShipStation rejects blank strings for several address fields."""
    return {k: v for k, v in address.items() if v not in (None, "")}


def _origin_address():
    missing = [label for key, label in ORIGIN_REQUIRED.items() if not (db.get_setting(key) or "").strip()]
    if missing:
        raise ProviderError(
            "Origin address is incomplete — fill in on the Settings page: " + ", ".join(missing)
        )
    company = db.get_setting("origin_company") or ""
    return _compact({
        "name": db.get_setting("origin_contact") or company or "Shipping",
        "company_name": company,
        "address_line1": db.get_setting("origin_address1"),
        "address_line2": db.get_setting("origin_address2") or "",
        "city_locality": db.get_setting("origin_city"),
        "state_province": db.get_setting("origin_state"),
        "postal_code": db.get_setting("origin_zip"),
        "country_code": "US",
        "phone": db.get_setting("origin_phone") or "",
        "email": db.get_setting("origin_email") or "",
    })


def _dest_address(dest):
    email = (dest.get("email") or "").strip() or (db.get_setting("origin_email") or "").strip()
    return _compact({
        "name": dest.get("contact") or dest.get("company") or "Recipient",
        "company_name": dest.get("company") or "",
        "address_line1": dest.get("address1"),
        "address_line2": dest.get("address2") or "",
        "city_locality": dest.get("city"),
        "state_province": (dest.get("state") or "").strip().upper(),
        "postal_code": (dest.get("zip") or "").strip(),
        "country_code": dest.get("country") or "US",
        "phone": dest.get("phone") or "",
        "email": email,
        "address_residential_indicator": "unknown",
    })


def _build_package(p):
    def dim(v):
        try:
            f = float(str(v or "").strip())
        except ValueError:
            f = 0.0
        return f if f > 0 else 1.0
    try:
        weight_lb = float(str(p.get("weight") or "0").strip())
    except ValueError:
        weight_lb = 0.0
    return {
        "weight": {"value": round(weight_lb, 3), "unit": "pound"},
        "dimensions": {
            "unit": "inch",
            "length": dim(p.get("length")),
            "width": dim(p.get("width")),
            "height": dim(p.get("height")),
        },
    }


def _service_id(rate):
    return f"{rate.get('carrier_id') or ''}:{rate.get('service_code') or ''}"


def _split_service_id(service_id):
    """'se-123:usps_priority_mail' -> ('se-123', 'usps_priority_mail')."""
    carrier_id, _, service_code = (service_id or "").partition(":")
    if not carrier_id or not service_code:
        raise ProviderError(f"Invalid ShipStation service id '{service_id}'")
    return carrier_id, service_code


def _amount(value):
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _rate_total(rate):
    return round(sum(_amount(rate.get(f)) for f in AMOUNT_FIELDS), 2)


def _usable(rate):
    if rate.get("error_messages"):
        return False
    if (rate.get("validation_status") or "").lower() == "invalid":
        return False
    return bool(rate.get("carrier_id") and rate.get("service_code"))


def _fetch_carriers(auth):
    """Every connected carrier (with services) across pages."""
    carriers = []
    page = 1
    while page <= 20:  # safety cap
        data = _request("GET", "/v2/carriers", params={"page": page, "page_size": 100}, auth=auth)
        batch = data.get("carriers") or []
        carriers.extend(c for c in batch if not c.get("disabled_by_billing_plan"))
        pages = data.get("pages") or 1
        if page >= pages or not batch:
            break
        page += 1
    return carriers


def _carriers(auth, force=False):
    """Connected carriers, cached per API key for CARRIER_CACHE_TTL seconds —
    rating needs the full carrier_id list on every request."""
    cache_key = hashlib.sha256(auth[1].encode()).hexdigest()
    now = time.monotonic()
    with _carrier_lock:
        hit = _carrier_cache.get(cache_key)
        if hit and not force and now - hit[0] < CARRIER_CACHE_TTL:
            return hit[1]
    carriers = _fetch_carriers(auth)
    with _carrier_lock:
        _carrier_cache[cache_key] = (now, carriers)
    return carriers


def _carrier_names(carriers):
    """{carrier_id: umbrella name}; a carrier connected more than once gets its
    nickname appended so the accounts can be told apart."""
    by_code = {}
    for c in carriers:
        by_code.setdefault(c.get("carrier_code"), []).append(c)
    names = {}
    for c in carriers:
        name = c.get("friendly_name") or c.get("carrier_code") or c.get("carrier_id") or ""
        nickname = (c.get("nickname") or "").strip()
        if len(by_code.get(c.get("carrier_code"), [])) > 1 and nickname and nickname != name:
            name = f"{name} · {nickname}"
        names[c.get("carrier_id")] = name
    return names


def _service_catalog(carriers):
    """{(carrier_id, service_code): service display name}."""
    out = {}
    for c in carriers:
        for s in c.get("services") or []:
            if s.get("service_code"):
                out[(c.get("carrier_id"), s["service_code"])] = s.get("name") or s["service_code"]
    return out


def _cheapest_by_service(rates):
    """The cheapest usable rate per carrier:service — deterministic when a box
    returns the same service more than once (e.g. package types)."""
    out = {}
    for r in rates or []:
        if not _usable(r):
            continue
        sid = _service_id(r)
        if sid not in out or _rate_total(r) < _rate_total(out[sid]):
            out[sid] = r
    return out


def _combine_rates(per_box_rates, catalog=None, carrier_names=None):
    """One quote list across per-box rate lists: only services every box can
    serve, price = sum across boxes. The cheapest combined rate is flagged as
    best value (ShipStation has no best-value attribute of its own)."""
    catalog = catalog or {}
    carrier_names = carrier_names or {}
    per_box = [_cheapest_by_service(rs) for rs in per_box_rates]
    if not per_box or any(not m for m in per_box):
        return []
    common = set(per_box[0])
    for m in per_box[1:]:
        common &= set(m)
    combined = []
    for sid in common:
        rs = [m[sid] for m in per_box]
        first = rs[0]
        key = (first.get("carrier_id"), first.get("service_code"))
        days = max((int(r.get("delivery_days") or 0) for r in rs), default=0) or None
        combined.append(Rate(
            provider="shipstation",
            provider_service_id=sid,
            courier_name=catalog.get(key) or first.get("service_type") or first.get("service_code") or sid,
            umbrella_name=(carrier_names.get(first.get("carrier_id"))
                           or first.get("carrier_friendly_name") or first.get("carrier_code") or ""),
            total_charge=round(sum(_rate_total(r) for r in rs), 2),
            currency=((first.get("shipping_amount") or {}).get("currency") or "USD").upper(),
            min_delivery_time=days,
            max_delivery_time=days,
            value_for_money_rank=None,
        ))
    combined.sort(key=lambda r: r.total_charge)
    if combined:
        combined[0].value_for_money_rank = 1
    return combined


def _label_status(label):
    status = (label.get("status") or "").lower()
    if status == "completed":
        return LabelStatus.READY
    if status == "processing":
        return LabelStatus.PENDING
    if status == "error":
        return LabelStatus.FAILED
    return LabelStatus.NOT_CREATED


def _to_state(label, catalog=None, carrier_names=None, error_message=None):
    catalog = catalog or {}
    carrier_names = carrier_names or {}
    status = _label_status(label)
    tracking = label.get("tracking_number")
    cost = _amount(label.get("shipment_cost")) + _amount(label.get("insurance_cost"))
    key = (label.get("carrier_id"), label.get("service_code"))
    return ShipmentState(
        provider_shipment_id=label.get("label_id"),
        label_status=status,
        tracking_numbers=[tracking] if tracking else [],
        courier_name=catalog.get(key) or label.get("service_code"),
        courier_umbrella_name=carrier_names.get(label.get("carrier_id")) or label.get("carrier_code"),
        cost=round(cost, 2) if label.get("shipment_cost") is not None else None,
        error_message=(error_message or "Label rejected by ShipStation") if status == LabelStatus.FAILED else None,
        raw=label,
    )


def _failed_state(sid, error):
    """A deterministic purchase rejection (4xx) as a FAILED state: the UI shows
    the reason and the buy loop stops re-issuing it every few seconds."""
    return ShipmentState(
        provider_shipment_id=None,
        label_status=LabelStatus.FAILED,
        error_message=str(error),
        raw={"draft_shipment_id": sid},
    )


def _map_parallel(items, fn):
    """Run fn(item) across items, capturing ProviderError per item."""
    out = {}
    with ThreadPoolExecutor(max_workers=min(len(items), 6)) as pool:
        futures = {pool.submit(fn, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                out[item] = future.result()
            except ProviderError as e:
                out[item] = e
    return out


def _inline_shipment(draft, carrier_id, service_code, external_shipment_id, ship_from=None):
    """Rebuild a purchasable shipment from a draft fetched via GET /v2/shipments:
    only whitelisted fields so read-only/draft-only properties never leak into
    the label request. `ship_from` is the origin to use when the draft carries
    none (e.g. it was rated against a warehouse)."""
    def pick(obj, fields):
        return {k: v for k, v in (obj or {}).items() if k in fields and v not in (None, "")}
    packages = [pick(p, PACKAGE_FIELDS) for p in draft.get("packages") or []]
    origin = pick(draft.get("ship_from"), ADDRESS_FIELDS) or dict(ship_from or {})
    if not packages or not draft.get("ship_to") or not origin:
        raise ProviderError("ShipStation draft shipment is missing address or package details")
    body = {
        "carrier_id": carrier_id,
        "service_code": service_code,
        "external_shipment_id": external_shipment_id,
        "ship_to": pick(draft.get("ship_to"), ADDRESS_FIELDS),
        "ship_from": origin,
        "packages": packages,
    }
    confirmation = draft.get("confirmation")
    if confirmation and confirmation != "none":
        body["confirmation"] = confirmation
    return body


class ShipStationProvider(ShippingProvider):
    name = "shipstation"
    label = "ShipStation"
    modes = ()

    # ---- rating / drafting ----
    def create_draft_shipments(self, destination, parcels, items, options=None):
        auth = _auth()
        carriers = _carriers(auth)
        carrier_ids = [c["carrier_id"] for c in carriers if c.get("carrier_id")]
        if not carrier_ids:
            raise ProviderError("No carriers are connected to this ShipStation account")
        ship_from = _origin_address()
        ship_to = _dest_address(destination)
        confirmation = CONFIRMATION.get((options or {}).get("signature") or "none")
        bodies = []
        for p in parcels:
            shipment = {
                "validate_address": "no_validation",
                "ship_to": ship_to,
                "ship_from": ship_from,
                "packages": [_build_package(p)],
            }
            if confirmation:
                shipment["confirmation"] = confirmation
            bodies.append({"shipment": shipment, "rate_options": {"carrier_ids": carrier_ids}})

        responses = [None] * len(bodies)
        if len(bodies) == 1:
            responses[0] = _request("POST", "/v2/rates", json_body=bodies[0], timeout=60, auth=auth)
        else:
            with ThreadPoolExecutor(max_workers=min(len(bodies), 6)) as pool:
                futures = {
                    pool.submit(partial(_request, "POST", "/v2/rates",
                                        json_body=body, timeout=60, auth=auth)): i
                    for i, body in enumerate(bodies)
                }
                errors = []
                for future in as_completed(futures):
                    i = futures[future]
                    try:
                        responses[i] = future.result()
                    except ProviderError as e:
                        errors.append(e)
                # Unbought ShipStation shipments aren't charged, so orphans are harmless.
                if errors:
                    raise errors[0]

        per_box_rates = []
        for resp in responses:
            rate_response = resp.get("rate_response") or {}
            rates = rate_response.get("rates") or []
            if not rates and rate_response.get("errors"):
                msgs = [e.get("message") for e in rate_response["errors"] if isinstance(e, dict) and e.get("message")]
                raise ProviderError("ShipStation rating failed: " + (" | ".join(msgs) or "no rates"))
            per_box_rates.append(rates)
        drafts = [DraftShipment(r.get("shipment_id")) for r in responses]
        if any(not d.provider_shipment_id for d in drafts):
            raise ProviderError("ShipStation did not return a shipment id for every box")
        return drafts, _combine_rates(per_box_rates, _service_catalog(carriers), _carrier_names(carriers)), []

    def get_excluded_service_ids(self):
        raw = db.get_setting(EXCLUDED_KEY)
        if not raw:
            return set()
        try:
            return {str(i) for i in json.loads(raw) if i}
        except (ValueError, TypeError):
            return set()

    def set_excluded_service_ids(self, ids):
        clean = sorted({str(i) for i in ids if i})
        db.set_setting(EXCLUDED_KEY, json.dumps(clean))
        return clean

    # ---- label lifecycle ----
    def _existing_label(self, sid, auth):
        """The newest non-voided label tagged with this draft's id, or None.
        Reusing it (instead of POSTing again) is what makes buying idempotent
        and prevents a lost-response re-buy from double-charging."""
        data = _request("GET", "/v2/labels",
                        params={"external_shipment_id": sid, "page_size": 25}, auth=auth)
        usable = [l for l in data.get("labels") or []
                  if (l.get("status") or "").lower() in ("completed", "processing", "error")
                  and not l.get("voided")]
        if not usable:
            return None
        usable.sort(key=lambda l: l.get("created_at") or "", reverse=True)
        label = usable[0]
        if (label.get("status") or "").lower() == "processing" and label.get("label_id"):
            label = _request("GET", f"/v2/labels/{label['label_id']}", auth=auth)
        return label

    def buy_labels(self, provider_shipment_ids, service_id):
        auth = _auth()
        carrier_id, service_code = _split_service_id(service_id)
        label_format = _label_format()
        test_label = _test_labels()
        carriers = _carriers(auth)
        catalog, names = _service_catalog(carriers), _carrier_names(carriers)
        origin = _origin_address()  # settings are read here, not in worker threads

        def work(sid):
            existing = self._existing_label(sid, auth)
            if existing is not None:
                return _to_state(existing, catalog, names)
            draft = _request("GET", f"/v2/shipments/{sid}", auth=auth)
            body = {
                "shipment": _inline_shipment(draft, carrier_id, service_code, sid, origin),
                "test_label": test_label,
                "validate_address": "no_validation",
                "label_format": label_format,
                "label_layout": "4x6",
                "label_download_type": "url",
            }
            try:
                label = _request("POST", "/v2/labels", json_body=body, timeout=90, auth=auth)
            except ProviderError as e:
                if e.recoverable:
                    raise
                return _failed_state(sid, e)
            return _to_state(label, catalog, names)

        return _map_parallel(list(provider_shipment_ids), work)

    def poll_shipments(self, provider_shipment_ids, service_id=None):
        auth = _auth()
        carriers = _carriers(auth)
        catalog, names = _service_catalog(carriers), _carrier_names(carriers)

        def work(sid):
            label = self._existing_label(sid, auth)
            if label is None:
                return ShipmentState(provider_shipment_id=sid,
                                     label_status=LabelStatus.NOT_CREATED, raw={})
            return _to_state(label, catalog, names)

        return _map_parallel(list(provider_shipment_ids), work)

    def fetch_labels(self, state):
        download = (state.raw or {}).get("label_download") or {}
        fmt = _label_format()
        url = download.get(fmt) or download.get("pdf") or download.get("href")
        if not url:
            return []
        resp = requests.get(url, timeout=30)
        if not resp.ok:
            return []
        data = resp.content
        return [(data, labels.sniff_label_format(data, fmt))]

    def cancel_all(self, provider_shipment_ids):
        """Ids are label ids after a purchase (void) or draft shipment ids before
        one (cancel) — both are `se-…`, so try the void first and fall back."""
        errors = []
        for sid in [i for i in provider_shipment_ids if i]:
            try:
                result = _request("PUT", f"/v2/labels/{sid}/void")
                if result.get("approved") is False:
                    msg = (result.get("message") or "").lower()
                    if "already" not in msg:
                        errors.append(f"{sid}: void rejected — {result.get('message') or 'no reason given'}")
                continue
            except ProviderError as e:
                if e.status != 404:
                    msg = str(e).lower()
                    if "already" in msg or "voided" in msg:
                        continue
                    errors.append(f"{sid}: {e}")
                    continue
            try:
                _request("PUT", f"/v2/shipments/{sid}/cancel")
            except ProviderError as e:
                # A never-created or already-cancelled draft — nothing to undo.
                if e.status in (404, 409):
                    continue
                errors.append(f"{sid}: {e}")
        return errors

    def get_raw_shipment(self, provider_shipment_id):
        try:
            return _request("GET", f"/v2/labels/{provider_shipment_id}")
        except ProviderError:
            return _request("GET", f"/v2/shipments/{provider_shipment_id}")

    # ---- settings surface ----
    def list_item_categories(self):
        return []

    def list_courier_services(self):
        carriers = _carriers(_auth(), force=True)
        names = _carrier_names(carriers)
        services = {}
        for c in carriers:
            for s in c.get("services") or []:
                code = s.get("service_code")
                if not code:
                    continue
                sid = f"{c.get('carrier_id')}:{code}"
                services[sid] = {
                    "id": sid,
                    "umbrella_name": names.get(c.get("carrier_id")) or "",
                    "name": s.get("name") or code,
                }
        return sorted(services.values(), key=lambda s: (s["umbrella_name"].lower(), s["name"].lower()))

    def active_mode(self):
        return ""

    def is_test_mode(self):
        return _test_labels()

    def test_connection(self, mode=None, token=None):
        if not token or token == MASK:
            token = db.get_setting("shipstation_api_key")
        if not token:
            raise ProviderError("No ShipStation API key configured")
        try:
            resp = requests.get(
                f"{BASE_URL}/v2/carriers",
                headers={"API-Key": token},
                params={"page_size": 50},
                timeout=15,
            )
        except requests.RequestException as e:
            raise ProviderError(f"Connection failed: {e}")
        if resp.status_code == 200:
            try:
                carriers = (resp.json() or {}).get("carriers") or []
            except ValueError:
                carriers = []
            names = sorted({c.get("friendly_name") or c.get("carrier_code") or "" for c in carriers} - {""})
            summary = f"{len(carriers)} carrier(s)" + (": " + ", ".join(names) if names else "")
            if _test_labels():
                summary += " — test labels ON (no charge)"
            return {"ok": True, "account": summary}
        if resp.status_code in (401, 403):
            raise ProviderError(f"API key rejected ({resp.status_code}) — check the key")
        raise ProviderError(f"ShipStation returned {resp.status_code}: {(resp.text or '')[:200]}")

    def descriptor(self):
        return {
            "name": self.name,
            "label": self.label,
            "enabled": db.get_setting("shipstation_enabled") == "true",
            "enabled_key": "shipstation_enabled",
            "modes": [],
            "fields": [
                {"key": "shipstation_api_key", "label": "API key (v2)", "type": "secret",
                 "hint": "ShipStation → Settings → Account → API Settings. Needs a Standard plan or higher."},
                {"key": "shipstation_label_format", "label": "Label format", "type": "select",
                 "options": [
                     {"value": "pdf", "label": "PDF (4x6)"},
                     {"value": "zpl", "label": "ZPL"},
                     {"value": "png", "label": "PNG"},
                 ]},
                {"key": "shipstation_test_labels", "label": "Test labels", "type": "select",
                 "options": [
                     {"value": "false", "label": "Off — live labels (cost money)"},
                     {"value": "true", "label": "On — test labels, no charge (not valid for shipping)"},
                 ],
                 "hint": "ShipStation has no sandbox; test labels are free but cannot be shipped. Shows the SANDBOX badge."},
            ],
            "test_endpoint": f"/api/providers/{self.name}/test",
            "supports": {"service_exclusions": True},
            "services_endpoint": f"/api/providers/{self.name}/services",
            "excluded_endpoint": f"/api/providers/{self.name}/excluded-services",
        }

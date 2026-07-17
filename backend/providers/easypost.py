"""EasyPost implementation of the ShippingProvider interface.

EasyPost's model mirrors Shippo (create shipment -> rates -> buy label) with a
few differences the adapter absorbs:
  * one API key whose prefix (EZTK.../EZAK...) sets the environment — no
    sandbox/production toggle;
  * HTTP Basic auth with the API key as the username and an empty password;
  * parcel weight is in OUNCES (the UI works in lb, so weight is converted);
  * rates ride on the shipment object itself (a rate id differs per box, so a
    stable "carrier:service" id intersects service levels across boxes);
  * the label lives on the shipment's postage_label; buying is NOT idempotent,
    so every purchase is guarded by a GET-before-buy check for an existing label.

One EasyPost shipment per box (mirrors Easyship/Shippo) so the group/box
machinery is reused unchanged.
"""
import json
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

BASE_URL = "https://api.easypost.com/v2"
EXCLUDED_KEY = "easypost_excluded_service_ids"
MASK = "••••••••"
LABEL_FORMATS = ("PDF", "ZPL", "PNG", "EPL2")

ORIGIN_REQUIRED = {
    "origin_company": "Company",
    "origin_address1": "Address 1",
    "origin_city": "City",
    "origin_state": "State",
    "origin_zip": "ZIP",
    "origin_phone": "Phone",
    "origin_email": "Email",
}


def _token():
    token = db.get_setting("easypost_token")
    if not token:
        raise ProviderError("No EasyPost API key configured — set it in Settings")
    return token


def _auth():
    """(base_url, token) captured in the request context so parallel worker
    threads — which have no Flask context — can still authenticate."""
    return BASE_URL, _token()


def _label_format():
    val = (db.get_setting("easypost_label_file_type") or "PDF").upper()
    return val if val in LABEL_FORMATS else "PDF"


def _extract_error(resp):
    try:
        data = resp.json()
    except ValueError:
        return f"EasyPost error ({resp.status_code}): {(resp.text or '')[:300]}"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, list):
            msg = "; ".join(str(m) for m in msg)
        details = []
        for sub in err.get("errors") or []:
            if isinstance(sub, dict):
                field = sub.get("field")
                message = sub.get("message")
                details.append(f"{field}: {message}" if field else str(message))
        suffix = (" | " + "; ".join(details)) if details else ""
        return f"EasyPost error ({resp.status_code}): {msg or 'request failed'}{suffix}"
    if isinstance(err, str):
        return f"EasyPost error ({resp.status_code}): {err}"
    return f"EasyPost error ({resp.status_code}): {str(data)[:300]}"


def _request(method, path, json_body=None, params=None, timeout=45, auth=None):
    base_url, token = auth or _auth()
    url = f"{base_url}{path}"
    # GETs are idempotent, so ride through transient gateway timeouts / 5xx.
    # Label purchase and refund writes are NEVER auto-retried — the buy loop
    # re-issues those only after a GET confirms no label exists yet.
    retry_recoverable = method.upper() == "GET"
    resp = None
    last_exc = None
    for attempt in range(4):
        try:
            resp = requests.request(
                method, url, json=json_body, params=params,
                auth=(token, ""),
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
        except requests.RequestException as e:
            if retry_recoverable and attempt < 3:
                last_exc = e
                time.sleep(min(1.5 * (attempt + 1), 10))
                continue
            raise ProviderError(f"EasyPost request failed: {e}", status=None)
        if resp.status_code == 429:
            time.sleep(min(1.5 * (attempt + 1), 15))
            continue
        if retry_recoverable and resp.status_code >= 500 and attempt < 3:
            time.sleep(min(1.5 * (attempt + 1), 10))
            continue
        break
    if resp is None:
        raise ProviderError(f"EasyPost request failed: {last_exc}", status=None)
    if resp.status_code >= 400:
        raise ProviderError(_extract_error(resp), status=resp.status_code)
    return resp.json() if resp.content else {}


def _origin_address():
    missing = [label for key, label in ORIGIN_REQUIRED.items() if not (db.get_setting(key) or "").strip()]
    if missing:
        raise ProviderError(
            "Origin address is incomplete — fill in on the Settings page: " + ", ".join(missing)
        )
    company = db.get_setting("origin_company") or ""
    return {
        "name": db.get_setting("origin_contact") or company or "Shipping",
        "company": company,
        "street1": db.get_setting("origin_address1"),
        "street2": db.get_setting("origin_address2") or "",
        "city": db.get_setting("origin_city"),
        "state": db.get_setting("origin_state"),
        "zip": db.get_setting("origin_zip"),
        "country": "US",
        "phone": db.get_setting("origin_phone") or "",
        "email": db.get_setting("origin_email") or "",
    }


def _dest_address(dest):
    email = (dest.get("email") or "").strip() or (db.get_setting("origin_email") or "").strip()
    return {
        "name": dest.get("contact") or dest.get("company") or "Recipient",
        "company": dest.get("company") or "",
        "street1": dest.get("address1"),
        "street2": dest.get("address2") or "",
        "city": dest.get("city"),
        "state": (dest.get("state") or "").strip().upper(),
        "zip": (dest.get("zip") or "").strip(),
        "country": dest.get("country") or "US",
        "phone": dest.get("phone") or "",
        "email": email,
    }


def _build_parcel(p):
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
        "length": dim(p.get("length")),
        "width": dim(p.get("width")),
        "height": dim(p.get("height")),
        "weight": round(weight_lb * 16, 2),  # EasyPost parcel weight is in ounces
    }


def _service_id(rate):
    return f"{rate.get('carrier') or ''}:{rate.get('service') or ''}"


def _rate_days(rate):
    return rate.get("delivery_days") or rate.get("est_delivery_days") or 0


def _cheapest_by_service(shipment):
    """The cheapest rate per carrier:service — a box can return the same service
    from more than one rate, so pick deterministically."""
    out = {}
    for r in shipment.get("rates") or []:
        if not r.get("carrier") or not r.get("service"):
            continue
        sid = _service_id(r)
        if sid not in out or float(r.get("rate") or 0) < float(out[sid].get("rate") or 0):
            out[sid] = r
    return out


def _combine_rates(shipments):
    """One quote list across per-box shipments: only services every box can
    serve, price = sum across boxes. The cheapest combined rate is flagged as
    best value (EasyPost has no best-value attribute of its own)."""
    per_box = [_cheapest_by_service(s) for s in shipments]
    if not per_box or any(not m for m in per_box):
        return []
    common = set(per_box[0])
    for m in per_box[1:]:
        common &= set(m)
    combined = []
    for sid in common:
        rs = [m[sid] for m in per_box]
        first = rs[0]
        days = max((_rate_days(r) for r in rs), default=0) or None
        combined.append(Rate(
            provider="easypost",
            provider_service_id=sid,
            courier_name=f"{first.get('carrier') or ''} {first.get('service') or ''}".strip() or sid,
            umbrella_name=first.get("carrier") or "",
            total_charge=round(sum(float(r.get("rate") or 0) for r in rs), 2),
            currency=first.get("currency") or "USD",
            min_delivery_time=days,
            max_delivery_time=days,
            value_for_money_rank=None,
        ))
    combined.sort(key=lambda r: r.total_charge)
    if combined:
        combined[0].value_for_money_rank = 1
    return combined


def _label_status(shipment):
    if (shipment.get("postage_label") or {}).get("label_url") or shipment.get("tracking_code"):
        return LabelStatus.READY
    if (shipment.get("status") or "").lower() in ("failure", "error"):
        return LabelStatus.FAILED
    return LabelStatus.NOT_CREATED


def _to_state(shipment, rate=None):
    selected = shipment.get("selected_rate") or rate or {}
    tracking = shipment.get("tracking_code")
    amount = selected.get("rate")
    carrier = selected.get("carrier")
    service = selected.get("service")
    return ShipmentState(
        provider_shipment_id=shipment.get("id"),
        label_status=_label_status(shipment),
        tracking_numbers=[tracking] if tracking else [],
        courier_name=(f"{carrier} {service}".strip() if carrier else None),
        courier_umbrella_name=carrier,
        cost=float(amount) if amount else None,
        raw=shipment,
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


class EasyPostProvider(ShippingProvider):
    name = "easypost"
    label = "EasyPost"
    modes = ()

    # ---- rating / drafting ----
    def create_draft_shipments(self, destination, parcels, items):
        auth = _auth()
        label_format = _label_format()
        address_from = _origin_address()
        address_to = _dest_address(destination)
        bodies = [
            {"shipment": {
                "from_address": address_from,
                "to_address": address_to,
                "parcel": _build_parcel(p),
                # label_size 4x6 is required for a 4x6 PDF — without it USPS PDFs
                # render as a full 8.5x11 page. ZPL/EPL2 are 4x6 regardless.
                "options": {"label_format": label_format, "label_size": "4x6"},
            }}
            for p in parcels
        ]
        shipments = [None] * len(bodies)
        if len(bodies) == 1:
            shipments[0] = _request("POST", "/shipments", json_body=bodies[0], timeout=60, auth=auth)
        else:
            with ThreadPoolExecutor(max_workers=min(len(bodies), 6)) as pool:
                futures = {
                    pool.submit(partial(_request, "POST", "/shipments",
                                        json_body=body, timeout=60, auth=auth)): i
                    for i, body in enumerate(bodies)
                }
                errors = []
                for future in as_completed(futures):
                    i = futures[future]
                    try:
                        shipments[i] = future.result()
                    except ProviderError as e:
                        errors.append(e)
                # Unbought EasyPost shipments aren't charged, so orphans are harmless.
                if errors:
                    raise errors[0]
        drafts = [DraftShipment(s["id"]) for s in shipments]
        return drafts, _combine_rates(shipments)

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
    def buy_labels(self, provider_shipment_ids, service_id):
        auth = _auth()

        def work(shipment_id):
            shipment = _request("GET", f"/shipments/{shipment_id}", auth=auth)
            # Idempotency guard: a label already on the shipment means a prior buy
            # landed (even if its response was lost) — never buy a second time.
            if _label_status(shipment) == LabelStatus.READY:
                return _to_state(shipment)
            rate = self._resolve_rate(shipment, service_id)
            bought = _request(
                "POST", f"/shipments/{shipment_id}/buy",
                json_body={"rate": {"id": rate["id"]}}, timeout=90, auth=auth,
            )
            return _to_state(bought, rate)

        return _map_parallel(list(provider_shipment_ids), work)

    def poll_shipments(self, provider_shipment_ids, service_id=None):
        auth = _auth()

        def work(shipment_id):
            shipment = _request("GET", f"/shipments/{shipment_id}", auth=auth)
            rate = None
            if not shipment.get("selected_rate") and service_id:
                try:
                    rate = self._resolve_rate(shipment, service_id)
                except ProviderError:
                    rate = None
            return _to_state(shipment, rate)

        return _map_parallel(list(provider_shipment_ids), work)

    def _resolve_rate(self, shipment, service_id):
        rates = shipment.get("rates") or []
        matches = [r for r in rates
                   if service_id is None or _service_id(r) == service_id]
        if not matches:
            raise ProviderError(f"Rate '{service_id}' is no longer available for this shipment")
        return min(matches, key=lambda r: float(r.get("rate") or 0))

    def fetch_labels(self, state):
        pl = (state.raw or {}).get("postage_label") or {}
        by_format = {
            "PDF": pl.get("label_pdf_url"),
            "ZPL": pl.get("label_zpl_url"),
            "EPL2": pl.get("label_epl2_url"),
            "PNG": pl.get("label_url"),
        }
        url = by_format.get(_label_format()) or pl.get("label_url") or pl.get("label_pdf_url")
        if not url:
            return []
        resp = requests.get(url, timeout=30)
        if not resp.ok:
            return []
        data = resp.content
        return [(data, labels.sniff_label_format(data, "pdf"))]

    def cancel_all(self, provider_shipment_ids):
        errors = []
        for sid in [i for i in provider_shipment_ids if i]:
            try:
                result = _request("POST", f"/shipments/{sid}/refund")
                status = (result.get("refund_status") or "").lower()
                if status in ("rejected", "not_applicable"):
                    errors.append(f"{sid}: refund {status}")
            except ProviderError as e:
                msg = str(e).lower()
                # A never-bought shipment id, or an already-refunded label — nothing to undo.
                if "not found" in msg or "already" in msg or "not eligible" in msg or "no postage" in msg:
                    continue
                errors.append(f"{sid}: {e}")
        return errors

    def get_raw_shipment(self, provider_shipment_id):
        return _request("GET", f"/shipments/{provider_shipment_id}")

    # ---- settings surface ----
    def list_item_categories(self):
        return []

    def list_courier_services(self):
        # Service exclusions are deferred for EasyPost (no clean service catalog
        # endpoint), so the settings UI shows no per-service list.
        return []

    def active_mode(self):
        return ""

    def is_test_mode(self):
        return (db.get_setting("easypost_token") or "").startswith("EZTK")

    def test_connection(self, mode=None, token=None):
        if not token or token == MASK:
            token = db.get_setting("easypost_token")
        if not token:
            raise ProviderError("No EasyPost API key configured")
        try:
            resp = requests.get(
                f"{BASE_URL}/shipments",
                auth=(token, ""),
                params={"page_size": 1},
                timeout=15,
            )
        except requests.RequestException as e:
            raise ProviderError(f"Connection failed: {e}")
        if resp.status_code == 200:
            env = "test" if token.startswith("EZTK") else "live"
            return {"ok": True, "account": f"connected ({env})"}
        if resp.status_code in (401, 403):
            raise ProviderError(f"API key rejected ({resp.status_code}) — check the key")
        raise ProviderError(f"EasyPost returned {resp.status_code}: {(resp.text or '')[:200]}")

    def descriptor(self):
        return {
            "name": self.name,
            "label": self.label,
            "enabled": db.get_setting("easypost_enabled") == "true",
            "enabled_key": "easypost_enabled",
            "modes": [],
            "fields": [
                {"key": "easypost_token", "label": "API key", "type": "secret"},
                {"key": "easypost_label_file_type", "label": "Label format", "type": "select",
                 "options": [
                     {"value": "PDF", "label": "PDF (4x6)"},
                     {"value": "ZPL", "label": "ZPL"},
                     {"value": "PNG", "label": "PNG"},
                 ]},
            ],
            "test_endpoint": f"/api/providers/{self.name}/test",
            "supports": {"service_exclusions": False},
        }

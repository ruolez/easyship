"""GoShippo implementation of the ShippingProvider interface.

Shippo's model mirrors Easyship (create shipment -> rates -> buy label) with
three differences the adapter absorbs:
  * one API token whose prefix (shippo_test_/shippo_live_) sets the environment
    — no sandbox/production toggle;
  * native lb/in units — no conversion;
  * the label lives on a separate Transaction object (buy by rate object_id,
    cancel via POST /refunds), and buying a rate is NOT idempotent, so every
    purchase is guarded by a check-before-buy against GET /transactions?rate=.

One Shippo shipment per box (mirrors Easyship) so the group/box machinery is
reused unchanged.
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

BASE_URL = "https://api.goshippo.com"
EXCLUDED_KEY = "shippo_excluded_service_ids"
MASK = "••••••••"

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
    token = db.get_setting("shippo_token")
    if not token:
        raise ProviderError("No Shippo token configured — set it in Settings")
    return token


def _auth():
    """(base_url, token) captured in the request context so parallel worker
    threads — which have no Flask context — can still authenticate."""
    return BASE_URL, _token()


def _extract_error(resp):
    try:
        data = resp.json()
    except ValueError:
        return f"Shippo error ({resp.status_code}): {(resp.text or '')[:300]}"
    if isinstance(data, dict):
        if data.get("detail"):
            return f"Shippo error ({resp.status_code}): {data['detail']}"
        parts = []
        for key, value in data.items():
            if isinstance(value, list):
                parts.append(f"{key}: {'; '.join(str(v) for v in value)}")
            else:
                parts.append(f"{key}: {value}")
        if parts:
            return f"Shippo error ({resp.status_code}): " + " | ".join(parts)
    return f"Shippo error ({resp.status_code}): {str(data)[:300]}"


def _request(method, path, json_body=None, params=None, timeout=45, auth=None):
    base_url, token = auth or _auth()
    url = f"{base_url}{path}"
    # GETs are idempotent, so ride through transient gateway timeouts / 5xx.
    # Label purchase and refund writes are NEVER auto-retried — the buy loop
    # re-issues those only after GET /transactions?rate confirms none exists.
    retry_recoverable = method.upper() == "GET"
    resp = None
    last_exc = None
    for attempt in range(4):
        try:
            resp = requests.request(
                method, url, json=json_body, params=params,
                headers={"Authorization": f"ShippoToken {token}", "Content-Type": "application/json"},
                timeout=timeout,
            )
        except requests.RequestException as e:
            if retry_recoverable and attempt < 3:
                last_exc = e
                time.sleep(min(1.5 * (attempt + 1), 10))
                continue
            raise ProviderError(f"Shippo request failed: {e}", status=None)
        if resp.status_code == 429:
            time.sleep(min(1.5 * (attempt + 1), 15))
            continue
        if retry_recoverable and resp.status_code >= 500 and attempt < 3:
            time.sleep(min(1.5 * (attempt + 1), 10))
            continue
        break
    if resp is None:
        raise ProviderError(f"Shippo request failed: {last_exc}", status=None)
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
        v = str(v or "").strip()
        return v if v else "1"
    return {
        "length": dim(p.get("length")),
        "width": dim(p.get("width")),
        "height": dim(p.get("height")),
        "distance_unit": "in",
        "weight": str(p.get("weight") or "0"),
        "mass_unit": "lb",
    }


def _cheapest_by_token(shipment):
    """The cheapest rate per servicelevel token — a box can return the same
    service from multiple carrier accounts, so pick deterministically."""
    out = {}
    for r in shipment.get("rates") or []:
        token = (r.get("servicelevel") or {}).get("token")
        if not token:
            continue
        if token not in out or float(r.get("amount") or 0) < float(out[token].get("amount") or 0):
            out[token] = r
    return out


def _combine_rates(shipments):
    """One quote list across per-box shipments: only service levels every box
    can serve, price = sum across boxes."""
    per_box = [_cheapest_by_token(s) for s in shipments]
    if not per_box or any(not m for m in per_box):
        # Some box returned no rates -> no service serves every box.
        if not per_box:
            return []
    common = set(per_box[0])
    for m in per_box[1:]:
        common &= set(m)
    combined = []
    for token in common:
        rs = [m[token] for m in per_box]
        sl = rs[0].get("servicelevel") or {}
        est = max((r.get("estimated_days") or 0) for r in rs) or None
        combined.append(Rate(
            provider="shippo",
            provider_service_id=token,
            courier_name=sl.get("display_name") or sl.get("name") or token,
            umbrella_name=rs[0].get("provider") or "",
            total_charge=round(sum(float(r.get("amount") or 0) for r in rs), 2),
            currency=rs[0].get("currency") or "USD",
            min_delivery_time=est,
            max_delivery_time=est,
            value_for_money_rank=1 if any("BESTVALUE" in (r.get("attributes") or []) for r in rs) else None,
        ))
    return sorted(combined, key=lambda r: r.total_charge)


def _label_status(txn):
    status = (txn.get("status") or "").upper()
    if status == "SUCCESS":
        return LabelStatus.READY
    if status == "QUEUED":
        return LabelStatus.PENDING
    if status == "ERROR":
        return LabelStatus.FAILED
    return LabelStatus.NOT_CREATED


def _to_state(txn, rate=None):
    sl = (rate or {}).get("servicelevel") or {}
    tracking = txn.get("tracking_number")
    amount = (rate or {}).get("amount")
    return ShipmentState(
        provider_shipment_id=txn.get("object_id"),
        label_status=_label_status(txn),
        tracking_numbers=[tracking] if tracking else [],
        courier_name=sl.get("display_name") or sl.get("name"),
        courier_umbrella_name=(rate or {}).get("provider"),
        cost=float(amount) if amount else None,
        raw=txn,
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


class ShippoProvider(ShippingProvider):
    name = "shippo"
    label = "GoShippo"
    modes = ()

    # ---- rating / drafting ----
    def create_draft_shipments(self, destination, parcels, items):
        auth = _auth()
        address_from = _origin_address()
        address_to = _dest_address(destination)
        bodies = [
            {"address_from": address_from, "address_to": address_to,
             "parcels": [_build_parcel(p)], "async": False}
            for p in parcels
        ]
        shipments = [None] * len(bodies)
        if len(bodies) == 1:
            shipments[0] = _request("POST", "/shipments/", json_body=bodies[0], timeout=60, auth=auth)
        else:
            with ThreadPoolExecutor(max_workers=min(len(bodies), 6)) as pool:
                futures = {
                    pool.submit(partial(_request, "POST", "/shipments/",
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
                # Unbought Shippo shipments aren't charged, so orphans are harmless.
                if errors:
                    raise errors[0]
        drafts = [DraftShipment(s["object_id"]) for s in shipments]
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
        label_type = db.get_setting("shippo_label_file_type") or "PDF_4x6"

        def work(shipment_id):
            rate = self._resolve_rate(shipment_id, service_id, auth)
            txn = self._existing_transaction(rate["object_id"], auth)
            if txn is None:
                txn = _request(
                    "POST", "/transactions/",
                    json_body={"rate": rate["object_id"], "label_file_type": label_type, "async": False},
                    timeout=90, auth=auth,
                )
            return _to_state(txn, rate)

        return _map_parallel(list(provider_shipment_ids), work)

    def poll_shipments(self, provider_shipment_ids, service_id=None):
        auth = _auth()

        def work(shipment_id):
            try:
                rate = self._resolve_rate(shipment_id, service_id, auth)
            except ProviderError:
                return ShipmentState(provider_shipment_id=shipment_id,
                                     label_status=LabelStatus.NOT_CREATED, raw={})
            txn = self._existing_transaction(rate["object_id"], auth, include_error=True)
            if txn is None:
                return ShipmentState(provider_shipment_id=shipment_id,
                                     label_status=LabelStatus.NOT_CREATED, raw={})
            return _to_state(txn, rate)

        return _map_parallel(list(provider_shipment_ids), work)

    def _resolve_rate(self, shipment_id, service_id, auth):
        rates = _request("GET", f"/shipments/{shipment_id}/rates/", auth=auth).get("results") or []
        matches = [r for r in rates
                   if service_id is None or (r.get("servicelevel") or {}).get("token") == service_id]
        if not matches:
            raise ProviderError(f"Rate '{service_id}' is no longer available for this shipment")
        return min(matches, key=lambda r: float(r.get("amount") or 0))

    def _existing_transaction(self, rate_id, auth, include_error=False):
        """The newest already-created transaction for a rate, or None. Reusing it
        (instead of POSTing again) is what makes buying idempotent and prevents a
        lost-response re-buy from double-charging."""
        results = _request("GET", "/transactions/", params={"rate": rate_id}, auth=auth).get("results") or []
        wanted = {"SUCCESS", "QUEUED", "ERROR"} if include_error else {"SUCCESS", "QUEUED"}
        usable = [t for t in results if (t.get("status") or "").upper() in wanted]
        if not usable:
            return None
        usable.sort(key=lambda t: t.get("object_created") or "", reverse=True)
        # The list payload may be partial — fetch the full transaction for label_url etc.
        return _request("GET", f"/transactions/{usable[0]['object_id']}/", auth=auth)

    def fetch_labels(self, state):
        url = (state.raw or {}).get("label_url")
        if not url:
            return []
        resp = requests.get(url, timeout=30)
        if not resp.ok:
            return []
        data = resp.content
        return [(data, labels.sniff_label_format(data, "pdf"))]

    def cancel_all(self, provider_shipment_ids):
        errors = []
        for tid in [i for i in provider_shipment_ids if i]:
            try:
                result = _request("POST", "/refunds/", json_body={"transaction": tid, "async": False})
                if (result.get("status") or "").upper() == "ERROR":
                    errors.append(f"{tid}: refund rejected")
            except ProviderError as e:
                msg = str(e).lower()
                # A never-bought shipment id, or an already-refunded label — nothing to undo.
                if "not found" in msg or "already" in msg or "not eligible" in msg:
                    continue
                errors.append(f"{tid}: {e}")
        return errors

    def get_raw_shipment(self, provider_shipment_id):
        try:
            return _request("GET", f"/transactions/{provider_shipment_id}/")
        except ProviderError:
            return _request("GET", f"/shipments/{provider_shipment_id}/")

    # ---- settings surface ----
    def list_item_categories(self):
        return []

    def list_courier_services(self):
        auth = _auth()
        services = {}
        page = 1
        while page <= 20:  # safety cap
            data = _request("GET", "/carrier_accounts/",
                            params={"service_levels": "true", "results": 100, "page": page}, auth=auth)
            accounts = data.get("results") or []
            for account in accounts:
                if not account.get("active"):
                    continue
                carrier = account.get("carrier") or ""
                for sl in account.get("service_levels") or []:
                    token = sl.get("token")
                    if token and token not in services:
                        services[token] = {
                            "id": token,
                            "umbrella_name": carrier,
                            "name": sl.get("name") or token,
                        }
            if not data.get("next") or not accounts:
                break
            page += 1
        return sorted(services.values(), key=lambda s: (s["umbrella_name"].lower(), s["name"].lower()))

    def active_mode(self):
        return ""

    def is_test_mode(self):
        return (db.get_setting("shippo_token") or "").startswith("shippo_test_")

    def test_connection(self, mode=None, token=None):
        if not token or token == MASK:
            token = db.get_setting("shippo_token")
        if not token:
            raise ProviderError("No Shippo token configured")
        try:
            resp = requests.get(
                f"{BASE_URL}/carrier_accounts/",
                headers={"Authorization": f"ShippoToken {token}"},
                params={"results": 1},
                timeout=15,
            )
        except requests.RequestException as e:
            raise ProviderError(f"Connection failed: {e}")
        if resp.status_code == 200:
            env = "test" if token.startswith("shippo_test_") else "live"
            return {"ok": True, "account": f"connected ({env})"}
        if resp.status_code in (401, 403):
            raise ProviderError(f"Token rejected ({resp.status_code}) — check the token")
        raise ProviderError(f"Shippo returned {resp.status_code}: {(resp.text or '')[:200]}")

    def descriptor(self):
        return {
            "name": self.name,
            "label": self.label,
            "enabled": db.get_setting("shippo_enabled") == "true",
            "enabled_key": "shippo_enabled",
            "modes": [],
            "fields": [
                {"key": "shippo_token", "label": "API token", "type": "secret"},
                {"key": "shippo_label_file_type", "label": "Label format", "type": "select",
                 "options": [
                     {"value": "PDF_4x6", "label": "PDF 4x6"},
                     {"value": "PNG", "label": "PNG"},
                     {"value": "ZPL", "label": "ZPL"},
                     {"value": "PDF", "label": "PDF (letter)"},
                 ]},
            ],
            "test_endpoint": f"/api/providers/{self.name}/test",
            "supports": {"service_exclusions": True},
            "services_endpoint": f"/api/providers/{self.name}/services",
            "excluded_endpoint": f"/api/providers/{self.name}/excluded-services",
        }

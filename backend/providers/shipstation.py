"""ShipStation API v2 implementation of the ShippingProvider interface.

ShipStation's model (rate a shipment -> rates -> buy label) mirrors the other
platforms, with these differences the adapter absorbs:
  * one API key (`API-Key` header), no sandbox — but `POST /v2/labels` accepts
    `test_label: true` (no charge), exposed as a Settings toggle;
  * rating needs explicit `carrier_ids`, so the connected carriers are fetched
    (and cached briefly) and all of them are quoted;
  * native lb/in units — no conversion;
  * rate ids are per request, so a stable "carrier_id:service_code" id names
    the chosen service; the label is bought with the shipment inline (the only
    purchase path that honors `test_label`) and tagged with the shipment id as
    `external_shipment_id`, which is what makes buying idempotent: every
    purchase is guarded by `GET /v2/labels?external_shipment_id=`.

Unlike the other providers, a multi-box order is ONE ShipStation shipment
with N packages — that's how the ShipStation site rates it, and it's the only
way multi-package discounts (single pickup fee, UPS/FedEx multi-piece and
hundredweight tiers) apply. The app's group/box machinery still wants one id
and one label per box, so the adapter hands out synthetic per-box ids
("<shipment_id>#<box>"): one purchase produces one label whose packages[]
carry per-box tracking numbers and label downloads.

Services that cannot rate several packages at once (every USPS service, UPS
SurePost — `is_multi_package_supported` in the carrier catalog) are rated one
box at a time and offered only when every box got them, priced as the sum;
buying one slices that box's package out of the shared draft, so each box
gets its own label, tagged "<shipment_id>#<box>" for idempotency.
"""
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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

BASE_URL = "https://api.shipstation.com"
PRIMARY_KEY = "shipstation"
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
PLAIN_PACKAGE = "package"

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


def _token(key=PRIMARY_KEY):
    token = db.get_setting(f"{key}_api_key")
    if not token:
        raise ProviderError("No ShipStation API key configured — set it in Settings")
    return token


def _auth(key=PRIMARY_KEY):
    """(base_url, token) captured in the request context so parallel worker
    threads — which have no Flask context — can still authenticate."""
    return BASE_URL, _token(key)


def _label_format(key=PRIMARY_KEY):
    val = (db.get_setting(f"{key}_label_format") or "pdf").lower()
    return val if val in LABEL_FORMATS else "pdf"


def _test_labels(key=PRIMARY_KEY):
    return db.get_setting(f"{key}_test_labels") == "true"


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


def _origin_address(origin):
    """ShipStation address from an `origin_settings()` dict."""
    missing = missing_origin_fields(origin, ORIGIN_REQUIRED)
    if missing:
        raise ProviderError(
            "Origin address is incomplete — fill in on the Settings page: " + ", ".join(missing)
        )
    company = origin.get("origin_company") or ""
    return _compact({
        "name": origin.get("origin_contact") or company or "Shipping",
        "company_name": company,
        "address_line1": origin.get("origin_address1"),
        "address_line2": origin.get("origin_address2") or "",
        "city_locality": origin.get("origin_city"),
        "state_province": origin.get("origin_state"),
        "postal_code": origin.get("origin_zip"),
        "country_code": "US",
        "phone": origin.get("origin_phone") or "",
        "email": origin.get("origin_email") or "",
    })


def _dest_address(dest, origin_email=""):
    email = (dest.get("email") or "").strip() or (origin_email or "").strip()
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
    """Weight in pounds, dimensions in inches — or no dimensions at all when any
    side is blank/0 (a 0×0×0 box): USPS then prices by weight and zone. A
    made-up side would have USPS quote a cubic tier the real box is never
    billed at."""
    try:
        weight_lb = float(str(p.get("weight") or "0").strip())
    except ValueError:
        weight_lb = 0.0
    package = {"weight": {"value": round(weight_lb, 3), "unit": "pound"}}
    dims = parcel_dimensions(p)
    if dims:
        package["dimensions"] = {"unit": "inch", **dims}
    return package


def _service_id(rate):
    return f"{rate.get('carrier_id') or ''}:{rate.get('service_code') or ''}"


def _split_service_id(service_id):
    """'se-123:usps_priority_mail' -> ('se-123', 'usps_priority_mail')."""
    carrier_id, _, service_code = (service_id or "").partition(":")
    if not carrier_id or not service_code:
        raise ProviderError(f"Invalid ShipStation service id '{service_id}'")
    return carrier_id, service_code


def _box_id(shipment_id, index, count):
    """The per-box draft id: the shipment id itself for a single box, or
    '<shipment_id>#<box>' when one multi-package shipment backs several boxes."""
    return shipment_id if count == 1 else f"{shipment_id}#{index + 1}"


def _split_box_id(box_id):
    """('se-123', 0) from 'se-123#1'; (id, None) for a plain single-box id."""
    base, _, idx = (box_id or "").partition("#")
    if not idx:
        return base, None
    try:
        return base, max(int(idx) - 1, 0)
    except ValueError:
        return base, None


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
    # USPS services come back once per package type (flat-rate envelope and
    # boxes included); the label is always bought as a plain package, so only
    # that price is honest.
    if (rate.get("package_type") or PLAIN_PACKAGE) != PLAIN_PACKAGE:
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


def _supports_multi_package(carriers, carrier_id, service_code):
    """Whether one label can carry several packages on this service: the
    service's own flag, else the carrier's, else assumed (the pre-flag behaviour)."""
    for c in carriers:
        if c.get("carrier_id") != carrier_id:
            continue
        for s in c.get("services") or []:
            if s.get("service_code") == service_code and s.get("is_multi_package_supported") is not None:
                return bool(s["is_multi_package_supported"])
        flag = c.get("has_multi_package_supporting_services")
        return True if flag is None else bool(flag)
    return True


def _quote(r, total, days, catalog, carrier_names, provider):
    key = (r.get("carrier_id"), r.get("service_code"))
    return Rate(
        provider=provider,
        provider_service_id=_service_id(r),
        courier_name=catalog.get(key) or r.get("service_type") or r.get("service_code") or _service_id(r),
        umbrella_name=(carrier_names.get(r.get("carrier_id"))
                       or r.get("carrier_friendly_name") or r.get("carrier_code") or ""),
        total_charge=total,
        currency=((r.get("shipping_amount") or {}).get("currency") or "USD").upper(),
        min_delivery_time=days,
        max_delivery_time=days,
        value_for_money_rank=None,
    )


def _rank(combined):
    """Cheapest first and flagged as best value (ShipStation has no best-value
    attribute of its own)."""
    combined.sort(key=lambda r: r.total_charge)
    for r in combined:
        r.value_for_money_rank = None
    if combined:
        combined[0].value_for_money_rank = 1
    return combined


def _combine_rates(rates, catalog=None, carrier_names=None, provider=PRIMARY_KEY):
    """Quotes for one shipment (which may carry several packages): the cheapest
    usable rate per service. `provider` is the instance key."""
    catalog = catalog or {}
    carrier_names = carrier_names or {}
    return _rank([
        _quote(r, _rate_total(r), int(r.get("delivery_days") or 0) or None, catalog, carrier_names, provider)
        for r in _cheapest_by_service(rates).values()
    ])


def _combine_per_box(rate_lists, catalog=None, carrier_names=None, provider=PRIMARY_KEY):
    """Quotes across one shipment per box: only services every box got, priced
    as the sum of the per-box labels, delivery as the slowest box."""
    catalog = catalog or {}
    carrier_names = carrier_names or {}
    per_box = [_cheapest_by_service(rs) for rs in rate_lists]
    if not per_box:
        return []
    common = set.intersection(*(set(m) for m in per_box))
    combined = []
    for sid in common:
        rs = [m[sid] for m in per_box]
        days = max(int(r.get("delivery_days") or 0) for r in rs) or None
        combined.append(_quote(rs[0], round(sum(_rate_total(r) for r in rs), 2), days,
                               catalog, carrier_names, provider))
    return _rank(combined)


def _label_status(label):
    status = (label.get("status") or "").lower()
    if status == "completed":
        return LabelStatus.READY
    if status == "processing":
        return LabelStatus.PENDING
    if status == "error":
        return LabelStatus.FAILED
    return LabelStatus.NOT_CREATED


def _to_state(label, catalog=None, carrier_names=None, error_message=None, box_id=None):
    """One box's view of a label. A multi-package label backs several boxes:
    the box's package supplies its tracking number and label download, and the
    shipment cost is split evenly across packages."""
    catalog = catalog or {}
    carrier_names = carrier_names or {}
    status = _label_status(label)
    tracking = label.get("tracking_number")
    cost = _amount(label.get("shipment_cost")) + _amount(label.get("insurance_cost"))
    raw = label
    packages = label.get("packages") or []
    _, idx = _split_box_id(box_id) if box_id else (None, None)
    if idx is not None and len(packages) > 1:
        pkg = packages[idx] if idx < len(packages) else {}
        tracking = pkg.get("tracking_number") or tracking
        cost = cost / len(packages)
        raw = {**label, "_package_index": idx}
    key = (label.get("carrier_id"), label.get("service_code"))
    return ShipmentState(
        provider_shipment_id=label.get("label_id"),
        label_status=status,
        tracking_numbers=[tracking] if tracking else [],
        courier_name=catalog.get(key) or label.get("service_code"),
        courier_umbrella_name=carrier_names.get(label.get("carrier_id")) or label.get("carrier_code"),
        cost=round(cost, 2) if label.get("shipment_cost") is not None else None,
        error_message=(error_message or "Label rejected by ShipStation") if status == LabelStatus.FAILED else None,
        raw=raw,
    )


def _download_url(raw, fmt):
    """The label file URL for one box: its own package's download on a
    multi-package label, the whole label's otherwise."""
    packages = raw.get("packages") or []
    idx = raw.get("_package_index")
    if idx is not None and len(packages) > 1:
        download = (packages[idx] if idx < len(packages) else {}).get("label_download") or {}
        # No per-package file -> nothing to print for this box; the whole-label
        # document would duplicate every box's pages.
        return download.get(fmt) or download.get("pdf") or download.get("href")
    download = raw.get("label_download") or {}
    return download.get(fmt) or download.get("pdf") or download.get("href")


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


def _inline_shipment(draft, carrier_id, service_code, external_shipment_id, ship_from=None,
                     package_index=None):
    """Rebuild a purchasable shipment from a draft fetched via GET /v2/shipments:
    only whitelisted fields so read-only/draft-only properties never leak into
    the label request. `ship_from` is the origin to use when the draft carries
    none (e.g. it was rated against a warehouse). `package_index` buys just
    that one package of a multi-package draft."""
    def pick(obj, fields):
        return {k: v for k, v in (obj or {}).items() if k in fields and v not in (None, "")}
    packages = [pick(p, PACKAGE_FIELDS) for p in draft.get("packages") or []]
    if package_index is not None:
        packages = packages[package_index:package_index + 1]
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
    platform = "shipstation"
    label = "ShipStation"
    modes = ()

    # ---- carrier hooks ----
    # A subclass locked to a subset of the account's carriers (see endicia.py)
    # overrides these; every carrier-list consumer below goes through them.
    def rating_carriers(self, carriers):
        """The connected carriers this instance quotes and lists services for."""
        return carriers

    def carrier_names(self, carriers):
        """{carrier_id: umbrella name} shown on rates and labels."""
        return _carrier_names(carriers)

    def list_carriers(self):
        """Every connected carrier as picker options [{value, label}]."""
        carriers = _carriers(_auth(self.name), force=True)
        names = _carrier_names(carriers)
        return [{"value": c["carrier_id"], "label": names.get(c["carrier_id"]) or c["carrier_id"]}
                for c in carriers if c.get("carrier_id")]

    # ---- rating / drafting ----
    def create_draft_shipments(self, destination, parcels, items, options=None):
        auth = _auth(self.name)
        carriers = self.rating_carriers(_carriers(auth))
        carrier_ids = [c["carrier_id"] for c in carriers if c.get("carrier_id")]
        if not carrier_ids:
            raise ProviderError("No carriers are connected to this ShipStation account")
        origin = self.origin()
        ship_from = _origin_address(origin)
        ship_to = _dest_address(destination, origin.get("origin_email"))
        confirmation = CONFIRMATION.get((options or {}).get("signature") or "none")
        catalog, names = _service_catalog(carriers), self.carrier_names(carriers)
        errors = []

        def rate_call(packages, ids):
            """(shipment_id, rates) for one POST /v2/rates; carrier refusals are
            collected so one carrier's objection cannot hide another's quotes."""
            shipment = {
                "validate_address": "no_validation",
                "ship_to": ship_to,
                "ship_from": ship_from,
                "packages": [_build_package(p) for p in packages],
            }
            if confirmation:
                shipment["confirmation"] = confirmation
            resp = _request("POST", "/v2/rates",
                            json_body={"shipment": shipment, "rate_options": {"carrier_ids": ids}},
                            timeout=60, auth=auth)
            rate_response = resp.get("rate_response") or {}
            errors.extend(e.get("message") for e in rate_response.get("errors") or []
                          if isinstance(e, dict) and e.get("message"))
            shipment_id = resp.get("shipment_id")
            if not shipment_id:
                raise ProviderError("ShipStation did not return a shipment id")
            return shipment_id, rate_response.get("rates") or []

        def multi_ok(quote):
            return _supports_multi_package(carriers, *_split_service_id(quote.provider_service_id))

        if len(parcels) == 1:
            shipment_id, rates = rate_call(parcels, carrier_ids)
            drafts = [DraftShipment(shipment_id)]
            combined = _combine_rates(rates, catalog, names, provider=self.name)
        else:
            # One shipment carrying every box: multi-package rating is how the
            # ShipStation site quotes, and the only way multi-package discounts
            # (single pickup fee, multi-piece/hundredweight tiers) apply.
            multi_ids = [c["carrier_id"] for c in carriers
                         if c.get("carrier_id") and _supports_multi_package(carriers, c["carrier_id"], None)]
            # Carriers with a service that rates one package at a time (USPS,
            # UPS SurePost) are rated once per box as well.
            single_ids = [c["carrier_id"] for c in carriers if c.get("carrier_id") and any(
                not _supports_multi_package(carriers, c["carrier_id"], s.get("service_code"))
                for s in c.get("services") or [])]
            multi = rate_call(parcels, multi_ids) if multi_ids else None
            per_box = {}
            if single_ids:
                per_box = _map_parallel(list(range(len(parcels))),
                                        lambda i: rate_call([parcels[i]], single_ids))
                failed = [r for r in per_box.values() if isinstance(r, ProviderError)]
                if failed:
                    errors.extend(str(e) for e in failed)
                    per_box = {}
            combined = []
            if multi:
                combined += [q for q in _combine_rates(multi[1], catalog, names, provider=self.name) if multi_ok(q)]
            if per_box:
                combined += [q for q in _combine_per_box([per_box[i][1] for i in range(len(parcels))],
                                                         catalog, names, provider=self.name) if not multi_ok(q)]
            combined = _rank(combined)
            if multi:
                drafts = [DraftShipment(_box_id(multi[0], i, len(parcels))) for i in range(len(parcels))]
            elif per_box:
                drafts = [DraftShipment(per_box[i][0]) for i in range(len(parcels))]
            else:
                drafts = []
        if not combined and errors:
            raise ProviderError("ShipStation rating failed: " + " | ".join(dict.fromkeys(errors)))
        if not drafts:
            raise ProviderError("ShipStation did not return a shipment id")
        self._remember_services(combined, catalog)
        return drafts, combined, []

    # ---- quoted-but-uncatalogued services ----
    # The carrier catalog (GET /v2/carriers) lists only some of the services
    # ShipStation actually quotes — USPS Media Mail, Parcel Select Ground and
    # Priority Mail Express come back from /v2/rates without being listed. Any
    # quoted service the catalog lacks is remembered so Settings can exclude it.
    def _seen_services(self):
        try:
            seen = json.loads(self.setting("seen_services") or "{}")
        except (ValueError, TypeError):
            return {}
        return seen if isinstance(seen, dict) else {}

    def _remember_services(self, quotes, catalog):
        seen = self._seen_services()
        new = {}
        for q in quotes:
            carrier_id, service_code = _split_service_id(q.provider_service_id)
            if (carrier_id, service_code) in catalog or q.provider_service_id in seen:
                continue
            new[q.provider_service_id] = {"carrier_id": carrier_id, "name": q.courier_name}
        if new:
            db.set_setting(self.setting_key("seen_services"), json.dumps({**seen, **new}))

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
        auth = _auth(self.name)
        carrier_id, service_code = _split_service_id(service_id)
        label_format = _label_format(self.name)
        test_label = _test_labels(self.name)
        carriers = _carriers(auth)
        catalog, names = _service_catalog(carriers), self.carrier_names(carriers)
        origin = _origin_address(self.origin())  # settings are read here, not in worker threads

        # Several box ids can share one multi-package shipment — purchase once
        # per shipment, then hand each box its own package's view of the label.
        # A service that cannot carry several packages is bought once per box
        # instead, each from its own slice of the shared draft.
        by_base = {}
        for bid in provider_shipment_ids:
            base, _ = _split_box_id(bid)
            by_base.setdefault(base, []).append(bid)
        multi_ok = _supports_multi_package(carriers, carrier_id, service_code)
        purchases = {}  # external id -> (draft id, package index or None, box ids it covers)
        for base, bids in by_base.items():
            if len(bids) > 1 and not multi_ok:
                for bid in bids:
                    purchases[bid] = (base, _split_box_id(bid)[1], [bid])
            else:
                purchases[base] = (base, None, bids)

        def work(external_id):
            existing = self._existing_label(external_id, auth)
            if existing is not None:
                return existing
            base, index, _ = purchases[external_id]
            draft = _request("GET", f"/v2/shipments/{base}", auth=auth)
            body = {
                "shipment": _inline_shipment(draft, carrier_id, service_code, external_id, origin, index),
                "test_label": test_label,
                "validate_address": "no_validation",
                "label_format": label_format,
                "label_layout": "4x6",
                "label_download_type": "url",
            }
            try:
                return _request("POST", "/v2/labels", json_body=body, timeout=90, auth=auth)
            except ProviderError as e:
                if e.recoverable:
                    raise  # captured per purchase by _map_parallel; the buy loop re-checks
                return ("failed", e)  # deterministic rejection — don't retry it every poll

        results = _map_parallel(list(purchases), work)
        out = {}
        for external_id, (_, _, bids) in purchases.items():
            res = results.get(external_id)
            for bid in bids:
                if isinstance(res, ProviderError):
                    out[bid] = res
                elif isinstance(res, tuple):
                    out[bid] = _failed_state(bid, res[1])
                else:
                    out[bid] = _to_state(res, catalog, names, box_id=bid)
        return out

    def poll_shipments(self, provider_shipment_ids, service_id=None):
        auth = _auth(self.name)
        carriers = _carriers(auth)
        catalog, names = _service_catalog(carriers), self.carrier_names(carriers)

        by_base = {}
        for bid in provider_shipment_ids:
            base, _ = _split_box_id(bid)
            by_base.setdefault(base, []).append(bid)

        def work(base):
            """The shared label, or — when the boxes were bought one at a time
            from this draft — each box's own label keyed by box id."""
            label = self._existing_label(base, auth)
            if label is not None or len(by_base[base]) == 1:
                return label
            return {bid: self._existing_label(bid, auth) for bid in by_base[base]}

        results = _map_parallel(list(by_base), work)
        out = {}
        for base, bids in by_base.items():
            res = results.get(base)
            for bid in bids:
                label = res.get(bid) if isinstance(res, dict) and "label_id" not in res else res
                if isinstance(label, ProviderError):
                    out[bid] = label
                elif label is None:
                    out[bid] = ShipmentState(provider_shipment_id=bid,
                                             label_status=LabelStatus.NOT_CREATED, raw={})
                else:
                    out[bid] = _to_state(label, catalog, names, box_id=bid)
        return out

    def fetch_labels(self, state):
        fmt = _label_format(self.name)
        url = _download_url(state.raw or {}, fmt)
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
        seen = set()
        bases = []
        for raw_id in provider_shipment_ids:
            base, _ = _split_box_id(raw_id)
            if base and base not in seen:
                seen.add(base)
                bases.append(base)
        auth = _auth(self.name) if bases else None
        for sid in bases:
            try:
                result = _request("PUT", f"/v2/labels/{sid}/void", auth=auth)
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
                _request("PUT", f"/v2/shipments/{sid}/cancel", auth=auth)
            except ProviderError as e:
                # A never-created or already-cancelled draft — nothing to undo.
                if e.status in (404, 409):
                    continue
                errors.append(f"{sid}: {e}")
        return errors

    def get_raw_shipment(self, provider_shipment_id):
        base, _ = _split_box_id(provider_shipment_id)
        auth = _auth(self.name)
        try:
            return _request("GET", f"/v2/labels/{base}", auth=auth)
        except ProviderError:
            return _request("GET", f"/v2/shipments/{base}", auth=auth)

    # ---- manifests ----
    def supports_manifests(self):
        return True

    def create_manifest(self, provider_shipment_ids):
        """POST /v2/manifests over the stored label ids. Every box of a
        multi-box group stores the same label id, so the ids are deduped (and
        any '#box' suffix stripped). USPS manifests normally come back in the
        create response itself; a pending manifest_request is polled."""
        auth = _auth(self.name)
        label_ids = []
        for raw_id in provider_shipment_ids:
            base, _ = _split_box_id(raw_id)
            if base and base not in label_ids:
                label_ids.append(base)
        if not label_ids:
            raise ProviderError("No labels to manifest")
        resp = _request("POST", "/v2/manifests", json_body={"label_ids": label_ids},
                        timeout=90, auth=auth)
        manifests = resp.get("manifests") or []
        if not manifests:
            manifests = self._wait_for_manifests(resp, auth)
        results = []
        for m in manifests:
            covered = m.get("label_ids") or []
            results.append(ManifestResult(
                provider_manifest_id=m.get("manifest_id"),
                ref_number=m.get("submission_id"),
                shipment_count=m.get("shipments") or len(covered) or len(label_ids),
                provider_shipment_ids=[i for i in label_ids if not covered or i in covered],
                document=self._manifest_document(m, auth),
                raw=m,
            ))
        return results

    def _wait_for_manifests(self, resp, auth, timeout=90):
        pending = [r.get("manifest_request_id")
                   for r in resp.get("manifest_requests") or [] if r.get("manifest_request_id")]
        if not pending:
            raise ProviderError("ShipStation returned no manifest for these labels")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(3)
            done = []
            for rid in pending:
                try:
                    data = _request("GET", f"/v2/manifests/{rid}", auth=auth)
                except ProviderError as e:
                    if e.status == 404:
                        continue  # not materialized under this id yet
                    raise
                if data.get("manifests"):
                    done.extend(data["manifests"])
                elif data.get("manifest_id") or data.get("manifest_download"):
                    done.append(data)
            if done:
                return done
        raise ProviderError(
            "ShipStation is still generating the manifest — check Shipments → "
            "End of Day in ShipStation before trying again, so the labels are "
            "not manifested twice"
        )

    def _manifest_document(self, manifest, auth):
        url = (manifest.get("manifest_download") or {}).get("href")
        if not url:
            return None
        try:
            doc = requests.get(url, timeout=30)
            if doc.status_code in (401, 403):
                doc = requests.get(url, headers={"API-Key": auth[1]}, timeout=30)
            if doc.ok and doc.content:
                return (doc.content, "pdf")
        except requests.RequestException:
            pass
        return None

    # ---- settings surface ----
    def list_item_categories(self):
        return []

    def list_courier_services(self):
        carriers = self.rating_carriers(_carriers(_auth(self.name), force=True))
        names = self.carrier_names(carriers)
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
        for sid, seen in self._seen_services().items():
            carrier_id = (seen or {}).get("carrier_id")
            if sid not in services and carrier_id in names:
                services[sid] = {
                    "id": sid,
                    "umbrella_name": names.get(carrier_id) or "",
                    "name": seen.get("name") or sid,
                }
        return sorted(services.values(), key=lambda s: (s["umbrella_name"].lower(), s["name"].lower()))

    def active_mode(self):
        return ""

    def is_test_mode(self):
        return _test_labels(self.name)

    def test_connection(self, mode=None, token=None):
        if not token or token == MASK:
            token = self.setting("api_key")
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
            return {"ok": True, "account": self._connection_summary(carriers)}
        if resp.status_code in (401, 403):
            raise ProviderError(f"API key rejected ({resp.status_code}) — check the key")
        raise ProviderError(f"ShipStation returned {resp.status_code}: {(resp.text or '')[:200]}")

    def _connection_summary(self, carriers):
        """The 'Connected' line on the Settings card, from the raw carrier list."""
        names = sorted({c.get("friendly_name") or c.get("carrier_code") or "" for c in carriers} - {""})
        summary = f"{len(carriers)} carrier(s)" + (": " + ", ".join(names) if names else "")
        return summary + self._test_labels_suffix()

    def _test_labels_suffix(self):
        return " — test labels ON (no charge)" if _test_labels(self.name) else ""

    def descriptor(self):
        return {
            "name": self.name,
            "key": self.name,
            "platform": self.platform,
            "platform_label": self.platform_label,
            "label": self.label,
            "enabled": self.setting("enabled") == "true",
            "enabled_key": self.setting_key("enabled"),
            "modes": [],
            "fields": [
                {"key": self.setting_key("api_key"), "label": "API key (v2)", "type": "secret",
                 "hint": "ShipStation → Settings → Account → API Settings. Needs a Standard plan or higher."},
                *self._label_fields(),
            ],
            "test_endpoint": f"/api/providers/{self.name}/test",
            "supports": {"service_exclusions": True},
            "services_endpoint": f"/api/providers/{self.name}/services",
            "excluded_endpoint": f"/api/providers/{self.name}/excluded-services",
            **origin_descriptor(self.name),
        }

    def _label_fields(self):
        """Descriptor fields every ShipStation-backed instance shares."""
        return [
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
             "hint": "ShipStation has no sandbox; test labels are free but cannot be shipped. Shows the SANDBOX badge."},
        ]

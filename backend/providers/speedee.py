"""Spee-Dee Delivery (ShipIt, on the Dispatch Science platform) implementation
of the ShippingProvider interface.

Spee-Dee's model differs from the label platforms; the adapter absorbs it:
  * HTTP Basic auth with a username + password per environment (test and
    production hosts), so two modes like Easyship;
  * a quote (`POST /orders/quote`) creates nothing — CREATING THE ORDER IS THE
    PURCHASE. The draft is therefore local: its order body is kept in the
    `speedee_drafts` table between rating and buying;
  * one order carries every box as an item, each with its own barcode
    (the tracking number). Box ids follow ShipStation's '<id>#<n>' convention;
  * right after creation each item's barcode is a placeholder ('01', '02'…)
    and printing it delays or returns the parcel, so a box stays PENDING until
    its real barcode is assigned;
  * labels are ZPL only (4x6 'STANDARD' template), one per item;
  * creating an order is not idempotent: the draft token rides in
    referenceNumber3 (referenceNumber2 is Spee-Dee's own), so an order whose
    create response was lost is found by search instead of being created twice;
  * there is no manifest API — the end-of-day manifest is a summary sheet this
    app generates for the driver.
"""
import base64
import json
import time
import uuid
from datetime import datetime, timezone

import requests

import db
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

BASE_URLS = {
    "sandbox": "https://speedeetest.dsapp.io/integration",
    "production": "https://speedee.dsapp.io/integration",
}
MASK = "••••••••"
SERVICE_ID = "PACKAGE_PARCEL"
VEHICLE_ID = "STEPVAN"
PARCEL_TYPE = "BOX"
COURIER_NAME = "Spee-Dee Parcel"
UMBRELLA_NAME = "Spee-Dee"
DEFAULT_LABEL_TEMPLATE = "STANDARD"
LABEL_FORMAT_ZPL = 0
SIGNATURE_FEES = {"signature": "AOD", "adult": "AODSIGNATURE"}
COD_FEE = "COD"
CANCELLED_STATUSES = {91, "91", "cancelled", "canceled"}
TRACKING_URL = "https://speedeedelivery.com/track-a-shipment/?barcodes={number}"
DRAFT_PREFIX = "spd_"
# A create whose response was lost may take a moment to become searchable;
# only after this long with no match is the order assumed never created.
LOST_CREATE_GRACE_S = 120

ORIGIN_REQUIRED = {
    "origin_company": "Company",
    "origin_address1": "Address 1",
    "origin_city": "City",
    "origin_state": "State",
    "origin_zip": "ZIP",
    "origin_phone": "Phone",
}


def _auth(key):
    """(base_url, username, password) for the instance's active mode, read in
    the request context."""
    mode = db.get_setting(f"{key}_mode") or "sandbox"
    username = db.get_setting(f"{key}_{mode}_username")
    password = db.get_setting(f"{key}_{mode}_password")
    if not username or not password:
        raise ProviderError(f"No Spee-Dee {mode} username/password configured — set them in Settings")
    return BASE_URLS.get(mode, BASE_URLS["sandbox"]), username, password


def _extract_error(resp):
    """Spee-Dee errors are ASP.NET ModelState dictionaries —
    {field: {errors: [{errorMessage}]}} or {"error": ["..."]} — or plain text."""
    try:
        data = resp.json()
    except ValueError:
        return f"Spee-Dee error ({resp.status_code}): {(resp.text or '').strip()[:300] or 'request failed'}"
    messages = []
    if isinstance(data, dict):
        for field, entry in data.items():
            if isinstance(entry, dict):
                for err in entry.get("errors") or []:
                    msg = err.get("errorMessage") if isinstance(err, dict) else err
                    if msg:
                        messages.append(f"{field}: {msg}" if field and field != "error" else str(msg))
            elif isinstance(entry, list):
                messages.extend(str(m) for m in entry if m)
            elif isinstance(entry, str) and field in ("error", "message", "title", "detail"):
                messages.append(entry)
    elif isinstance(data, list):
        messages.extend(str(m) for m in data if m)
    detail = "; ".join(messages) or str(data)[:300]
    return f"Spee-Dee error ({resp.status_code}): {detail}"


def _request(method, path, json_body=None, params=None, timeout=45, auth=None):
    base_url, username, password = auth
    url = f"{base_url}{path}"
    # GETs are idempotent, so ride through transient gateway timeouts / 5xx.
    # Order creation is NEVER auto-retried — a lost create is found by search.
    retry_recoverable = method.upper() == "GET"
    resp = None
    last_exc = None
    for attempt in range(4):
        try:
            resp = requests.request(
                method, url, json=json_body, params=params,
                auth=(username, password),
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
        except requests.RequestException as e:
            if retry_recoverable and attempt < 3:
                last_exc = e
                time.sleep(min(1.5 * (attempt + 1), 10))
                continue
            raise ProviderError(f"Spee-Dee request failed: {e}", status=None)
        if resp.status_code == 429:
            time.sleep(min(1.5 * (attempt + 1), 15))
            continue
        if retry_recoverable and resp.status_code >= 500 and attempt < 3:
            time.sleep(min(1.5 * (attempt + 1), 10))
            continue
        break
    if resp is None:
        raise ProviderError(f"Spee-Dee request failed: {last_exc}", status=None)
    if resp.status_code >= 400:
        raise ProviderError(_extract_error(resp), status=resp.status_code)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


def _address(company, address1, address2, city, state, zip_code, country):
    return {
        "addressLine1": address1 or "",
        "addressLine2": address2 or None,
        "company": company or None,
        "city": city or "",
        "stateProvince": (state or "").strip().upper(),
        "postalCode": (zip_code or "").strip(),
        "country": country or "US",
    }


def _contact(name, phone, email):
    return {"name": name or None, "phoneNumber": phone or None, "email": email or None}


def _origin_parts(origin):
    missing = missing_origin_fields(origin, ORIGIN_REQUIRED)
    if missing:
        raise ProviderError(
            "Origin address is incomplete — fill in on the Settings page: " + ", ".join(missing)
        )
    company = origin.get("origin_company")
    address = _address(company, origin.get("origin_address1"), origin.get("origin_address2"),
                       origin.get("origin_city"), origin.get("origin_state"),
                       origin.get("origin_zip"), "US")
    contact = _contact(origin.get("origin_contact") or company,
                       origin.get("origin_phone"), origin.get("origin_email"))
    return address, contact


def _dest_parts(dest):
    name = dest.get("contact") or dest.get("company") or "Recipient"
    # A person's name on the label when there is no company — the label has no
    # other recipient line.
    company = dest.get("company") or name
    address = _address(company, dest.get("address1"), dest.get("address2"), dest.get("city"),
                       dest.get("state"), dest.get("zip"), dest.get("country"))
    return address, _contact(name, dest.get("phone"), dest.get("email"))


def _build_items(parcels):
    """One BOX item per parcel, weight in lb and sides in inches (Spee-Dee's
    units). Spee-Dee rejects an item without a positive weight and all three
    sides, so such boxes are refused here, by number."""
    items, incomplete = [], []
    for number, p in enumerate(parcels, start=1):
        try:
            weight = float(str(p.get("weight") or "0").strip())
        except ValueError:
            weight = 0.0
        dims = parcel_dimensions(p)
        if weight <= 0 or not dims:
            incomplete.append(str(number))
            continue
        items.append({"parcelTypeId": PARCEL_TYPE, "weight": round(weight, 2), **dims, "userFields": []})
    if incomplete:
        raise ProviderError("Spee-Dee needs the weight and L × W × H of every box — missing on box "
                            + ", ".join(incomplete))
    return items


def _ready_at():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _order_body(origin, dest, parcels, options, token):
    pickup_address, pickup_contact = _origin_parts(origin)
    dropoff_address, dropoff_contact = _dest_parts(dest)
    body = {
        "pickupAddress": pickup_address,
        "pickupContactInfo": pickup_contact,
        "dropoffAddress": dropoff_address,
        "dropoffContactInfo": dropoff_contact,
        "serviceLevelId": SERVICE_ID,
        "vehicleTypeId": VEHICLE_ID,
        "readyAt": _ready_at(),
        # referenceNumber2 is reserved: Spee-Dee builds the tracking number from it.
        "referenceNumber1": ((options or {}).get("reference") or "")[:50],
        "referenceNumber3": token,
        "items": _build_items(parcels),
    }
    options = options or {}
    fees = [SIGNATURE_FEES.get(options.get("signature") or "none")]
    cod = options.get("cod")
    if cod:
        fees.append(COD_FEE)
        # One order per group, so the amount is collected once for every box.
        body["collectOnDelivery"] = cod["amount"]
        body["allowPartialCollectOnDelivery"] = bool(cod.get("allow_partial"))
    if any(fees):
        body["extraFees"] = [{"extraFeeId": fee, "quantity": 1} for fee in fees if fee]
    return body


def _box_id(base, index, count):
    """The per-box id: the base itself for a single box, '<base>#<n>' when one
    order backs several boxes."""
    return base if count == 1 else f"{base}#{index + 1}"


def _split_box_id(box_id):
    """('SD3041', 0) from 'SD3041#1'; (id, None) for a plain single-box id."""
    base, _, idx = (box_id or "").partition("#")
    if not idx:
        return base, None
    try:
        return base, max(int(idx) - 1, 0)
    except ValueError:
        return base, None


def _quote_total(quote):
    total = 0.0
    for charge in quote.get("charges") or []:
        try:
            total += float(charge.get("price") or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _transit_days(quote):
    """Days between the pickup window opening and the dropoff window closing —
    Spee-Dee's own delivery promise for the quote."""
    start = _parse_time((quote.get("pickupWindow") or {}).get("start"))
    end = _parse_time((quote.get("dropoffWindow") or {}).get("end"))
    if not start or not end or end < start:
        return None
    return max((end.date() - start.date()).days, 1)


def _validation_errors(payload):
    return [str(e) for e in (payload or {}).get("validationErrors") or [] if e]


def _is_temporary_barcode(item):
    """Right after an order is created each item's barcode is its item number
    ('01', '02'…) until Spee-Dee assigns the real one."""
    barcode = str(item.get("barcode") or "").strip()
    if not barcode:
        return True
    try:
        return barcode.isdigit() and int(barcode) == int(item.get("itemNumber") or 0)
    except ValueError:
        return False


def _failed_state(error):
    return ShipmentState(provider_shipment_id=None, label_status=LabelStatus.FAILED,
                         error_message=str(error))


def _pending_state():
    return ShipmentState(provider_shipment_id=None, label_status=LabelStatus.PENDING)


class SpeedeeProvider(ShippingProvider):
    platform = "speedee"
    label = "Spee-Dee"
    modes = ("sandbox", "production")
    manifest_carrier = "Spee-Dee"
    manifests_every_label = True
    supports_cod = True

    def _label_template(self):
        return (self.setting("label_template") or "").strip() or DEFAULT_LABEL_TEMPLATE

    # ---- drafts (the local half of a Spee-Dee "shipment") ----
    def _save_draft(self, body, quote):
        draft_id = DRAFT_PREFIX + uuid.uuid4().hex[:12]
        db.execute(
            "INSERT INTO speedee_drafts (draft_id, provider, body, quote) VALUES (%s, %s, %s, %s)",
            (draft_id, self.name, json.dumps(body), json.dumps(quote)),
        )
        # Unbought drafts are never billed; keep the table from growing forever.
        db.execute(
            """DELETE FROM speedee_drafts
               WHERE order_id IS NULL AND created_at < now() - interval '30 days'""",
        )
        return draft_id

    def _draft(self, draft_id):
        return db.query("SELECT * FROM speedee_drafts WHERE draft_id = %s AND provider = %s",
                        (draft_id, self.name), one=True)

    def _order_id_for(self, base):
        """The Spee-Dee order behind a box's base id — a draft id is resolved
        through its draft row, an order id is itself."""
        if not base.startswith(DRAFT_PREFIX):
            return base
        draft = self._draft(base)
        return (draft or {}).get("order_id")

    # ---- rating / drafting ----
    def create_draft_shipments(self, destination, parcels, items, options=None):
        auth = _auth(self.name)
        token = uuid.uuid4().hex[:16]
        body = _order_body(self.origin(), destination, parcels, options, token)
        quote = _request("POST", "/api/v1/orders/quote", json_body=body, timeout=60, auth=auth)
        errors = _validation_errors(quote)
        if errors:
            raise ProviderError("Spee-Dee cannot ship this: " + "; ".join(errors))
        total = _quote_total(quote)
        if total <= 0:
            raise ProviderError("Spee-Dee returned no price for this shipment")
        draft_id = self._save_draft(body, {"total": total, "boxes": len(parcels)})
        drafts = [DraftShipment(_box_id(draft_id, i, len(parcels))) for i in range(len(parcels))]
        days = _transit_days(quote)
        rate = Rate(
            provider=self.name,
            provider_service_id=SERVICE_ID,
            courier_name=COURIER_NAME,
            umbrella_name=UMBRELLA_NAME,
            total_charge=total,
            currency="USD",
            min_delivery_time=days,
            max_delivery_time=days,
            value_for_money_rank=1,
        )
        return drafts, [rate], []

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
    def _find_lost_order(self, token, auth):
        """The order a lost create response made, found by its draft token."""
        result = _request("POST", "/api/v1/orders/search", json_body={
            "query": token, "searchFields": ["ReferenceNumber3"], "operator": 2, "maximumResults": 5,
        }, auth=auth)
        for order in (result or {}).get("items") or []:
            if order.get("status") in CANCELLED_STATUSES:
                continue
            ref = order.get("referenceNumber3")
            if order.get("orderId") and (ref is None or ref == token):
                return order["orderId"]
        return None

    def _place_order(self, draft, auth):
        """The order id for a draft — created at most once. Returns
        (order_id, None) or (None, ShipmentState) when there is no order yet."""
        if draft.get("order_id"):
            return draft["order_id"], None
        body = draft["body"]
        token = body.get("referenceNumber3")
        if draft.get("create_started_at"):
            order_id = self._find_lost_order(token, auth)
            if order_id:
                db.execute("UPDATE speedee_drafts SET order_id = %s WHERE draft_id = %s",
                           (order_id, draft["draft_id"]))
                return order_id, None
            age = (datetime.now(timezone.utc) - draft["create_started_at"]).total_seconds()
            if age < LOST_CREATE_GRACE_S:
                return None, _pending_state()
        db.execute("UPDATE speedee_drafts SET create_started_at = now() WHERE draft_id = %s",
                   (draft["draft_id"],))
        try:
            created = _request("POST", "/api/v1/orders",
                               json_body={**body, "readyAt": _ready_at()}, timeout=90, auth=auth)
        except ProviderError as e:
            if e.recoverable:
                raise  # the order may exist — the next attempt searches for it
            db.execute("UPDATE speedee_drafts SET create_started_at = NULL WHERE draft_id = %s",
                       (draft["draft_id"],))
            return None, _failed_state(e)
        order_id = (created or {}).get("orderId")
        if not order_id:
            db.execute("UPDATE speedee_drafts SET create_started_at = NULL WHERE draft_id = %s",
                       (draft["draft_id"],))
            reason = "; ".join(_validation_errors(created)) or "Spee-Dee created no order"
            return None, _failed_state(reason)
        db.execute("UPDATE speedee_drafts SET order_id = %s WHERE draft_id = %s",
                   (order_id, draft["draft_id"]))
        return order_id, None

    def _box_states(self, order_id, box_ids, draft, auth):
        """Each box's state from the order's items: READY once its real barcode
        is assigned. `box_ids` are the caller's ids (draft or order based)."""
        items = _request("GET", f"/api/v1/orders/{order_id}/items", auth=auth)
        by_number = {int(i.get("itemNumber") or 0): i for i in items or [] if isinstance(i, dict)}
        quote = (draft or {}).get("quote") or {}
        count = len(box_ids)
        cost = round(float(quote["total"]) / count, 2) if quote.get("total") and count else None
        out = {}
        cancelled = None
        for bid in box_ids:
            _, index = _split_box_id(bid)
            number = (index or 0) + 1
            item = by_number.get(number)
            active_id = _box_id(order_id, number - 1, count)
            if item and not _is_temporary_barcode(item):
                out[bid] = ShipmentState(
                    provider_shipment_id=active_id,
                    label_status=LabelStatus.READY,
                    tracking_numbers=[item["barcode"]],
                    courier_name=COURIER_NAME,
                    courier_umbrella_name=UMBRELLA_NAME,
                    cost=cost,
                    raw={"order_id": order_id, "item_id": item.get("itemId"), "item_number": number},
                )
                continue
            if cancelled is None:
                status = (_request("GET", f"/api/v1/orders/{order_id}/status", auth=auth) or {}).get("status")
                cancelled = str(status).lower() in {str(s) for s in CANCELLED_STATUSES}
            if cancelled:
                out[bid] = _failed_state(f"Spee-Dee order {order_id} was cancelled")
            else:
                out[bid] = ShipmentState(provider_shipment_id=active_id, label_status=LabelStatus.PENDING,
                                         raw={"order_id": order_id, "item_number": number})
        return out

    def _by_base(self, provider_shipment_ids):
        groups = {}
        for bid in provider_shipment_ids:
            base, _ = _split_box_id(bid)
            groups.setdefault(base, []).append(bid)
        return groups

    def buy_labels(self, provider_shipment_ids, service_id):
        auth = _auth(self.name)
        out = {}
        # One order per draft, every box an item — sequential, since drafts
        # live in Postgres and worker threads have no request context.
        for base, bids in self._by_base(provider_shipment_ids).items():
            try:
                draft = self._draft(base)
                if not draft:
                    raise ProviderError("Spee-Dee quote not found — get rates again")
                order_id, state = self._place_order(draft, auth)
                if state is not None:
                    out.update({bid: state for bid in bids})
                    continue
                out.update(self._box_states(order_id, bids, draft, auth))
            except ProviderError as e:
                out.update({bid: e for bid in bids})
        return out

    def poll_shipments(self, provider_shipment_ids, service_id=None):
        auth = _auth(self.name)
        out = {}
        for base, bids in self._by_base(provider_shipment_ids).items():
            try:
                draft = self._draft(base) if base.startswith(DRAFT_PREFIX) else None
                order_id = draft.get("order_id") if draft else (
                    None if base.startswith(DRAFT_PREFIX) else base)
                if not order_id:
                    # Not created yet (or its create response was lost): the
                    # buy loop re-issues buy_labels, which finds or creates it.
                    out.update({bid: ShipmentState(provider_shipment_id=None,
                                                   label_status=LabelStatus.NOT_CREATED)
                                for bid in bids})
                    continue
                out.update(self._box_states(order_id, bids, draft, auth))
            except ProviderError as e:
                out.update({bid: e for bid in bids})
        return out

    def fetch_labels(self, state):
        raw = state.raw or {}
        order_id = raw.get("order_id")
        if not order_id:
            return []
        data = _request(
            "GET", f"/api/v1/orders/{order_id}/items/label/{self._label_template()}/{LABEL_FORMAT_ZPL}",
            auth=_auth(self.name), timeout=60,
        )
        labels = (data or {}).get("labels") or []
        match = next((l for l in labels if raw.get("item_id") and l.get("itemId") == raw["item_id"]), None)
        if match is None and state.tracking_numbers:
            match = next((l for l in labels if l.get("itemBarcode") == state.tracking_numbers[0]), None)
        if match is None or not match.get("encodedLabel"):
            return []
        try:
            return [(base64.b64decode(match["encodedLabel"]), "zpl")]
        except (ValueError, TypeError):
            return []

    def cancel_all(self, provider_shipment_ids):
        errors = []
        bases = list(dict.fromkeys(_split_box_id(i)[0] for i in provider_shipment_ids if i))
        if not bases:
            return errors
        auth = None
        for base in bases:
            order_id = self._order_id_for(base)
            if not order_id:
                if base.startswith(DRAFT_PREFIX):
                    db.execute("DELETE FROM speedee_drafts WHERE draft_id = %s AND order_id IS NULL",
                               (base,))
                continue  # never created — nothing to cancel
            try:
                auth = auth or _auth(self.name)
                _request("DELETE", f"/api/v1/orders/{order_id}", auth=auth)
            except ProviderError as e:
                if e.status == 404 or "already" in str(e).lower():
                    continue
                errors.append(f"{order_id}: {e}")
        return errors

    def get_raw_shipment(self, provider_shipment_id):
        base, _ = _split_box_id(provider_shipment_id)
        order_id = self._order_id_for(base)
        if not order_id:
            return {"draft": self._draft(base)}
        auth = _auth(self.name)
        return {
            "order": _request("GET", f"/api/v1/orders/{order_id}", auth=auth),
            "items": _request("GET", f"/api/v1/orders/{order_id}/items", auth=auth),
        }

    def tracking_url(self, number):
        return TRACKING_URL.format(number=number) if number else None

    # ---- manifests: a summary sheet for the driver (no Spee-Dee API) ----
    def supports_manifests(self):
        return True

    def create_manifest(self, provider_shipment_ids):
        import manifest_sheet

        ids = [i for i in dict.fromkeys(provider_shipment_ids) if i]
        if not ids:
            raise ProviderError("No shipments to manifest")
        rows = db.query(
            """SELECT easyship_shipment_id, box_number, box_total, tracking_number, destination,
                      total_weight_lb, label_created_at, options
               FROM shipments WHERE provider = %s AND easyship_shipment_id = ANY(%s)
               ORDER BY label_created_at, group_id, box_number""",
            (self.name, ids),
        ) or []
        if not rows:
            raise ProviderError("None of these parcels were found")
        now = datetime.now(timezone.utc)
        document = manifest_sheet.summary_sheet_pdf(
            title=f"{self.label} — pickup manifest",
            ship_date=manifest_sheet.central_date(now),
            origin=self.origin(),
            rows=[manifest_sheet.sheet_row(r, _split_box_id(r["easyship_shipment_id"])[0]) for r in rows],
        )
        return [ManifestResult(
            provider_manifest_id=None,
            ref_number=f"{manifest_sheet.central_date(now)} · {len(rows)} pkg",
            shipment_count=len(rows),
            provider_shipment_ids=ids,
            document=(document, "pdf"),
            raw={"generated": "summary_sheet", "packages": len(rows)},
        )]

    # ---- settings surface ----
    def list_item_categories(self):
        return []

    def list_courier_services(self):
        return [{"id": SERVICE_ID, "umbrella_name": UMBRELLA_NAME, "name": COURIER_NAME}]

    def active_mode(self):
        mode = self.setting("mode") or "sandbox"
        return mode if mode in BASE_URLS else "sandbox"

    def test_connection(self, mode=None, token=None, username=None):
        mode = mode if mode in BASE_URLS else self.active_mode()
        password = token if token and token != MASK else self.setting(f"{mode}_password")
        username = (username or "").strip() or self.setting(f"{mode}_username")
        if not username or not password:
            raise ProviderError(f"Enter the Spee-Dee {mode} username and password")
        try:
            resp = requests.get(f"{BASE_URLS[mode]}/api/v1/account/workflows",
                                auth=(username, password), headers={"Accept": "application/json"},
                                timeout=15)
        except requests.RequestException as e:
            raise ProviderError(f"Connection failed: {e}")
        if resp.status_code == 200:
            env = "test" if mode == "sandbox" else "live"
            return {"ok": True, "account": f"{username} connected ({env})"}
        if resp.status_code in (401, 403):
            raise ProviderError(f"Username or password rejected ({resp.status_code}) — check the {mode} credentials")
        raise ProviderError(_extract_error(resp))

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
                {"value": "sandbox", "label": "Test (speedeetest)"},
                {"value": "production", "label": "Production (live — orders are real pickups)"},
            ],
            "fields": [
                {"key": self.setting_key("sandbox_username"), "label": "Test username",
                 "type": "text", "mode": "sandbox", "test_param": "username"},
                {"key": self.setting_key("sandbox_password"), "label": "Test password",
                 "type": "secret", "mode": "sandbox"},
                {"key": self.setting_key("production_username"), "label": "Production username",
                 "type": "text", "mode": "production", "test_param": "username"},
                {"key": self.setting_key("production_password"), "label": "Production password",
                 "type": "secret", "mode": "production"},
                {"key": self.setting_key("label_template"), "label": "Label template ID",
                 "type": "text", "hint": "Blank = STANDARD (4x6 ZPL). Labels print on a Zebra printer."},
            ],
            "test_endpoint": f"/api/providers/{self.name}/test",
            "supports": {"service_exclusions": False},
            **origin_descriptor(self.name),
        }

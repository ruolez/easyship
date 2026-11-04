"""UDS (United Delivery Service) implementation of the ShippingProvider
interface, on the UDS API 3.0 (`api3.udsdelivery.com/v3`, OpenAPI spec at
uniteddeliveryservice.com/uds/3.0/api/doc.php).

UDS's model, and how the adapter absorbs it:
  * OAuth client credentials: `POST /token` returns an access token (1 h)
    that goes in the `x-api-key` header — not `Authorization: Bearer`. There
    is one host and no sandbox, so the platform has no modes;
  * a quote (`POST /order/quote/{vendorId}`) creates nothing — CREATING THE
    ORDER IS THE PURCHASE. The draft is local: its order body is kept in the
    `uds_drafts` table between rating and buying;
  * one order carries every box as a package, each with its own barcode (the
    tracking number). Box ids follow Spee-Dee's '<order id>#<n>' convention;
  * the create response carries each package's label in the requested
    PrintLabelFormat (raw ZPL, or a base64 PDF), stored on the draft row;
  * creating an order is not idempotent by default: OrderRef is unique per
    draft and UseGroupByOrderRef is false, so UDS refuses a second order with
    the same OrderRef and a create whose response was lost can be re-sent;
  * tracking events are pulled (`GET /tracking/pending` hands each event out
    once) or read per barcode (`POST /tracking`); no webhooks — the server is
    on a LAN;
  * there is no COD and no manifest API.
"""
import base64
import hashlib
import json
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import db
from providers.base import (
    DraftShipment,
    LabelStatus,
    ProviderError,
    Rate,
    ShipmentState,
    ShippingProvider,
    missing_origin_fields,
    origin_descriptor,
    parcel_dimensions,
)

BASE_URL = "https://api3.udsdelivery.com/v3"
MASK = "••••••••"
SERVICE_ID = "UDS"
COURIER_NAME = "UDS Delivery"
UMBRELLA_NAME = "UDS"
TRACKING_URL = "https://www.uniteddeliveryservice.com/track/barcode/{number}"
DRAFT_PREFIX = "uds_"
CENTRAL = ZoneInfo("America/Chicago")
TOKEN_MARGIN_S = 60
# A create whose response was lost is re-sent (UDS refuses a duplicate
# OrderRef), but only within the day: after that UDS may treat it as a new
# delivery date, so a person checks the portal instead.
LOST_CREATE_MAX_AGE_S = 12 * 3600
DEFAULT_POLL_MINUTES = 15

# (value, label) — every PrintLabelFormat the API documents.
LABEL_FORMATS = (
    ("LabelZplStrUDS_4x6", "ZPL 4x6, UDS logo"),
    ("LabelZplStr2_4x6", "ZPL 4x6, v2 layout"),
    ("LabelZplStr_4x6", "ZPL 4x6, standard"),
    ("LabelZplStrUDS", "ZPL 4x4, UDS logo"),
    ("LabelZplStr2", "ZPL 4x4, v2 layout"),
    ("LabelZplStr", "ZPL 4x4, standard"),
    ("zebra10000300", "PDF 4x6 (Zebra 100x300)"),
    ("zebra10000280", "PDF 4x6.5 (Zebra 100x280)"),
    ("4by8point25", "PDF 4x8.25 with receipt"),
    ("RRD4by4v2", "PDF 4x4, RR Donnelley v2"),
    ("RRD4by4", "PDF 4x4, RR Donnelley"),
    ("avery5164", "PDF Avery 5164 (letter sheet, 6 per page)"),
)
DEFAULT_LABEL_FORMAT = LABEL_FORMATS[0][0]

# Preferred Drop Location values UDS accepts (AddressNotes).
DROP_LOCATIONS = (
    "Front Door", "Between Doors", "Porch", "Side Door", "Back Door", "Leasing Office",
    "Left at front desk", "Left in mailroom", "Left in mail slot", "Under door",
    "Under mat", "Under garage door",
)
# Ship-page option key -> AddressNotes Name (spelled as in the spec's example).
ADDRESS_NOTES = (
    ("access_code", "AccessCode"),
    ("door_code", "DoorCode"),
    ("parking", "ParkingInstructions"),
    ("drop_location", "PreferredDropLocation"),
    ("notes", "GeneralNotes"),
)
NOTE_MAX = 100
INSTRUCTIONS_MAX = 100
DESCRIPTION_MAX = 100

# Field maximums from the spec — longer values are a 400.
MAX = {"OrderRef": 50, "LocationName": 35, "Address1": 40, "Address2": 40, "City": 30,
       "State": 2, "ContactName": 35, "Email": 254, "Instructions": 100}

ORIGIN_REQUIRED = {
    "origin_company": "Company",
    "origin_address1": "Address 1",
    "origin_city": "City",
    "origin_state": "State",
    "origin_zip": "ZIP",
}

# Event status code -> normalized tracking status (UDS Delivery Statuses v7).
STATUS_BY_CODE = {
    "A": "info_received",
    "R": "in_transit",
    "S": "exception",
    "L": "out_for_delivery",
    "D": "delivered",
    "EX": "exception",
    "AV": "will_call",
}


# ---- small pure helpers ----
def _cut(value, field):
    text = str(value or "").strip()
    limit = MAX.get(field)
    return text[:limit] if limit else text


def zip_value(value):
    """The 5-digit ZIP as the spec's integer, or as a string when it starts
    with 0 (an integer would drop the zero)."""
    digits = re.sub(r"\D", "", str(value or ""))[:5]
    if len(digits) < 5:
        return str(value or "").strip()
    return digits if digits.startswith("0") else int(digits)


def phone_value(value):
    """The 10-digit phone as an integer (a leading country code 1 dropped),
    or None when it is not a US number — UDS stores it as an integer."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return int(digits) if len(digits) == 10 and digits[0] != "0" else None


def _weight(value):
    try:
        return round(float(str(value or "0").strip()), 2)
    except ValueError:
        return 0.0


def status_for_code(code):
    """Normalized status for a UDS event code: E* exceptions (delays,
    redeliveries), ER* returns."""
    code = str(code or "").strip().upper()
    if code in STATUS_BY_CODE:
        return STATUS_BY_CODE[code]
    if code.startswith("ER"):
        return "returning"
    if code.startswith("E"):
        return "exception"
    return "unknown"


def central_today():
    return datetime.now(CENTRAL).date()


def business_days(start, end):
    """Weekdays after `start` up to and including `end` — at least 1."""
    days, day = 0, start
    while day < end:
        day += timedelta(days=1)
        if day.weekday() < 5:
            days += 1
    return max(days, 1)


def clean_options(raw, today=None):
    """The UDS options panel, validated. Raises ValueError with a message the
    packer can act on."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    when = str(raw.get("delivery_date") or "").strip()
    if when:
        try:
            parsed = date.fromisoformat(when)
        except ValueError:
            raise ValueError("UDS delivery date must be a date (YYYY-MM-DD)")
        if parsed < (today or central_today()):
            raise ValueError("UDS delivery date cannot be in the past")
        out["delivery_date"] = parsed.isoformat()
    text = {"instructions": INSTRUCTIONS_MAX, "description": DESCRIPTION_MAX,
            **{k: NOTE_MAX for k, _ in ADDRESS_NOTES if k != "drop_location"}}
    for key, limit in text.items():
        value = " ".join(str(raw.get(key) or "").split())
        if len(value) > limit:
            raise ValueError(f"UDS {key.replace('_', ' ')} is limited to {limit} characters")
        if value:
            out[key] = value
    drop = str(raw.get("drop_location") or "").strip()
    if drop:
        match = next((d for d in DROP_LOCATIONS if d.lower() == drop.lower()), None)
        if not match:
            raise ValueError(f"Unknown UDS drop location '{drop}'")
        out["drop_location"] = match
    return out


def order_ref(reference, token):
    """A per-draft OrderRef: the order number for people, plus a token that
    makes it unique so UDS refuses a duplicate create."""
    ref = re.sub(r"\s+", " ", str(reference or "").strip())[:37]
    return f"{ref}-{token}" if ref else token


def _address_notes(options):
    notes = [{"Name": name, "Value": options[key]} for key, name in ADDRESS_NOTES if options.get(key)]
    return notes or None


def build_order(origin, dest, parcels, options, ref, label_format):
    """The {Order, Packages} object UDS quotes and creates (create sends a
    list of them). Empty optional fields are left out."""
    missing = missing_origin_fields(origin, ORIGIN_REQUIRED)
    if missing:
        raise ProviderError("Origin address is incomplete — fill in on the Settings page: " + ", ".join(missing))
    options = options or {}
    uds = options.get("provider") or {}
    signature = options.get("signature") or "none"
    contact = dest.get("contact") or dest.get("company") or "Recipient"
    reference = str(options.get("reference") or "").strip()
    packages = []
    for i, parcel in enumerate(parcels):
        dims = parcel_dimensions(parcel)
        package = {
            "Barcode": "",
            "Description": (uds.get("description") or "Box")[:DESCRIPTION_MAX],
            "Weight": _weight(parcel.get("weight")),
            "PieceNumber": f"{ref}-{i + 1}",
        }
        if dims:
            package.update({"Length": dims["length"], "Width": dims["width"], "Height": dims["height"]})
        packages.append(package)
    order = {
        "OrderRef": _cut(ref, "OrderRef"),
        "DeliveryDate": uds.get("delivery_date"),
        "LocationName": _cut(dest.get("company") or contact, "LocationName"),
        "Address1": _cut(dest.get("address1"), "Address1"),
        "Address2": _cut(dest.get("address2"), "Address2"),
        "City": _cut(dest.get("city"), "City"),
        "State": _cut((dest.get("state") or "").upper(), "State"),
        "ZipCode": zip_value(dest.get("zip")),
        "FromLocationName": _cut(origin.get("origin_company"), "LocationName"),
        "FromAddress1": _cut(origin.get("origin_address1"), "Address1"),
        "FromAddress2": _cut(origin.get("origin_address2"), "Address2"),
        "FromCity": _cut(origin.get("origin_city"), "City"),
        "FromState": _cut((origin.get("origin_state") or "").upper(), "State"),
        "FromZipCode": zip_value(origin.get("origin_zip")),
        "ContactName": _cut(contact, "ContactName"),
        "Email": _cut(dest.get("email"), "Email"),
        "Phone": phone_value(dest.get("phone")),
        "Instructions": _cut(uds.get("instructions") or (reference and f"Order {reference}"), "Instructions"),
        "SignatureRequired": signature in ("signature", "adult"),
        "AdultSignatureRequired": signature == "adult",
        "Pieces": len(packages),
        "Weight": round(sum(p["Weight"] for p in packages), 2),
        "PrintLabelFormat": label_format,
        "UseGroupByOrderRef": False,
        "AddressNotes": _address_notes(uds),
    }
    order = {k: v for k, v in order.items() if v not in (None, "")}
    return {"Order": order, "Packages": packages}


def price_breakdown(price):
    return [
        {"name": str(c.get("Name") or ""), "amount": _money(c.get("Value")),
         "description": str(c.get("Description") or ""), "included": c.get("calculated") is not False}
        for c in (price or {}).get("components") or [] if isinstance(c, dict)
    ]


def _money(value):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _event_id(number, ev):
    """UDS's TrackingId, or a stable id from the event's content."""
    if ev.get("TrackingId") not in (None, ""):
        try:
            return int(ev["TrackingId"])
        except (TypeError, ValueError):
            pass
    seed = f"{number}|{ev.get('EventStatus')}|{ev.get('EventDateTime')}|{ev.get('TrackingEventDetail')}"
    return int(hashlib.sha1(seed.encode()).hexdigest()[:15], 16)


def _location(detail, fallback):
    detail = detail or {}
    city_state = ", ".join(x for x in (detail.get("City"), detail.get("StateProvinceCode")) if x)
    return city_state or (fallback or "")


def normalize_tracking(packages):
    """TrackingResponse objects (from /tracking, /tracking/orderRef or
    /tracking/pending) -> flat event dicts for tracking_store.save."""
    events = []
    for entry in packages or []:
        info = (entry or {}).get("PackageTrackingInfo") or {}
        number = str(info.get("TrackingNumber") or "").strip()
        if not number:
            continue
        package_photos = info.get("Photos") or []
        for ev in info.get("TrackingEventHistory") or []:
            code = str(ev.get("EventStatus") or "").strip().upper()
            photos = [
                {"url": p.get("url"), "label": p.get("shortDescription") or p.get("locationLabel"),
                 "at": p.get("timestamp")}
                for p in (ev.get("Photos") or (package_photos if code == "D" else []))
                if isinstance(p, dict) and p.get("url")
            ]
            events.append({
                "tracking_number": number,
                "event_id": _event_id(number, ev),
                "status_code": code,
                "status": status_for_code(code),
                "description": ev.get("EventDescription") or "",
                "detail": ev.get("EventDetail") or "",
                "exception_detail": ev.get("TrackingEventDetail") or "",
                "event_at": ev.get("EventDateTime"),
                "location": _location(ev.get("EventScanLocationDetail"), ev.get("EventScanLocation")),
                "signed_by": ev.get("SignedForByName") or "",
                "left_at": ev.get("AdditionalLocationInfo") or "",
                "vpod_url": ev.get("VPODURL") or "",
                "signature_png": ev.get("SignatureImage") or "",
                "photos": photos,
                "rescheduled_for": ev.get("RescheduledDate"),
                "order_ref": info.get("OrderRef"),
                "delivery_date": info.get("DeliveryDate"),
            })
    return events


def normalize_order(payload):
    """GET/PATCH/DELETE /order/id responses -> the UDS order panel's fields."""
    orders = (payload or {}).get("orders")
    if isinstance(orders, list):
        orders = orders[0] if orders else {}
    order = (orders or {}).get("Order") or {}
    price = order.get("Price") or {}
    return {
        "order_id": order.get("OrderId"),
        "order_ref": order.get("OrderRef"),
        "status": order.get("Status"),
        "price_status": order.get("PriceStatus"),
        "pricing_name": order.get("PricingDefinitionName"),
        "price_total": _money(price.get("total")),
        "price_breakdown": price_breakdown(price),
        "invoice_id": order.get("InvoiceId"),
        "delivery_by": order.get("DeliveryBy"),
        "created_at": order.get("CreatedTimestamp"),
        "instructions": order.get("Instruction") or "",
        "signature": "adult" if order.get("AdultSignatureRequired") else (
            "signature" if order.get("SignatureRequired") else "none"),
        "address": {
            "company": order.get("LocationName") or "", "address1": order.get("Address1") or "",
            "address2": order.get("Address2") or "", "city": order.get("City") or "",
            "state": order.get("State") or "", "zip": str(order.get("ZipCode") or ""),
            "contact": order.get("ContactName") or "", "phone": str(order.get("Phone") or ""),
            "email": order.get("Email") or "",
        },
        "pods": [
            {"name": p.get("Name"), "at": p.get("Timestamp"), "type": p.get("Type"),
             "latitude": p.get("Latitude"), "longitude": p.get("Longitude"),
             "images": [{"type": i.get("Type"), "value": i.get("Value")}
                        for i in p.get("Images") or [] if isinstance(i, dict)]}
            for p in order.get("PODs") or [] if isinstance(p, dict)
        ],
        "packages": [
            {"barcode": p.get("Barcode"), "weight": p.get("Weight"), "dim_weight": p.get("DimWeight"),
             "piece_number": p.get("PieceNumber")}
            for p in (orders or {}).get("Packages") or [] if isinstance(p, dict)
        ],
    }


# Ship-side destination field -> PATCH /order/id field.
UPDATE_FIELDS = (
    ("address1", "Address1"), ("address2", "Address2"), ("city", "City"),
    ("state", "State"), ("zip", "ZipCode"), ("contact", "ContactName"), ("phone", "Phone"),
    ("instructions", "Instructions"),
)


def build_update(changes):
    """The PATCH body for the fields the packer changed. LocationName is the
    company, or the contact when there is none (as at create)."""
    changes = changes or {}
    order = {}
    for key, field in UPDATE_FIELDS:
        if key not in changes:
            continue
        value = changes[key]
        if key == "zip":
            order[field] = zip_value(value)
        elif key == "phone":
            phone = phone_value(value)
            if str(value or "").strip() and phone is None:
                raise ProviderError("UDS needs a 10-digit US phone number")
            order[field] = phone
        elif key == "state":
            order[field] = _cut(str(value or "").upper(), "State")
        else:
            order[field] = _cut(value, field)
    if "company" in changes or "contact" in changes:
        name = (changes.get("company") or "").strip() or (changes.get("contact") or "").strip()
        if name:
            order["LocationName"] = _cut(name, "LocationName")
    order = {k: v for k, v in order.items() if v is not None}
    if not order:
        raise ProviderError("Nothing to change")
    return {"Order": order}


def _split_box_id(box_id):
    """('123456', 0) from '123456#1'; (id, None) for a plain single-box id."""
    base, _, idx = str(box_id or "").partition("#")
    if not idx:
        return base, None
    try:
        return base, max(int(idx) - 1, 0)
    except ValueError:
        return base, None


def _box_id(base, index, count):
    return str(base) if count == 1 else f"{base}#{index + 1}"


def _label_bytes(label, label_format):
    """A stored label as (bytes, fmt): ZPL formats come back as text, the
    PDF formats as base64."""
    if not label:
        return None
    if str(label_format or "").startswith("LabelZpl"):
        return (label.encode() if isinstance(label, str) else label), "zpl"
    try:
        data = base64.b64decode(label)
    except (ValueError, TypeError):
        return None
    return (data, "pdf") if data.startswith(b"%PDF") else None


def _failed_state(error):
    return ShipmentState(provider_shipment_id=None, label_status=LabelStatus.FAILED,
                         error_message=str(error))


# ---- HTTP ----
def _extract_error(resp):
    try:
        data = resp.json()
    except ValueError:
        text = (resp.text or "").strip()[:300]
        return f"UDS error ({resp.status_code}): {text or 'request failed'}"
    if isinstance(data, dict):
        detail = data.get("message") or data.get("error_description") or data.get("error") or str(data)[:300]
    else:
        detail = str(data)[:300]
    if resp.status_code == 403:
        detail = f"{detail} — check the Client ID, secret and Vendor ID"
    return f"UDS error ({resp.status_code}): {detail}"


def _send(method, path, token, json_body=None, timeout=45):
    try:
        return requests.request(method, f"{BASE_URL}{path}", json=json_body, timeout=timeout,
                                headers={"x-api-key": token, "Accept": "application/json"})
    except requests.RequestException as e:
        raise ProviderError(f"UDS request failed: {e}", status=None)


def _post_token(client_id, secret):
    try:
        resp = requests.post(f"{BASE_URL}/token", timeout=20, headers={"Accept": "application/json"},
                             data={"grant_type": "client_credentials", "client_id": client_id,
                                   "client_secret": secret})
    except requests.RequestException as e:
        raise ProviderError(f"UDS sign-in failed: {e}", status=None)
    if resp.status_code in (400, 401, 403):
        raise ProviderError(f"UDS rejected the Client ID / secret ({resp.status_code})", status=resp.status_code)
    if resp.status_code >= 400:
        raise ProviderError(_extract_error(resp), status=resp.status_code)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not data.get("access_token"):
        raise ProviderError("UDS sign-in returned no access token", status=resp.status_code)
    return data


def _json(resp):
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


class UdsProvider(ShippingProvider):
    platform = "uds"
    label = "UDS"
    modes = ()
    supports_cod = False
    auto_mode = False
    tracks_parcels = True
    edits_orders = True

    # ---- credentials ----
    def _credentials(self):
        client_id = (self.setting("client_id") or "").strip()
        secret = (self.setting("client_secret") or "").strip()
        if not client_id or not secret:
            raise ProviderError("No UDS Client ID / secret configured — set them in Settings")
        return client_id, secret

    def _vendor_id(self):
        vendor = re.sub(r"\D", "", self.setting("vendor_id") or "")
        if not vendor:
            raise ProviderError("No UDS Vendor ID configured — set it in Settings")
        return vendor

    def label_format(self):
        value = (self.setting("label_format") or "").strip()
        return value if value in dict(LABEL_FORMATS) else DEFAULT_LABEL_FORMAT

    def poll_minutes(self):
        value = (self.setting("tracking_poll_minutes") or "").strip()
        if value == "off":
            return 0
        try:
            return max(int(value), 1)
        except ValueError:
            return DEFAULT_POLL_MINUTES

    def _cached_token(self, client_id):
        token = self.setting("access_token")
        try:
            expires = float(self.setting("token_expires_at") or 0)
        except ValueError:
            expires = 0
        if token and self.setting("token_client") == client_id and expires - time.time() > TOKEN_MARGIN_S:
            return token
        return None

    def _forget_token(self):
        db.set_setting(self.setting_key("access_token"), "")
        db.set_setting(self.setting_key("token_expires_at"), "")

    def _token(self):
        """A live access token, fetched under an advisory lock so the
        gunicorn workers don't all sign in at once."""
        client_id, secret = self._credentials()
        token = self._cached_token(client_id)
        if token:
            return token
        lock = f"uds-token:{self.name}"
        db.query("SELECT pg_advisory_lock(hashtext(%s))", (lock,))
        try:
            token = self._cached_token(client_id)  # another worker may have just signed in
            if token:
                return token
            data = _post_token(client_id, secret)
            lifetime = float(data.get("expires_in") or 3600)
            db.set_setting(self.setting_key("access_token"), data["access_token"])
            db.set_setting(self.setting_key("token_expires_at"), str(time.time() + lifetime))
            db.set_setting(self.setting_key("token_client"), client_id)
            return data["access_token"]
        finally:
            db.query("SELECT pg_advisory_unlock(hashtext(%s))", (lock,))

    def _request(self, method, path, json_body=None, timeout=45):
        """One API call. GETs ride through network errors / 5xx, every call
        waits out a 429, and a 403 signs in again once. Order creation goes
        through `_create`, which never retries on its own."""
        retry_recoverable = method.upper() == "GET"
        token = self._token()
        signed_in_again = False
        resp = None
        for attempt in range(4):
            try:
                resp = _send(method, path, token, json_body, timeout)
            except ProviderError:
                if retry_recoverable and attempt < 3:
                    time.sleep(min(1.5 * (attempt + 1), 10))
                    continue
                raise
            if resp.status_code == 403 and not signed_in_again:
                signed_in_again = True
                self._forget_token()
                token = self._token()
                continue
            if resp.status_code == 429:
                time.sleep(min(1.0 * (attempt + 1), 5))
                continue
            if retry_recoverable and resp.status_code >= 500 and attempt < 3:
                time.sleep(min(1.5 * (attempt + 1), 10))
                continue
            break
        if resp.status_code >= 400:
            raise ProviderError(_extract_error(resp), status=resp.status_code)
        data = _json(resp)
        if isinstance(data, dict) and data.get("success") is False:
            raise ProviderError(f"UDS: {data.get('message') or 'request refused'}", status=422)
        return data

    # ---- drafts (the local half of a UDS order) ----
    def _save_draft(self, body, quote, ref):
        draft_id = DRAFT_PREFIX + uuid.uuid4().hex[:12]
        db.execute(
            "INSERT INTO uds_drafts (draft_id, provider, body, quote, order_ref) VALUES (%s, %s, %s, %s, %s)",
            (draft_id, self.name, json.dumps(body), json.dumps(quote), ref),
        )
        # Unbought drafts are never billed; keep the table from growing forever.
        db.execute("DELETE FROM uds_drafts WHERE order_id IS NULL AND created_at < now() - interval '30 days'")
        return draft_id

    def _draft(self, draft_id):
        return db.query("SELECT * FROM uds_drafts WHERE draft_id = %s AND provider = %s",
                        (draft_id, self.name), one=True)

    def _draft_for_order(self, order_id):
        return db.query("SELECT * FROM uds_drafts WHERE order_id = %s AND provider = %s",
                        (str(order_id), self.name), one=True)

    def _draft_for_base(self, base):
        return self._draft(base) if base.startswith(DRAFT_PREFIX) else self._draft_for_order(base)

    def _order_id_for(self, base):
        if not base.startswith(DRAFT_PREFIX):
            return base
        return (self._draft(base) or {}).get("order_id")

    # ---- rating ----
    def clean_ship_options(self, raw):
        return clean_options(raw)

    def _quote(self, body):
        data = self._request("POST", f"/order/quote/{self._vendor_id()}", json_body=body, timeout=60)
        price = (data or {}).get("Price") or {}
        total = _money(price.get("total"))
        if not total or total <= 0:
            raise ProviderError("UDS returned no price for this shipment")
        return total, price_breakdown(price)

    def create_draft_shipments(self, destination, parcels, items, options=None):
        options = options or {}
        ref = order_ref(options.get("reference"), uuid.uuid4().hex[:12])
        body = build_order(self.origin(), destination, parcels, options, ref, self.label_format())
        total, breakdown = self._quote(body)
        draft_id = self._save_draft(body, {"total": total, "boxes": len(parcels), "breakdown": breakdown}, ref)
        drafts = [DraftShipment(_box_id(draft_id, i, len(parcels))) for i in range(len(parcels))]
        when = (options.get("provider") or {}).get("delivery_date")
        days = business_days(central_today(), date.fromisoformat(when)) if when else 1
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
            breakdown=breakdown,
        )
        warnings = []
        if when:
            warnings.append(f"UDS delivery date: {when}")
        return drafts, [rate], warnings

    def get_excluded_service_ids(self):
        return set()

    def set_excluded_service_ids(self, ids):
        return []

    # ---- label lifecycle ----
    def _orders_by_ref(self, ref):
        """Tracking numbers UDS holds for an OrderRef — how a create whose
        response was lost is recognized."""
        try:
            data = self._request("POST", "/tracking/orderRef", json_body={"OrderRef": ref})
        except ProviderError as e:
            if e.status in (400, 404, 422):
                return []
            raise
        packages = data if isinstance(data, list) else [data] if data else []
        return [((p or {}).get("PackageTrackingInfo") or {}).get("TrackingNumber")
                for p in packages if ((p or {}).get("PackageTrackingInfo") or {}).get("TrackingNumber")]

    def _lost_label(self, ref, numbers):
        return _failed_state(
            f"UDS created order {ref} (tracking {', '.join(numbers)}) but its label response was lost — "
            "print or void it in the UDS portal, then undo this shipment")

    def _create(self, draft):
        """POST the create once. Returns the created OrderObject."""
        token = self._token()
        resp = _send("POST", f"/order/create/{self._vendor_id()}", token, [draft["body"]], timeout=90)
        if resp.status_code == 403:
            self._forget_token()
            resp = _send("POST", f"/order/create/{self._vendor_id()}", self._token(), [draft["body"]], timeout=90)
        if resp.status_code >= 400:
            raise ProviderError(_extract_error(resp), status=resp.status_code)
        data = _json(resp)
        orders = (data or {}).get("orders") or []
        order = orders[0] if orders and isinstance(orders[0], dict) else None
        if not order or not (order.get("Order") or {}).get("OrderId"):
            raise ProviderError(f"UDS created no order: {(data or {}).get('message') or 'no order returned'}",
                                status=422)
        return order

    def _place_order(self, draft):
        """The draft's order — created at most once. Returns (draft row, None)
        once it exists or (None, ShipmentState) when there is none yet."""
        if draft.get("order_id"):
            return draft, None
        ref = draft.get("order_ref") or (draft["body"].get("Order") or {}).get("OrderRef")
        resend = bool(draft.get("create_started_at"))
        if resend:
            age = (datetime.now(timezone.utc) - draft["create_started_at"]).total_seconds()
            if age > LOST_CREATE_MAX_AGE_S:
                numbers = self._orders_by_ref(ref)
                if numbers:
                    return None, self._lost_label(ref, numbers)
                return None, _failed_state(
                    f"UDS order {ref} was sent over 12 hours ago with no answer — check the UDS portal, "
                    "then get rates again")
        db.execute("UPDATE uds_drafts SET create_started_at = now() WHERE draft_id = %s", (draft["draft_id"],))
        try:
            order = self._create(draft)
        except ProviderError as e:
            if e.recoverable:
                raise  # it may exist — the next attempt re-sends; UDS refuses a duplicate OrderRef
            if resend:
                numbers = self._orders_by_ref(ref)
                if numbers:
                    return None, self._lost_label(ref, numbers)
            db.execute("UPDATE uds_drafts SET create_started_at = NULL WHERE draft_id = %s",
                       (draft["draft_id"],))
            return None, _failed_state(e)
        order_id = str(order["Order"]["OrderId"])
        by_piece = {str(p.get("PieceNumber") or ""): p for p in order.get("Packages") or [] if isinstance(p, dict)}
        returned = [p for p in order.get("Packages") or [] if isinstance(p, dict)]
        packages = []
        for i, sent in enumerate(draft["body"]["Packages"]):
            got = by_piece.get(sent["PieceNumber"]) or (returned[i] if i < len(returned) else {})
            packages.append({"barcode": got.get("Barcode") or "", "label": got.get("Label") or ""})
        db.execute(
            "UPDATE uds_drafts SET order_id = %s, packages = %s WHERE draft_id = %s",
            (order_id, json.dumps(packages), draft["draft_id"]),
        )
        return {**draft, "order_id": order_id, "packages": packages}, None

    def _box_states(self, draft, box_ids):
        order_id = draft["order_id"]
        packages = draft.get("packages") or []
        total = (draft.get("quote") or {}).get("total")
        count = len(draft["body"]["Packages"])
        cost = round(float(total) / count, 2) if total and count else None
        out = {}
        for bid in box_ids:
            _, index = _split_box_id(bid)
            index = index or 0
            package = packages[index] if index < len(packages) else {}
            if not package.get("barcode"):
                out[bid] = _failed_state(f"UDS order {order_id} returned no barcode for box {index + 1}")
                continue
            out[bid] = ShipmentState(
                provider_shipment_id=_box_id(order_id, index, count),
                label_status=LabelStatus.READY,
                tracking_numbers=[package["barcode"]],
                courier_name=COURIER_NAME,
                courier_umbrella_name=UMBRELLA_NAME,
                cost=cost,
                raw={"order_id": order_id, "index": index, "draft_id": draft["draft_id"]},
            )
        return out

    def _by_base(self, provider_shipment_ids):
        groups = {}
        for bid in provider_shipment_ids:
            groups.setdefault(_split_box_id(bid)[0], []).append(bid)
        return groups

    def buy_labels(self, provider_shipment_ids, service_id):
        out = {}
        for base, bids in self._by_base(provider_shipment_ids).items():
            try:
                draft = self._draft_for_base(base)
                if not draft:
                    raise ProviderError("UDS quote not found — get rates again")
                draft, state = self._place_order(draft)
                if state is not None:
                    out.update({bid: state for bid in bids})
                    continue
                out.update(self._box_states(draft, bids))
            except ProviderError as e:
                out.update({bid: e for bid in bids})
        return out

    def poll_shipments(self, provider_shipment_ids, service_id=None):
        out = {}
        for base, bids in self._by_base(provider_shipment_ids).items():
            draft = self._draft_for_base(base)
            if not draft or not draft.get("order_id"):
                # Not created yet (or its create response was lost): the buy
                # loop re-issues buy_labels, which creates or re-sends it.
                out.update({bid: ShipmentState(provider_shipment_id=None, label_status=LabelStatus.NOT_CREATED)
                            for bid in bids})
                continue
            out.update(self._box_states(draft, bids))
        return out

    def fetch_labels(self, state):
        raw = state.raw or {}
        draft = (self._draft(raw["draft_id"]) if raw.get("draft_id")
                 else self._draft_for_order(raw.get("order_id")) if raw.get("order_id") else None)
        if not draft:
            return []
        packages = draft.get("packages") or []
        index = raw.get("index") or 0
        if index >= len(packages):
            return []
        fmt = (draft["body"].get("Order") or {}).get("PrintLabelFormat")
        doc = _label_bytes(packages[index].get("label"), fmt)
        return [doc] if doc else []

    def cancel_all(self, provider_shipment_ids):
        errors = []
        bases = list(dict.fromkeys(_split_box_id(i)[0] for i in provider_shipment_ids if i))
        for base in bases:
            order_id = self._order_id_for(base)
            if not order_id:
                if base.startswith(DRAFT_PREFIX):
                    db.execute("DELETE FROM uds_drafts WHERE draft_id = %s AND order_id IS NULL "
                               "AND create_started_at IS NULL", (base,))
                continue  # never created — nothing to void
            try:
                self._request("DELETE", f"/order/id/{order_id}")
            except ProviderError as e:
                if e.status == 404:
                    continue
                errors.append(f"UDS order {order_id}: {e}")
        return errors

    def get_raw_shipment(self, provider_shipment_id):
        base, _ = _split_box_id(provider_shipment_id)
        order_id = self._order_id_for(base)
        if not order_id:
            return {"draft": self._draft(base)}
        return self._request("GET", f"/order/id/{order_id}")

    def tracking_url(self, number):
        return TRACKING_URL.format(number=number) if number else None

    # ---- after the purchase: order details, edits, tracking ----
    def _require_order(self, provider_shipment_id):
        order_id = self._order_id_for(_split_box_id(provider_shipment_id)[0])
        if not order_id:
            raise ProviderError("This UDS order was never created")
        return order_id

    def order_details(self, provider_shipment_id):
        return normalize_order(self._request("GET", f"/order/id/{self._require_order(provider_shipment_id)}"))

    def update_order(self, provider_shipment_id, changes):
        order_id = self._require_order(provider_shipment_id)
        try:
            data = self._request("PATCH", f"/order/id/{order_id}", json_body=build_update(changes))
        except ProviderError as e:
            if e.status == 409:
                raise ProviderError(f"UDS no longer allows changes to order {order_id}: {e}", status=409)
            raise
        return normalize_order(data)

    def track(self, numbers):
        """Every event UDS holds for these barcodes (does not consume the
        pending feed). Returns (events, errors)."""
        events, errors = [], []
        for number in dict.fromkeys(n for n in numbers if n):
            try:
                data = self._request("POST", "/tracking", json_body={"TrackingNumber": number})
            except ProviderError as e:
                errors.append(f"{number}: {e}")
                continue
            events.extend(normalize_tracking(data if isinstance(data, list) else [data]))
        return events, errors

    def pull_tracking(self):
        """New events since the last pull. UDS marks them processed as it
        returns them, so the caller must store them."""
        data = self._request("GET", "/tracking/pending", timeout=90)
        return normalize_tracking(data if isinstance(data, list) else [data] if data else [])

    # ---- settings surface ----
    def list_item_categories(self):
        return []

    def list_courier_services(self):
        return [{"id": SERVICE_ID, "umbrella_name": UMBRELLA_NAME, "name": COURIER_NAME}]

    def active_mode(self):
        return ""

    def test_connection(self, mode=None, token=None, username=None):
        client_id = (username or "").strip() or (self.setting("client_id") or "").strip()
        secret = token if token and token != MASK else (self.setting("client_secret") or "").strip()
        if not client_id or not secret:
            raise ProviderError("Enter the UDS Client ID and Client secret")
        access = _post_token(client_id, secret)["access_token"]
        vendor = re.sub(r"\D", "", self.setting("vendor_id") or "")
        if not vendor:
            return {"ok": True, "account": "Signed in — save the Vendor ID, then test again to check it"}
        origin = self.origin()
        if missing_origin_fields(origin, ORIGIN_REQUIRED):
            return {"ok": True, "account": f"Signed in (vendor {vendor}) — fill in the ship-from address to test a quote"}
        dest = {"company": origin.get("origin_company"), "address1": origin.get("origin_address1"),
                "address2": origin.get("origin_address2"), "city": origin.get("origin_city"),
                "state": origin.get("origin_state"), "zip": origin.get("origin_zip")}
        body = build_order(origin, dest, [{"weight": 1}], {}, order_ref("TEST", uuid.uuid4().hex[:8]),
                           self.label_format())
        resp = _send("POST", f"/order/quote/{vendor}", access, body, timeout=30)
        if resp.status_code in (401, 403, 404):
            raise ProviderError(f"Signed in, but UDS refused Vendor ID {vendor} ({resp.status_code})")
        if resp.status_code >= 400:
            raise ProviderError(_extract_error(resp))
        total = _money(((_json(resp) or {}).get("Price") or {}).get("total"))
        sample = f" · 1 lb to your own address quotes ${total:.2f}" if total else ""
        return {"ok": True, "account": f"Vendor {vendor} connected{sample}"}

    def descriptor(self):
        last_pull = self.setting("tracking_last_pull")
        last_error = self.setting("tracking_last_error")
        notice = None
        if last_error:
            notice = f"Last tracking pull failed: {last_error}"
        elif last_pull:
            try:
                when = datetime.fromtimestamp(float(last_pull), CENTRAL).strftime("%-m/%-d %-I:%M %p")
                notice = f"Tracking last pulled {when}"
            except ValueError:
                notice = None
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
                {"key": self.setting_key("client_id"), "label": "Client ID", "type": "text",
                 "test_param": "username"},
                {"key": self.setting_key("client_secret"), "label": "Client secret", "type": "secret"},
                {"key": self.setting_key("vendor_id"), "label": "Vendor ID", "type": "text",
                 "hint": "Your numeric UDS vendor number."},
                {"key": self.setting_key("label_format"), "label": "Label format", "type": "select",
                 "options": [{"value": "", "label": f"{LABEL_FORMATS[0][1]} (default)"}]
                            + [{"value": v, "label": l} for v, l in LABEL_FORMATS[1:]],
                 "hint": "ZPL formats print straight to a Zebra. Avery 5164 is a letter sheet."},
                {"key": self.setting_key("tracking_poll_minutes"), "label": "Tracking updates",
                 "type": "select",
                 "options": [{"value": "", "label": "Every 15 minutes (default)"},
                             {"value": "5", "label": "Every 5 minutes"},
                             {"value": "30", "label": "Every 30 minutes"},
                             {"value": "60", "label": "Every hour"},
                             {"value": "off", "label": "Off"}],
                 "hint": "New delivery events are pulled from UDS in the background."},
            ],
            "notice": notice,
            "test_endpoint": f"/api/providers/{self.name}/test",
            "supports": {"service_exclusions": False},
            **origin_descriptor(self.name),
        }

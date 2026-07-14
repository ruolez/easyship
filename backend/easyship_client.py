import base64
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial

import requests

import config
import db

# Easyship limits requests per second; parallel per-box calls must be spaced
# out. Processing still overlaps — only the launches are staggered.
_throttle_lock = threading.Lock()
_next_slot = 0.0
MIN_REQUEST_INTERVAL = 0.6


def _throttle():
    global _next_slot
    with _throttle_lock:
        now = time.monotonic()
        slot = max(now, _next_slot)
        _next_slot = slot + MIN_REQUEST_INTERVAL
    wait = slot - now
    if wait > 0:
        time.sleep(wait)

LB_TO_KG = 0.45359237
IN_TO_CM = 2.54


class EasyshipError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status

    @property
    def recoverable(self):
        """Timeouts and gateway 5xx — the request may still have succeeded
        on Easyship's side."""
        return self.status is None or self.status >= 500


def _base_url():
    mode = db.get_setting("easyship_mode") or "sandbox"
    return config.EASYSHIP_BASE_URLS[mode]


def _token():
    mode = db.get_setting("easyship_mode") or "sandbox"
    token = db.get_setting(f"easyship_{mode}_token")
    if not token:
        raise EasyshipError(f"No Easyship {mode} token configured — set it in Settings")
    return token


def _auth():
    """Resolve (base_url, token) inside the request context — worker threads
    have no Flask context, so parallel helpers capture this first."""
    return _base_url(), _token()


def _request(method, path, json_body=None, params=None, timeout=45, auth=None):
    base_url, token = auth or _auth()
    url = f"{base_url}{path}"
    # GETs are idempotent, so we ride through transient gateway timeouts / 5xx
    # (e.g. Cloudflare 522) by retrying them. Money-sensitive writes (label
    # purchase) are deliberately NOT retried here — the group-buy loop re-issues
    # those only after re-checking shipment state, so a lost write can never
    # double-charge.
    retry_recoverable = method.upper() == "GET"
    resp = None
    last_exc = None
    for attempt in range(4):
        _throttle()
        try:
            resp = requests.request(
                method,
                url,
                json=json_body,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=timeout,
            )
        except requests.RequestException as e:
            if retry_recoverable and attempt < 3:
                last_exc = e
                time.sleep(min(1.5 * (attempt + 1), 10))
                continue
            raise EasyshipError(f"Easyship request failed: {e}", status=None)
        if resp.status_code == 429:
            try:
                retry_after = float(resp.headers.get("Retry-After") or 0)
            except ValueError:
                retry_after = 0
            time.sleep(min(retry_after or 1.5 * (attempt + 1), 15))
            continue
        if retry_recoverable and resp.status_code >= 500 and attempt < 3:
            time.sleep(min(1.5 * (attempt + 1), 10))
            continue
        break
    if resp is None:
        raise EasyshipError(f"Easyship request failed: {last_exc}", status=None)
    if resp.status_code >= 400:
        raise EasyshipError(_extract_error(resp), status=resp.status_code)
    return resp.json()


def _extract_error(resp):
    body = resp.text or ""
    if body.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        return (
            f"Easyship did not respond in time (gateway error {resp.status_code}). "
            "The request may still have gone through on their side."
        )
    try:
        data = resp.json()
        err = data.get("error") or {}
        message = err.get("message") or str(err) or body[:300]
        details = err.get("details")
        if details:
            message += " — " + "; ".join(str(d) for d in details)
        return f"Easyship error ({resp.status_code}): {message}"
    except ValueError:
        return f"Easyship error ({resp.status_code}): {body[:300]}"


def _clean(d):
    """Easyship's OpenAPI validation rejects explicit nulls — omit empty fields."""
    return {k: v for k, v in d.items() if v not in (None, "")}


ORIGIN_REQUIRED = {
    "origin_company": "Company",
    "origin_address1": "Address 1",
    "origin_city": "City",
    "origin_state": "State",
    "origin_zip": "ZIP",
    "origin_phone": "Phone",
    "origin_email": "Email",
}


def origin_address():
    missing = [label for key, label in ORIGIN_REQUIRED.items() if not (db.get_setting(key) or "").strip()]
    if missing:
        raise EasyshipError(
            "Origin address is incomplete — fill in on the Settings page: " + ", ".join(missing)
        )
    return _clean({
        "company_name": db.get_setting("origin_company") or None,
        "contact_name": db.get_setting("origin_contact") or db.get_setting("origin_company") or "Shipping",
        "contact_phone": db.get_setting("origin_phone") or None,
        "contact_email": db.get_setting("origin_email") or None,
        "line_1": db.get_setting("origin_address1"),
        "line_2": db.get_setting("origin_address2") or None,
        "city": db.get_setting("origin_city"),
        "state": db.get_setting("origin_state"),
        "postal_code": db.get_setting("origin_zip"),
        "country_alpha2": "US",
    })


def build_destination(dest):
    # Easyship requires a destination email; fall back to the origin email
    # (shipment notifications then go to us instead of the customer).
    email = (dest.get("email") or "").strip() or (db.get_setting("origin_email") or "").strip()
    return _clean({
        "company_name": dest.get("company") or None,
        "contact_name": dest.get("contact") or dest.get("company") or "Recipient",
        "contact_phone": dest.get("phone") or None,
        "contact_email": email or None,
        "line_1": dest.get("address1"),
        "line_2": dest.get("address2") or None,
        "city": dest.get("city"),
        "state": (dest.get("state") or "").strip().upper(),
        "postal_code": (dest.get("zip") or "").strip(),
        "country_alpha2": dest.get("country") or "US",
    })


def build_parcels(parcels, items):
    """parcels: [{length,width,height (in), weight (lb)}]; items: [{description, quantity, value, sku?, weight?}]"""
    category = (db.get_setting("default_item_category") or "dry_food_supplements").strip()
    es_parcels = []
    for i, p in enumerate(parcels):
        box_dims = None
        if p.get("length") and p.get("width") and p.get("height"):
            box_dims = {
                "length": round(float(p["length"]) * IN_TO_CM, 2),
                "width": round(float(p["width"]) * IN_TO_CM, 2),
                "height": round(float(p["height"]) * IN_TO_CM, 2),
            }
        # Easyship requires per-item dimensions and a category even for domestic
        # shipments; the box (or a 1-inch cube) stands in for item size.
        item_dims = box_dims or {"length": 2.54, "width": 2.54, "height": 2.54}

        parcel_items = []
        if i == 0:
            for item in items or []:
                parcel_items.append(_clean({
                    "description": (item.get("description") or "Merchandise")[:100],
                    "sku": item.get("sku") or None,
                    "quantity": int(item.get("quantity") or 1),
                    "declared_currency": "USD",
                    "declared_customs_value": float(item.get("value") or 1),
                    "actual_weight": round(float(item.get("weight") or 0) * LB_TO_KG, 4) or None,
                    "origin_country_alpha2": "US",
                    "category": category,
                    "dimensions": item_dims,
                }))
        if not parcel_items:
            parcel_items = [{
                "description": "Merchandise",
                "quantity": 1,
                "declared_currency": "USD",
                "declared_customs_value": 1.0,
                "origin_country_alpha2": "US",
                "category": category,
                "dimensions": item_dims,
            }]
        weight_lb = float(p.get("weight") or 0)
        es_parcel = {
            "total_actual_weight": round(weight_lb * LB_TO_KG, 4),
            "items": parcel_items,
        }
        if box_dims:
            # ParcelCreate.box takes flat dimensions (unlike box objects in
            # responses, which nest them under outer_dimensions)
            es_parcel["box"] = box_dims
        es_parcels.append(es_parcel)
    return es_parcels


def list_item_categories():
    data = _request("GET", "/item_categories", params={"perPage": 50})
    return [
        {"slug": c.get("slug"), "name": c.get("name") or c.get("slug")}
        for c in data.get("item_categories") or []
        if c.get("slug")
    ]


def create_shipments(destination, parcels, items):
    """One Easyship shipment PER BOX, created in parallel. Couriers like USPS
    don't support true multi-parcel shipments — separate shipments give every
    box its own label and tracking number, and parallel requests keep it fast.
    Returns shipments in box order."""
    auth = _auth()
    origin = origin_address()
    dest = build_destination(destination)
    bodies = []
    for i, parcel in enumerate(parcels):
        bodies.append({
            "origin_address": origin,
            "destination_address": dest,
            "incoterms": "DDU",
            # order items ride on box 1 for customs; other boxes get a stub
            "parcels": build_parcels([parcel], items if i == 0 else []),
        })

    if len(bodies) == 1:
        data = _request("POST", "/shipments", json_body=bodies[0], timeout=60, auth=auth)
        return [data["shipment"]]

    results = [None] * len(bodies)
    with ThreadPoolExecutor(max_workers=min(len(bodies), 6)) as pool:
        futures = {
            pool.submit(partial(_request, "POST", "/shipments",
                                json_body=body, timeout=60, auth=auth)): i
            for i, body in enumerate(bodies)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result()["shipment"]
            except EasyshipError as e:
                results[i] = e

    errors = [r for r in results if isinstance(r, EasyshipError)]
    if errors:
        # don't leave orphan shipments behind for the boxes that succeeded
        created = [r["easyship_shipment_id"] for r in results if not isinstance(r, EasyshipError)]
        cancel_all(created)
        raise errors[0]
    return results


def buy_labels(shipment_ids, courier_service_id):
    """Purchase labels for all shipments in parallel.
    Returns {shipment_id: shipment_object_or_EasyshipError}."""
    auth = _auth()
    out = {}
    with ThreadPoolExecutor(max_workers=min(len(shipment_ids), 6)) as pool:
        futures = {
            pool.submit(partial(_request, "POST", f"/shipments/{sid}/label",
                                json_body={"courier_service_id": courier_service_id},
                                timeout=90, auth=auth)): sid
            for sid in shipment_ids
        }
        for future in as_completed(futures):
            sid = futures[future]
            try:
                out[sid] = future.result()["shipment"]
            except EasyshipError as e:
                out[sid] = e
    return out


def get_shipments(shipment_ids):
    """Fetch several shipments in parallel.
    Returns {shipment_id: shipment_object_or_EasyshipError}."""
    auth = _auth()
    out = {}
    with ThreadPoolExecutor(max_workers=min(len(shipment_ids), 6)) as pool:
        futures = {
            pool.submit(partial(_request, "GET", f"/shipments/{sid}", auth=auth)): sid
            for sid in shipment_ids
        }
        for future in as_completed(futures):
            sid = futures[future]
            try:
                out[sid] = future.result()["shipment"]
            except EasyshipError as e:
                out[sid] = e
    return out


def get_shipment(easyship_shipment_id, pdf_4x6=False):
    params = {"format": "PDF", "label": "4x6"} if pdf_4x6 else None
    data = _request("GET", f"/shipments/{easyship_shipment_id}", params=params)
    return data["shipment"]


def cancel_shipment(easyship_shipment_id):
    return _request("POST", f"/shipments/{easyship_shipment_id}/cancel")


def cancel_all(shipment_ids):
    """Cancel several shipments; already-cancelled ones don't count as errors.
    Returns a list of error strings."""
    errors = []
    for sid in [s for s in shipment_ids if s]:
        try:
            cancel_shipment(sid)
        except EasyshipError as e:
            try:
                current = get_shipment(sid)
                if current.get("label_state") in ("voided", "cancelled") or \
                   current.get("shipment_state") in ("cancelled", "abandoned"):
                    continue
            except EasyshipError:
                pass
            errors.append(f"{sid}: {e}")
    return errors


def extract_tracking_numbers(shipment):
    """All tracking numbers. Per-box numbers live at parcels[].tracking_number
    (couriers that issue them); trackings[] only carries the shipment-level
    lead number."""
    numbers = []

    def add(n):
        if n and n not in numbers:
            numbers.append(n)

    for parcel in shipment.get("parcels") or []:
        add(parcel.get("tracking_number"))
    for tracking in shipment.get("trackings") or []:
        add(tracking.get("tracking_number"))
    add(shipment.get("tracking_number"))
    return numbers


def count_label_pages(docs):
    """Printable pages across label documents — a 3-box shipment may arrive as
    one 3-page PDF or three 1-page documents."""
    import io
    total = 0
    for data, fmt in docs:
        if fmt == "pdf":
            try:
                from pypdf import PdfReader
                total += len(PdfReader(io.BytesIO(data)).pages)
            except Exception:
                total += 1
        elif fmt == "zpl":
            total += max(data.count(b"^XA"), 1)
        else:
            total += 1
    return total


def extract_tracking_number(shipment):
    numbers = extract_tracking_numbers(shipment)
    return numbers[0] if numbers else None


def sniff_label_format(data, declared="pdf"):
    """The declared document format can be 'url' or wrong — trust the bytes."""
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"\x89PNG"):
        return "png"
    if data[:3] == b"^XA" or data[:16].lstrip()[:3] == b"^XA":
        return "zpl"
    return declared if declared in ("pdf", "png", "zpl") else "pdf"


def extract_label_documents(shipment):
    """ALL label documents as [(bytes, format)] — multi-box shipments return
    one label per parcel (multiple base64 strings and/or documents)."""
    docs = []
    for doc in shipment.get("shipping_documents") or []:
        if doc.get("category") != "label":
            continue
        fmt = (doc.get("format") or "pdf").lower()
        for encoded in doc.get("base64_encoded_strings") or []:
            data = base64.b64decode(encoded)
            docs.append((data, sniff_label_format(data, fmt)))
        if not doc.get("base64_encoded_strings") and doc.get("url"):
            resp = requests.get(doc["url"], timeout=30)
            if resp.ok:
                docs.append((resp.content, sniff_label_format(resp.content, fmt)))
    return docs


def _image_to_pdf(data):
    """Wrap a raster label (PNG/JPG) in a single-page PDF at its native size,
    honoring the image's embedded DPI so a 4x6 label stays 4x6."""
    import io
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    dpi = img.info.get("dpi")
    resolution = float(dpi[0]) if dpi and dpi[0] else 203.0  # thermal labels are 203 DPI
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=resolution)
    return buf.getvalue()


def _merge_to_pdf(docs):
    """One multi-page PDF, one label per page — PDF pages copied as-is, raster
    labels converted first. Handles all-PDF, all-image, and mixed sets."""
    import io
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for data, fmt in docs:
        page_pdf = data if fmt == "pdf" else _image_to_pdf(data)
        for page in PdfReader(io.BytesIO(page_pdf)).pages:
            writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def merge_label_documents(docs):
    """Combine per-box labels into one printable file. PDF/PNG labels merge into
    a single multi-page PDF (one label per page); ZPL concatenates. Returns
    (bytes, format) or (None, None)."""
    if not docs:
        return None, None
    if len(docs) == 1:
        return docs[0]
    formats = {fmt for _, fmt in docs}
    if formats == {"zpl"}:
        return b"\n".join(data for data, _ in docs), "zpl"
    if formats <= {"pdf", "png"}:
        try:
            return _merge_to_pdf(docs), "pdf"
        except Exception:
            return docs[0]  # never drop the whole job if conversion fails
    return docs[0]


def extract_label_document(shipment):
    """Returns (bytes, format) of the combined label document, or (None, None)."""
    return merge_label_documents(extract_label_documents(shipment))

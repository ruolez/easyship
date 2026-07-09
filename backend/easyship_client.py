import base64

import requests

import config
import db

LB_TO_KG = 0.45359237
IN_TO_CM = 2.54


class EasyshipError(Exception):
    pass


def _base_url():
    mode = db.get_setting("easyship_mode", "sandbox")
    return config.EASYSHIP_BASE_URLS[mode]


def _token():
    mode = db.get_setting("easyship_mode", "sandbox")
    token = db.get_setting(f"easyship_{mode}_token")
    if not token:
        raise EasyshipError(f"No Easyship {mode} token configured — set it in Settings")
    return token


def _request(method, path, json_body=None, params=None, timeout=45):
    url = f"{_base_url()}{path}"
    try:
        resp = requests.request(
            method,
            url,
            json=json_body,
            params=params,
            headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise EasyshipError(f"Easyship request failed: {e}")
    if resp.status_code >= 400:
        raise EasyshipError(_extract_error(resp))
    return resp.json()


def _extract_error(resp):
    try:
        data = resp.json()
        err = data.get("error") or {}
        message = err.get("message") or str(err) or resp.text[:300]
        details = err.get("details")
        if details:
            message += " — " + "; ".join(str(d) for d in details)
        return f"Easyship error ({resp.status_code}): {message}"
    except ValueError:
        return f"Easyship error ({resp.status_code}): {resp.text[:300]}"


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
            es_parcel["box"] = {"outer_dimensions": box_dims, "weight": 0}
        es_parcels.append(es_parcel)
    return es_parcels


def list_item_categories():
    data = _request("GET", "/item_categories", params={"perPage": 50})
    return [
        {"slug": c.get("slug"), "name": c.get("name") or c.get("slug")}
        for c in data.get("item_categories") or []
        if c.get("slug")
    ]


def create_shipment(destination, parcels, items):
    body = {
        "origin_address": origin_address(),
        "destination_address": build_destination(destination),
        "incoterms": "DDU",
        "parcels": build_parcels(parcels, items),
    }
    data = _request("POST", "/shipments", json_body=body)
    return data["shipment"]


def buy_label(easyship_shipment_id, courier_service_id):
    data = _request(
        "POST",
        f"/shipments/{easyship_shipment_id}/label",
        json_body={"courier_service_id": courier_service_id},
        timeout=60,
    )
    return data["shipment"]


def get_shipment(easyship_shipment_id, pdf_4x6=False):
    params = {"format": "PDF", "label": "4x6"} if pdf_4x6 else None
    data = _request("GET", f"/shipments/{easyship_shipment_id}", params=params)
    return data["shipment"]


def cancel_shipment(easyship_shipment_id):
    return _request("POST", f"/shipments/{easyship_shipment_id}/cancel")


def extract_tracking_number(shipment):
    for tracking in shipment.get("trackings") or []:
        if tracking.get("tracking_number"):
            return tracking["tracking_number"]
    return shipment.get("tracking_number")


def extract_label_document(shipment):
    """Returns (bytes, format) of the label document, or (None, None)."""
    for doc in shipment.get("shipping_documents") or []:
        if doc.get("category") != "label":
            continue
        fmt = (doc.get("format") or "pdf").lower()
        if doc.get("base64_encoded_strings"):
            return base64.b64decode(doc["base64_encoded_strings"][0]), fmt
        if doc.get("url"):
            resp = requests.get(doc["url"], timeout=30)
            if resp.ok:
                return resp.content, fmt
    return None, None

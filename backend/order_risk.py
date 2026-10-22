"""Shopify's order risk evaluation for the Pick lists fraud window: risk
level, recommendation and facts, the name on the card checked against the
order's billing and shipping names, and where the buyer's IP is. Ported from
the Shopify Automation app so both show the same verdicts."""
import ipaddress
import os
import re
import time
import unicodedata

import requests

LEVEL_SEVERITY = {"NONE": 0, "PENDING": 1, "LOW": 2, "MEDIUM": 3, "HIGH": 4}

# Worst first.
CARDHOLDER_MATCHES = ("MISMATCH", "LAST_NAME_ONLY", "MATCH")
CARD_PAYMENT_KINDS = ("SALE", "AUTHORIZATION")
NAME_AFFIXES = {"mr", "mrs", "ms", "miss", "dr", "jr", "sr", "ii", "iii", "iv"}


def risk_level(risk):
    """The most severe level across the order's assessments (Shopify's own and
    any third-party fraud apps), or None when none is known."""
    levels = [(a or {}).get("riskLevel") for a in ((risk or {}).get("assessments") or [])]
    known = [level for level in levels if level in LEVEL_SEVERITY]
    return max(known, key=LEVEL_SEVERITY.__getitem__) if known else None


def _name_tokens(name):
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"['’`.]", "", text)
    return [token for token in re.split(r"[^a-z0-9]+", text) if token and token not in NAME_AFFIXES]


def _first_last_match(card, order):
    if not set(card[1:] or card) & set(order[1:] or order):
        return "MISMATCH"
    first_card, first_order = card[0], order[0]
    initial = min(len(first_card), len(first_order)) == 1
    if first_card == first_order or (initial and first_card[0] == first_order[0]):
        return "MATCH"
    return "LAST_NAME_ONLY"


def cardholder_name_match(card_name, order_name):
    """MATCH when first and last names agree (initials, middle names, accents,
    punctuation and "LAST/FIRST" card order tolerated), LAST_NAME_ONLY when
    only a surname is shared, MISMATCH otherwise; None when either is blank."""
    card, order = _name_tokens(card_name), _name_tokens(order_name)
    if not card or not order:
        return None
    results = {_first_last_match(card, order), _first_last_match(card[::-1], order)}
    return next(match for match in reversed(CARDHOLDER_MATCHES) if match in results)


def _address_name(address):
    address = address or {}
    return " ".join(part for part in (address.get("firstName"), address.get("lastName")) if part) or None


def cardholder_check(order):
    """Each card that paid takes its best match across the billing and
    shipping names, and the worst card decides. None when no card name or no
    order name is known."""
    card_names = list(dict.fromkeys(
        ((t.get("paymentDetails") or {}).get("name") or "").strip()
        for t in (order.get("transactions") or [])
        if isinstance(t, dict) and t.get("kind") in CARD_PAYMENT_KINDS and t.get("status") == "SUCCESS"
    ))
    card_names = [name for name in card_names if name]
    billing_name = _address_name(order.get("billingAddress"))
    shipping_name = _address_name(order.get("shippingAddress"))
    order_names = [name for name in (billing_name, shipping_name) if name]
    if not card_names or not order_names:
        return None

    def best(card_name):
        matches = {cardholder_name_match(card_name, name) for name in order_names}
        return next((m for m in reversed(CARDHOLDER_MATCHES) if m in matches), "MISMATCH")

    card_matches = {best(name) for name in card_names}
    return {
        "status": next(match for match in CARDHOLDER_MATCHES if match in card_matches),
        "card_names": card_names,
        "billing_name": billing_name,
        "shipping_name": shipping_name,
    }


def order_risk(order):
    """What Shopify's "Order risk evaluation" panel shows; the caller fills in
    the IP location, which Shopify's API does not provide."""
    risk = order.get("risk") or {}
    ip = order.get("clientIp") or None
    assessments = [
        {
            "provider": ((a or {}).get("provider") or {}).get("title"),
            "level": (a or {}).get("riskLevel"),
            "facts": [
                {"description": f["description"], "sentiment": f.get("sentiment")}
                for f in ((a or {}).get("facts") or [])
                if isinstance(f, dict) and f.get("description")
            ],
        }
        for a in (risk.get("assessments") or [])
    ]
    recommendation = risk.get("recommendation")
    cardholder = cardholder_check(order)
    if not assessments and not recommendation and not ip and not cardholder:
        return None
    return {
        "level": risk_level(risk),
        "recommendation": recommendation,
        "assessments": assessments,
        "ip": ip,
        "ip_location": None,
        "cardholder": cardholder,
    }


# ip-api.com's free endpoint is HTTP only, 45 requests/minute and licensed
# for non-commercial use; IP_API_KEY switches to the paid HTTPS endpoint.
IP_FIELDS = "status,city,regionName,country"
IP_CACHE_SECONDS = 24 * 60 * 60
_ip_cache = {}


def _ip_lookup_url(ip):
    key = os.environ.get("IP_API_KEY")
    if key:
        return f"https://pro.ip-api.com/json/{ip}?fields={IP_FIELDS}&key={key}"
    return f"http://ip-api.com/json/{ip}?fields={IP_FIELDS}"


def ip_location(ip):
    """"City, Region, Country" for a public IP, or None when it is private,
    invalid or the lookup fails; answers are cached for a day."""
    try:
        if not ip or not ipaddress.ip_address(ip).is_global:
            return None
    except ValueError:
        return None
    cached = _ip_cache.get(ip)
    if cached and time.monotonic() - cached[0] < IP_CACHE_SECONDS:
        return cached[1]
    try:
        resp = requests.get(_ip_lookup_url(ip), timeout=3)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    location = None
    if data.get("status") == "success":
        location = ", ".join(p for p in (data.get("city"), data.get("regionName"), data.get("country")) if p) or None
    _ip_cache[ip] = (time.monotonic(), location)
    return location

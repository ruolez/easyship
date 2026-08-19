"""Order tag rules: what a Shopify order tag implies for shipping.

A rule maps one tag (case-insensitive) to a preferred courier service and/or a
signature requirement. Pure functions over plain data so they're testable
without the database; `load_rules` is the only DB touchpoint.
"""
import json
import re

import db

RULES_KEY = "order_tag_rules"
SIGNATURES = ("none", "signature", "adult")
_SIGNATURE_RANK = {s: i for i, s in enumerate(SIGNATURES)}
_NAME_NOISE = re.compile(r"[®™©]|\s+")


def normalize_signature(value):
    value = (value or "none").strip().lower()
    return value if value in SIGNATURES else "none"


def parse_rules(raw):
    """Rules as stored in settings (JSON text) → clean list. Bad input → []."""
    try:
        data = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    rules = []
    for item in data:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag") or "").strip()
        if not tag:
            continue
        rules.append({
            "tag": tag,
            "service": str(item.get("service") or "").strip(),
            "signature": normalize_signature(item.get("signature")),
        })
    return rules


def load_rules():
    return parse_rules(db.get_setting(RULES_KEY))


def normalize_service_name(name):
    """Compare courier names across providers: 'UPS® Ground' == 'ups ground'."""
    return _NAME_NOISE.sub(" ", (name or "")).strip().lower().replace("  ", " ")


def service_matches(rule_service, courier_name):
    want = normalize_service_name(rule_service)
    have = normalize_service_name(courier_name)
    return bool(want) and (want == have or want in have)


def resolve(rules, tags):
    """What the order's tags imply: the strongest signature requirement, the
    first preferred service, and which rules fired (for the UI banner)."""
    tag_set = {t.strip().lower() for t in (tags or []) if t}
    matched = [r for r in rules if r["tag"].lower() in tag_set]
    signature = "none"
    service = ""
    for r in matched:
        if _SIGNATURE_RANK[r["signature"]] > _SIGNATURE_RANK[signature]:
            signature = r["signature"]
        if not service and r["service"]:
            service = r["service"]
    return {"signature": signature, "service": service, "matched": matched}


def preferred_by(courier_name, service_id, preferred_service="", preferred_service_id=""):
    """Why a rate is the preferred one: 'tag_rule' when an order tag rule
    names its service, 'preset' when it is the station's Auto Mode service,
    else None. A tag rule outranks the preset."""
    if service_matches(preferred_service, courier_name):
        return "tag_rule"
    if service_id and preferred_service_id and str(service_id) == str(preferred_service_id):
        return "preset"
    return None

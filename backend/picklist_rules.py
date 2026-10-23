"""Pick list rules: named, admin-defined order filters for the Pick lists page.

A rule either hides the orders it matches or keeps only them; the page
toggles rules on and off. Conditions look at order tags, customer tags, the
ship-to state, the store, the shipping method and whether the billing address
matches the ship-to address. Pure functions over plain data, like
`tag_rules`; `load_rules` is the only DB touchpoint.
"""
import json
import re
import uuid

import db

RULES_KEY = "picklist_rules"
ACTIONS = ("hide", "keep")
MATCHES = ("all", "any")
# field -> the operators it accepts; tag and text fields hold one value, list fields many.
TAG_FIELDS = {"order_tag": "tags", "customer_tag": "customer_tags"}
TEXT_FIELDS = {"shipping_method": "shipping_method"}
# Yes/no facts about the order: the condition holds when the fact is true.
FLAG_FIELDS = {"address_match": "billing_matches_shipping"}
LIST_FIELDS = ("ship_state", "store")
OPS = {"order_tag": ("has", "not_has"), "customer_tag": ("has", "not_has"),
       "ship_state": ("in", "not_in"), "store": ("in", "not_in"),
       "shipping_method": ("contains", "not_contains"),
       "address_match": ("match",)}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _list_value(raw, field):
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    values = [str(v).strip() for v in items]
    if field == "ship_state":
        values = [v.upper() for v in values]
    return [v for v in dict.fromkeys(values) if v]


def _parse_condition(item):
    if not isinstance(item, dict):
        return None
    field = str(item.get("field") or "")
    op = str(item.get("op") or "")
    if op not in OPS.get(field, ()):
        return None
    if field in FLAG_FIELDS:
        value = "yes"
    elif field in TAG_FIELDS or field in TEXT_FIELDS:
        value = str(item.get("value") or "").strip()
    else:
        value = _list_value(item.get("value"), field)
    return {"field": field, "op": op, "value": value} if value else None


def parse_rules(raw):
    """Rules as stored in settings (JSON text, or an already-decoded list) →
    clean list. Rules without a name or a usable condition are dropped, and
    each rule keeps a unique id so the page can remember which are on."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    rules, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        conditions = [c for c in map(_parse_condition, item.get("conditions") or []) if c]
        if not name or not conditions:
            continue
        rid = str(item.get("id") or "")
        if not _ID.match(rid) or rid in seen:
            rid = uuid.uuid4().hex[:12]
        seen.add(rid)
        action = str(item.get("action") or "").lower()
        match = str(item.get("match") or "").lower()
        rules.append({
            "id": rid,
            "name": name,
            "action": action if action in ACTIONS else "hide",
            "match": match if match in MATCHES else "all",
            "conditions": conditions,
        })
    return rules


def load_rules():
    return parse_rules(db.get_setting(RULES_KEY))


def _condition_matches(cond, order):
    field, op, value = cond["field"], cond["op"], cond["value"]
    if field in TAG_FIELDS:
        tags = {str(t).strip().lower() for t in order.get(TAG_FIELDS[field]) or []}
        hit = value.lower() in tags
        return hit if op == "has" else not hit
    if field in FLAG_FIELDS:
        return order.get(FLAG_FIELDS[field]) is True
    if field in TEXT_FIELDS:
        hit = value.lower() in str(order.get(TEXT_FIELDS[field]) or "").lower()
        return hit if op == "contains" else not hit
    if field == "ship_state":
        have = str(order.get("ship_state") or "").strip().upper()
    else:
        have = str(order.get("store_id") or "")
    hit = bool(have) and have in value
    return hit if op == "in" else not hit


def rule_matches(rule, order):
    results = (_condition_matches(c, order) for c in rule["conditions"])
    return all(results) if rule["match"] == "all" else any(results)


def matched_rule_ids(rules, order):
    return [r["id"] for r in rules if rule_matches(r, order)]

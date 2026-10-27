"""Pick list rules: named, admin-defined order filters for the Pick lists page.

A rule either hides the orders it matches or keeps only them; the page
toggles rules on and off. Conditions look at order tags, customer tags, the
ship-to state, the store, the shipping method, the order total and whether the
billing address matches the ship-to address. Pure functions over plain data, like
`tag_rules`; the `load_*` functions are the only DB touchpoints.

Presets combine saved rules into one filter: groups of rules joined by AND
or OR (each rule may be negated), and the groups joined by AND or OR. They
point at rules by id, so they are always read against the rules in place.
"""
import json
import math
import re
import uuid

import db

RULES_KEY = "picklist_rules"
PRESETS_KEY = "picklist_presets"
ACTIONS = ("hide", "keep")
JOINS = ("and", "or")
MAX_NAME = 40
MATCHES = ("all", "any")
# field -> the operators it accepts; tag and text fields hold one value, list fields many.
TAG_FIELDS = {"order_tag": "tags", "customer_tag": "customer_tags"}
TEXT_FIELDS = {"shipping_method": "shipping_method"}
# Dollar amounts compared against the order's number.
MONEY_FIELDS = {"order_total": "total"}
# Yes/no facts about the order: the condition holds when the fact is true.
FLAG_FIELDS = {"address_match": "billing_matches_shipping"}
LIST_FIELDS = ("ship_state", "store")
OPS = {"order_tag": ("contains", "not_contains"), "customer_tag": ("contains", "not_contains"),
       "ship_state": ("in", "not_in"), "store": ("in", "not_in"),
       "shipping_method": ("contains", "not_contains"),
       "order_total": ("at_least", "under"),
       "address_match": ("match",)}
# Tag operators saved before they were renamed.
LEGACY_TAG_OPS = {"has": "contains", "not_has": "not_contains"}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _list_value(raw, field):
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    values = [str(v).strip() for v in items]
    if field == "ship_state":
        values = [v.upper() for v in values]
    return [v for v in dict.fromkeys(values) if v]


def _money(raw):
    """A non-negative dollar amount ("$1,250.50", 99, "0") → float, else None."""
    try:
        amount = float(str(raw).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return round(amount, 2) if math.isfinite(amount) and amount >= 0 else None


def _parse_condition(item):
    if not isinstance(item, dict):
        return None
    field = str(item.get("field") or "")
    op = str(item.get("op") or "")
    if field in TAG_FIELDS:
        op = LEGACY_TAG_OPS.get(op, op)
    if op not in OPS.get(field, ()):
        return None
    if field in MONEY_FIELDS:
        value = _money(item.get("value"))
        return None if value is None else {"field": field, "op": op, "value": value}
    if field in FLAG_FIELDS:
        value = "yes"
    elif field in TAG_FIELDS or field in TEXT_FIELDS:
        value = str(item.get("value") or "").strip()
    else:
        value = _list_value(item.get("value"), field)
    return {"field": field, "op": op, "value": value} if value else None


def _json_list(raw):
    """Settings JSON text, or an already-decoded value → a list ([] when unusable)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            return []
    return raw if isinstance(raw, list) else []


def _unique_id(raw_id, seen):
    rid = str(raw_id or "")
    if not _ID.match(rid) or rid in seen:
        rid = uuid.uuid4().hex[:12]
    seen.add(rid)
    return rid


def parse_rules(raw):
    """Rules as stored in settings (JSON text, or an already-decoded list) →
    clean list. Rules without a name or a usable condition are dropped, and
    each rule keeps a unique id so the page can remember which are on."""
    rules, seen = [], set()
    for item in _json_list(raw):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        conditions = [c for c in map(_parse_condition, item.get("conditions") or []) if c]
        if not name or not conditions:
            continue
        rid = _unique_id(item.get("id"), seen)
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


def _join(raw):
    join = str(raw or "").lower()
    return join if join in JOINS else "and"


def _parse_group(group, known):
    if not isinstance(group, dict):
        return None
    items, used = [], set()
    for item in group.get("items") or []:
        if not isinstance(item, dict):
            continue
        rule_id, negate = str(item.get("rule") or ""), item.get("negate") is True
        if rule_id in known and (rule_id, negate) not in used:
            used.add((rule_id, negate))
            items.append({"rule": rule_id, "negate": negate})
    return {"join": _join(group.get("join")), "items": items} if items else None


def parse_presets(raw, rule_ids):
    """Presets as stored → clean list, read against the ids of the rules in
    place: items of rules that are gone are dropped, then groups left empty,
    then presets left with no group or no name."""
    known = set(rule_ids)
    presets, seen = [], set()
    for item in _json_list(raw):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:MAX_NAME]
        groups = [g for g in (_parse_group(g, known) for g in item.get("groups") or []) if g]
        if not name or not groups:
            continue
        presets.append({"id": _unique_id(item.get("id"), seen), "name": name,
                        "join": _join(item.get("join")), "groups": groups})
    return presets


def load_presets(rules):
    return parse_presets(db.get_setting(PRESETS_KEY), [r["id"] for r in rules])


def preset_matches(preset, matched_rule_ids):
    """Whether an order matching these rules falls in the preset — the same
    reading as presetMatches on the Pick lists page."""
    def item(it):
        return (it["rule"] in matched_rule_ids) != it["negate"]

    def group(g):
        return (any if g["join"] == "or" else all)(item(it) for it in g["items"])

    return (any if preset["join"] == "or" else all)(group(g) for g in preset["groups"])


def _condition_matches(cond, order):
    field, op, value = cond["field"], cond["op"], cond["value"]
    if field in TAG_FIELDS:
        needle = value.lower()
        hit = any(needle in str(t).lower() for t in order.get(TAG_FIELDS[field]) or [])
        return hit if op == "contains" else not hit
    if field in MONEY_FIELDS:
        total = _money(order.get(MONEY_FIELDS[field]))
        if total is None:
            return False
        return total >= value if op == "at_least" else total < value
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

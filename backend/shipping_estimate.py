"""Shipping cost estimates for the Pick lists order window, from the labels
EasyShip already bought: the average cost of same-store, same-state Shopify
shipments whose items weighed about the same, widening the weight band until
enough samples are found. Ported from Shopify Automation's estimate, with
EasyShip's own label history in place of the shipper database.

Weights compare item weights on both sides (the order's unfulfilled items, a
past shipment's stored items), so the box itself never skews the match.
"""
import json

import db

SAMPLE_WINDOW_DAYS = 90
# Label weights are entered to a tenth of a pound, so the bands start there.
TOLERANCE_TIERS_LB = (0.1, 0.25, 0.5, 1.0)
# After the fixed bands, widen relative to the order's own weight so heavy
# (wholesale) orders can match: a 200 lb order tries ±10, 20, 40, 70 lb.
RELATIVE_TOLERANCE_TIERS = (0.05, 0.10, 0.20, 0.35)
MIN_SAMPLES = 3


def tolerance_tiers_for(weight_lb):
    """Fixed bands, then relative bands that are wider than the last fixed one."""
    tiers = list(TOLERANCE_TIERS_LB)
    for fraction in RELATIVE_TOLERANCE_TIERS:
        tolerance = round(weight_lb * fraction, 2)
        if tolerance > tiers[-1]:
            tiers.append(tolerance)
    return tiers


def pick_samples(samples, weight_lb):
    """(tolerance used, costs): the first band with at least MIN_SAMPLES,
    else whatever the widest band has."""
    costs = []
    tiers = tolerance_tiers_for(weight_lb)
    for tolerance in tiers:
        costs = [cost for weight, cost in samples if abs(weight - weight_lb) <= tolerance + 1e-9]
        if len(costs) >= MIN_SAMPLES:
            return tolerance, costs
    return (tiers[-1] if costs else None), costs


def estimate_from_samples(samples, weight_lb):
    tolerance, costs = pick_samples(samples, weight_lb)
    if not costs:
        return {"cost": None, "samples": 0, "tolerance_lb": None}
    return {"cost": round(sum(costs) / len(costs), 2), "samples": len(costs), "tolerance_lb": tolerance}


def items_weight_lb(items):
    """Total weight of a shipment's stored items (per-unit lb × quantity)."""
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            return 0.0
    total = 0.0
    for item in items or []:
        try:
            total += float(item.get("weight") or 0) * float(item.get("quantity") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
    return round(total, 3)


# One sample per shipment: the boxes of a group add up, and the items live on box 1.
SAMPLES_SQL = """
    SELECT SUM(shipping_cost) AS cost,
           (ARRAY_AGG(items ORDER BY box_number))[1] AS items
      FROM shipments
     WHERE source = 'shopify' AND shopify_store_id = %s
       AND status IN ('label_created', 'fulfilled')
       AND UPPER(destination->>'state') = %s
       AND label_created_at >= now() - make_interval(days => %s)
     GROUP BY COALESCE(group_id, id::text)
    HAVING COUNT(*) = COUNT(shipping_cost)
"""


def load_samples(store_id, state):
    rows = db.query(SAMPLES_SQL, (store_id, state, SAMPLE_WINDOW_DAYS)) or []
    samples = []
    for row in rows:
        weight = items_weight_lb(row.get("items"))
        if weight > 0 and row.get("cost") is not None:
            samples.append((weight, float(row["cost"])))
    return samples


def estimate_shipping(store_id, state, weight_lb):
    """The estimate for one order; never raises."""
    state = (state or "").strip().upper()
    out = {"cost": None, "samples": 0, "tolerance_lb": None, "state": state or None,
           "weight_lb": round(weight_lb, 2) if weight_lb else None}
    if not store_id or not state or not weight_lb or weight_lb <= 0:
        return out
    try:
        samples = load_samples(store_id, state)
    except Exception:
        return out
    return {**out, **estimate_from_samples(samples, weight_lb)}

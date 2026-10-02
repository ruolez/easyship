"""Order profitability: whole-order economics from the order source, each
rate's evaluation against the admin thresholds, and the buy-time gate that
reads the snapshot stored on the draft shipment."""
import json

import db

SETTING_MIN_AMOUNT = "profit_min_amount"
SETTING_MIN_PCT = "profit_min_margin_pct"
SETTING_PASSWORD = "profit_bypass_password"

GATED_SOURCES = ("shopify", "backoffice")

UNAVAILABLE_TEXT = {
    "no_order_id": "the order could not be loaded from Shopify",
    "no_invoice_id": "the invoice could not be loaded from BackOffice",
    "lines_truncated": "the order has more than 100 line items",
    "not_rated": "the shipment was rated before profit checks were turned on",
    "no_rate": "the chosen rate was not among the rates offered for this shipment",
}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _money(value):
    return ("-" if value < 0 else "") + f"${abs(value):,.2f}"


def parse_thresholds(min_amount, min_margin_pct):
    amount, pct = _num(min_amount), _num(min_margin_pct)
    return {"min_amount": amount, "min_margin_pct": pct,
            "enabled": amount is not None or pct is not None}


def load_thresholds():
    return parse_thresholds(db.get_setting(SETTING_MIN_AMOUNT), db.get_setting(SETTING_MIN_PCT))


def unavailable(reason, currency=None):
    return {"available": False, "reason": reason,
            "reason_text": UNAVAILABLE_TEXT.get(reason, reason), "currency": currency,
            "items_subtotal": None, "shipping_paid": None, "revenue": None,
            "items_cost": None, "line_count": 0, "missing_cost": []}


def compute_economics(lines, items_subtotal, shipping_paid, currency):
    """Whole-order revenue and cost. A line without a usable unit cost is
    costed at its selling price (zero profit) and listed in missing_cost."""
    items_cost = 0.0
    missing = []
    line_count = 0
    for line in lines:
        qty = _num(line.get("quantity")) or 0
        if qty <= 0:
            continue
        line_count += 1
        price = _num(line.get("unit_price")) or 0.0
        cost = _num(line.get("unit_cost"))
        if cost is None or cost <= 0:
            cost = price
            missing.append({"description": line.get("description"), "sku": line.get("sku"),
                            "quantity": line.get("quantity")})
        items_cost += cost * qty
    subtotal = _num(items_subtotal) or 0.0
    shipping = _num(shipping_paid) or 0.0
    return {"available": True, "reason": None, "reason_text": None, "currency": currency,
            "items_subtotal": round(subtotal, 2), "shipping_paid": round(shipping, 2),
            "revenue": round(subtotal + shipping, 2), "items_cost": round(items_cost, 2),
            "line_count": line_count, "missing_cost": missing}


def evaluate(economics, label_cost, rate_currency, thresholds):
    """Profit of one rate against the thresholds. below_threshold is only ever
    True while a threshold is set; a profit that cannot be determined then
    counts as below."""
    econ = economics or unavailable("not_rated")
    cost = _num(label_cost)
    out = {
        "available": econ["available"], "reason": econ["reason"],
        "reason_text": econ["reason_text"], "currency": econ["currency"],
        "revenue": econ["revenue"], "items_cost": econ["items_cost"],
        "label_cost": round(cost, 2) if cost is not None else None,
        "profit": None, "margin_pct": None, "below_threshold": False, "reasons": [],
        "thresholds": {"min_amount": thresholds["min_amount"],
                       "min_margin_pct": thresholds["min_margin_pct"]},
        "missing_cost": econ["missing_cost"],
    }
    reasons = []
    if not econ["available"]:
        reasons.append(f"Profit could not be determined: {econ['reason_text']}")
    elif cost is None:
        reasons.append(f"Profit could not be determined: {UNAVAILABLE_TEXT['no_rate']}")
    elif rate_currency and econ["currency"] and rate_currency != econ["currency"]:
        reasons.append(f"Rate currency {rate_currency} differs from the order currency {econ['currency']}")
    else:
        profit = round(econ["revenue"] - econ["items_cost"] - cost, 2)
        out["profit"] = profit
        if econ["revenue"] > 0:
            out["margin_pct"] = round(profit / econ["revenue"] * 100, 1)
        min_amount, min_pct = thresholds["min_amount"], thresholds["min_margin_pct"]
        if min_amount is not None and profit < min_amount:
            reasons.append(f"Profit {_money(profit)} is below the {_money(min_amount)} minimum")
        if min_pct is not None:
            if out["margin_pct"] is None:
                reasons.append("Margin cannot be measured — the order has no revenue")
            elif out["margin_pct"] < min_pct:
                reasons.append(f"Margin {out['margin_pct']}% is below the {min_pct:g}% minimum")
    if thresholds["enabled"]:
        out["below_threshold"] = bool(reasons)
        out["reasons"] = reasons
    return out


def fetch_economics(source, data):
    """Economics straight from the order source at rate time. Never raises:
    whatever stops the fetch becomes an unavailable() the gate treats as
    below threshold."""
    try:
        if source == "shopify":
            if not data.get("order_id"):
                return unavailable("no_order_id")
            import shopify_client
            return shopify_client.get_order(data.get("store_id"), data["order_id"])["economics"]
        if source == "backoffice":
            if not data.get("invoice_id"):
                return unavailable("no_invoice_id")
            import backoffice
            return backoffice.get_invoice(data.get("db_id"), data["invoice_id"])["economics"]
    except Exception as e:
        return unavailable(str(e) or e.__class__.__name__)
    return None


def snapshot(economics, rates):
    """What the buy-time gate needs, stored on box 1 at rate time. The label
    cost is taken from the rates the server itself offered, so a rate sent
    back by the browser cannot dodge the gate."""
    return {
        "economics": economics,
        "offered_rates": [{"provider": r.get("provider"),
                           "courier_service_id": r.get("courier_service_id"),
                           "total_charge": r.get("total_charge"),
                           "currency": r.get("currency")} for r in rates],
        "cleared": None,
    }


def resolve_label_cost(profit_check, provider, courier_service_id):
    for r in (profit_check or {}).get("offered_rates") or []:
        if r.get("provider") == provider and str(r.get("courier_service_id")) == str(courier_service_id):
            return r.get("total_charge"), r.get("currency")
    return None, None


def gate_for_buy(row, provider, courier_service_id, thresholds):
    """Evaluation of the chosen rate for a buy, or None when the gate does
    not apply (manual shipment, no threshold set)."""
    if row.get("source") not in GATED_SOURCES or not thresholds["enabled"]:
        return None
    check = row.get("profit_check") or {}
    label_cost, currency = resolve_label_cost(check, provider, courier_service_id)
    return evaluate(check.get("economics"), label_cost, currency, thresholds)


def already_cleared(row, provider, courier_service_id):
    cleared = (row.get("profit_check") or {}).get("cleared") or {}
    return (cleared.get("provider") == provider
            and str(cleared.get("courier_service_id")) == str(courier_service_id))


def mark_cleared(row_id, provider, courier_service_id, bypassed):
    cleared = {"provider": provider, "courier_service_id": courier_service_id, "bypassed": bypassed}
    db.execute(
        """UPDATE shipments SET profit_check = COALESCE(profit_check, '{}'::jsonb) || %s::jsonb,
           updated_at=now() WHERE id=%s""",
        (json.dumps({"cleared": cleared}), row_id),
    )

"""Shopify order gate: an order on hold or not fully paid can be rated but
never bought — there is no bypass. The buy judges live from Shopify, from
the rate-time snapshot when Shopify cannot be reached, and not at all when
the order was never loaded (the outage flow that only knows its number)."""

PAID_STATUSES = ("PAID", "PARTIALLY_REFUNDED")

UNPAID_TEXT = {
    "PENDING": "Payment is pending — the order has not been paid",
    "AUTHORIZED": "Payment is authorized but has not been captured",
    "PARTIALLY_PAID": "The order is only partially paid",
    "EXPIRED": "The payment authorization has expired",
    "VOIDED": "The payment was voided",
    "REFUNDED": "The order has been fully refunded",
}

UNPAID_LABEL = {
    "PENDING": "Payment pending",
    "AUTHORIZED": "Payment not captured",
    "PARTIALLY_PAID": "Partially paid",
    "EXPIRED": "Payment expired",
    "VOIDED": "Payment voided",
    "REFUNDED": "Refunded",
}

ACTIVE_STATUSES = ("OPEN", "IN_PROGRESS")


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hold_text(hold):
    text = (hold.get("reason") or "other").replace("_", " ").capitalize()
    return f"{text} ({hold['notes']})" if hold.get("notes") else text


def from_shopify_order(order):
    """The gate for a raw GraphQL order node. Holds come from the fulfillment
    orders; the Orders list only carries displayFulfillmentStatus, which
    says ON_HOLD when every unfulfilled item is held."""
    order = order or {}
    nodes = (order.get("fulfillmentOrders") or {}).get("nodes")
    holds = []
    on_hold = False
    others_open = False
    if nodes is None:
        on_hold = order.get("displayFulfillmentStatus") == "ON_HOLD"
    else:
        for fo in nodes:
            if fo.get("status") == "ON_HOLD":
                on_hold = True
                holds.extend({"reason": h.get("reason"), "notes": h.get("reasonNotes") or None}
                             for h in fo.get("fulfillmentHolds") or [])
            elif fo.get("status") in ACTIVE_STATUSES:
                others_open = True

    status = order.get("displayFinancialStatus")
    fully_paid = status in PAID_STATUSES if status else order.get("fullyPaid") is not False
    money = (order.get("totalOutstandingSet") or {}).get("shopMoney") or {}
    outstanding = _num(money.get("amount")) or 0.0

    reasons = []
    if on_hold:
        lead = "Some items are on hold in Shopify" if others_open else "Fulfillment is on hold in Shopify"
        detail = ", ".join(_hold_text(h) for h in holds)
        reasons.append(f"{lead}: {detail}" if detail else lead)
    if not fully_paid:
        text = UNPAID_TEXT.get(status, "The order is not fully paid")
        if outstanding > 0:
            text += f" — ${outstanding:,.2f} outstanding"
        reasons.append(text)

    label = None
    if on_hold:
        label = "On hold"
    elif not fully_paid:
        label = UNPAID_LABEL.get(status, "Not paid")
    return {
        "shippable": not reasons,
        "label": label,
        "reasons": reasons,
        "on_hold": on_hold,
        "fully_paid": fully_paid,
        "financial_status": status,
        "holds": holds,
        "outstanding": round(outstanding, 2),
        "currency": money.get("currencyCode"),
    }


def warning_text(gate):
    return f"{gate['label']} — rates only, a label cannot be bought: {'; '.join(gate['reasons'])}"


def for_buy(row):
    """(gate, live) for a buy: Shopify's current answer when it can be
    reached, otherwise the rate-time snapshot — None when there is none."""
    if row.get("shopify_order_id"):
        import shopify_client
        try:
            return shopify_client.get_order_gate(row["shopify_store_id"], row["shopify_order_id"]), True
        except shopify_client.ShopifyError:
            pass
    return row.get("order_gate"), False

import requests

import config
import db


class ShopifyError(Exception):
    pass


def _store(store_id):
    store = db.query("SELECT * FROM shopify_stores WHERE id = %s", (store_id,), one=True)
    if not store:
        raise ShopifyError("Shopify store not found")
    return store


def _graphql(store_id, query, variables=None):
    store = _store(store_id)
    url = f"https://{store['shop_domain']}/admin/api/{config.SHOPIFY_API_VERSION}/graphql.json"
    try:
        resp = requests.post(
            url,
            headers={"X-Shopify-Access-Token": store["access_token"]},
            json={"query": query, "variables": variables or {}},
            timeout=30,
        )
    except requests.RequestException as e:
        raise ShopifyError(f"Shopify request failed: {e}")
    if resp.status_code != 200:
        raise ShopifyError(f"Shopify returned {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if data.get("errors"):
        raise ShopifyError(f"Shopify GraphQL error: {data['errors']}")
    return data["data"]


ORDERS_QUERY = """
query openOrders($query: String!) {
  orders(first: 50, query: $query, sortKey: CREATED_AT, reverse: true) {
    nodes {
      id
      name
      createdAt
      displayFulfillmentStatus
      totalPriceSet { shopMoney { amount } }
      customer { displayName }
      shippingAddress { name company city provinceCode zip }
      lineItems(first: 5) { nodes { id } }
      subtotalLineItemsQuantity
    }
  }
}
"""

ORDER_DETAIL_QUERY = """
query orderDetail($id: ID!) {
  order(id: $id) {
    id
    name
    email
    displayFulfillmentStatus
    fulfillments(first: 25) {
      status
      trackingInfo { number }
    }
    customer { displayName }
    shippingAddress {
      name
      company
      address1
      address2
      city
      provinceCode
      zip
      countryCodeV2
      phone
    }
    lineItems(first: 100) {
      nodes {
        title
        sku
        quantity
        unfulfilledQuantity
        originalUnitPriceSet { shopMoney { amount } }
        variant {
          inventoryItem {
            measurement { weight { unit value } }
          }
        }
      }
    }
  }
}
"""

FULFILLMENT_ORDERS_QUERY = """
query fulfillmentOrders($id: ID!) {
  order(id: $id) {
    fulfillmentOrders(first: 10) {
      nodes { id status }
    }
  }
}
"""

FULFILLMENT_CREATE_MUTATION = """
mutation fulfill($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment { id status }
    userErrors { field message }
  }
}
"""

FULFILLMENT_CANCEL_MUTATION = """
mutation cancelFulfillment($id: ID!) {
  fulfillmentCancel(id: $id) {
    fulfillment { id status }
    userErrors { field message }
  }
}
"""

ORDER_FULFILLMENTS_QUERY = """
query orderFulfillments($id: ID!) {
  order(id: $id) {
    fulfillments(first: 25) {
      id
      status
      trackingInfo { number company }
    }
  }
}
"""

TRACKING_UPDATE_MUTATION = """
mutation updateTracking($fulfillmentId: ID!, $trackingInfoInput: FulfillmentTrackingInput!, $notifyCustomer: Boolean) {
  fulfillmentTrackingInfoUpdate(fulfillmentId: $fulfillmentId, trackingInfoInput: $trackingInfoInput, notifyCustomer: $notifyCustomer) {
    fulfillment { id status }
    userErrors { field message }
  }
}
"""

WEIGHT_TO_LB = {"POUNDS": 1.0, "OUNCES": 1.0 / 16, "KILOGRAMS": 2.20462, "GRAMS": 0.00220462}


def list_open_orders(store_id):
    data = _graphql(store_id, ORDERS_QUERY, {"query": "fulfillment_status:unfulfilled status:open"})
    orders = []
    for node in data["orders"]["nodes"]:
        addr = node.get("shippingAddress") or {}
        orders.append({
            "id": node["id"],
            "name": node["name"],
            "created_at": (node.get("createdAt") or "")[:10],
            "customer": (node.get("customer") or {}).get("displayName")
                        or addr.get("name") or "",
            "ship_to": ", ".join(filter(None, [
                addr.get("company") or addr.get("name"),
                addr.get("city"), addr.get("provinceCode"), addr.get("zip"),
            ])),
            "item_count": node.get("subtotalLineItemsQuantity") or 0,
            "total": (node.get("totalPriceSet") or {}).get("shopMoney", {}).get("amount"),
        })
    return orders


ORDER_ID_BY_NAME_QUERY = """
query orderByName($query: String!) {
  orders(first: 1, query: $query) {
    nodes { id name }
  }
}
"""


def find_order_by_number(store_id, number):
    """Look up an order by its number (with or without the # prefix).
    Returns the order gid or None."""
    number = (number or "").strip().lstrip("#")
    data = _graphql(store_id, ORDER_ID_BY_NAME_QUERY, {"query": f"name:#{number}"})
    nodes = data["orders"]["nodes"]
    if not nodes:
        data = _graphql(store_id, ORDER_ID_BY_NAME_QUERY, {"query": f"name:{number}"})
        nodes = data["orders"]["nodes"]
    return nodes[0]["id"] if nodes else None


def get_order(store_id, order_gid):
    data = _graphql(store_id, ORDER_DETAIL_QUERY, {"id": order_gid})
    order = data.get("order")
    if not order:
        raise ShopifyError("Order not found")
    addr = order.get("shippingAddress") or {}
    existing_tracking = []
    for f in order.get("fulfillments") or []:
        if f.get("status") == "CANCELLED":
            continue
        for t in f.get("trackingInfo") or []:
            n = t.get("number")
            if n and n not in existing_tracking:
                existing_tracking.append(n)
    items = []
    total_weight_lb = 0.0
    for li in order["lineItems"]["nodes"]:
        qty = li.get("unfulfilledQuantity") or li.get("quantity") or 0
        if qty <= 0:
            continue
        weight_lb = 0.0
        variant = li.get("variant") or {}
        measurement = ((variant.get("inventoryItem") or {}).get("measurement") or {})
        weight = measurement.get("weight") or {}
        if weight.get("value"):
            weight_lb = float(weight["value"]) * WEIGHT_TO_LB.get(weight.get("unit"), 0)
        total_weight_lb += weight_lb * qty
        items.append({
            "description": li.get("title"),
            "sku": li.get("sku"),
            "quantity": qty,
            "value": float((li.get("originalUnitPriceSet") or {}).get("shopMoney", {}).get("amount") or 0),
            "weight": round(weight_lb, 3),
        })
    return {
        "id": order["id"],
        "name": order["name"],
        "customer": (order.get("customer") or {}).get("displayName") or addr.get("name"),
        "destination": {
            "company": addr.get("company"),
            "contact": addr.get("name"),
            "address1": addr.get("address1"),
            "address2": addr.get("address2"),
            "city": addr.get("city"),
            "state": addr.get("provinceCode"),
            "zip": addr.get("zip"),
            "country": addr.get("countryCodeV2") or "US",
            "phone": addr.get("phone"),
            "email": order.get("email"),
        },
        "items": items,
        "total_weight_lb": round(total_weight_lb, 1) if total_weight_lb else None,
        "fulfillment_status": order.get("displayFulfillmentStatus"),
        "existing_tracking": existing_tracking,
    }


def fulfill_order(store_id, order_gid, tracking_number, courier_name, all_numbers=None):
    data = _graphql(store_id, FULFILLMENT_ORDERS_QUERY, {"id": order_gid})
    order = data.get("order")
    if not order:
        raise ShopifyError("Order not found for fulfillment")
    open_fos = [
        fo for fo in order["fulfillmentOrders"]["nodes"]
        if fo["status"] in ("OPEN", "IN_PROGRESS")
    ]
    if not open_fos:
        numbers = list(all_numbers or ([tracking_number] if tracking_number else []))
        return _append_tracking(store_id, order_gid, numbers, courier_name)
    tracking_info = {"company": courier_name or "Other"}
    if all_numbers and len(all_numbers) > 1:
        tracking_info["numbers"] = all_numbers
    else:
        tracking_info["number"] = tracking_number
    fulfillment = {
        "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": fo["id"]} for fo in open_fos],
        "trackingInfo": tracking_info,
        "notifyCustomer": True,
    }
    data = _graphql(store_id, FULFILLMENT_CREATE_MUTATION, {"fulfillment": fulfillment})
    result = data["fulfillmentCreate"]
    if result.get("userErrors"):
        raise ShopifyError("; ".join(e["message"] for e in result["userErrors"]))
    return result["fulfillment"]


def _active_fulfillments(store_id, order_gid):
    data = _graphql(store_id, ORDER_FULFILLMENTS_QUERY, {"id": order_gid})
    order = data.get("order")
    if not order:
        raise ShopifyError("Order not found")
    return [f for f in order.get("fulfillments") or [] if f.get("status") != "CANCELLED"]


def _update_tracking(store_id, fulfillment_gid, tracking_info, notify):
    data = _graphql(store_id, TRACKING_UPDATE_MUTATION, {
        "fulfillmentId": fulfillment_gid,
        "trackingInfoInput": tracking_info,
        "notifyCustomer": notify,
    })
    result = data["fulfillmentTrackingInfoUpdate"]
    if result.get("userErrors"):
        raise ShopifyError("; ".join(e["message"] for e in result["userErrors"]))
    return result["fulfillment"]


def _append_tracking(store_id, order_gid, numbers, courier_name):
    """Re-ship of an already-fulfilled order: add the new numbers to the latest
    existing fulfillment's tracking info — the order stays fulfilled."""
    fulfillments = _active_fulfillments(store_id, order_gid)
    if not fulfillments:
        raise ShopifyError("No open fulfillment orders and no existing fulfillment to add tracking to")
    if not numbers:
        raise ShopifyError("No tracking numbers to add")
    target = fulfillments[-1]
    current = [t.get("number") for t in target.get("trackingInfo") or [] if t.get("number")]
    merged = current + [n for n in numbers if n not in current]
    company = next(
        (t.get("company") for t in target.get("trackingInfo") or [] if t.get("company")),
        None,
    )
    tracking_info = {"company": company or courier_name or "Other", "numbers": merged}
    return _update_tracking(store_id, target["id"], tracking_info, notify=True)


def cancel_fulfillment(store_id, fulfillment_gid):
    data = _graphql(store_id, FULFILLMENT_CANCEL_MUTATION, {"id": fulfillment_gid})
    result = data["fulfillmentCancel"]
    if result.get("userErrors"):
        raise ShopifyError("; ".join(e["message"] for e in result["userErrors"]))
    return result["fulfillment"]


def remove_tracking(store_id, order_gid, fulfillment_gid, numbers):
    """Undo our tracking writeback. A fulfillment that carries only our numbers is
    cancelled outright; a pre-existing fulfillment we appended to on a re-ship
    keeps its other numbers and stays fulfilled."""
    target = next(
        (f for f in _active_fulfillments(store_id, order_gid) if f["id"] == fulfillment_gid),
        None,
    )
    if target is None:
        return "fulfillment already cancelled"
    current = [t.get("number") for t in target.get("trackingInfo") or [] if t.get("number")]
    kept = [n for n in current if n not in (numbers or [])]
    if not kept:
        cancel_fulfillment(store_id, fulfillment_gid)
        return "fulfillment cancelled"
    if kept == current:
        return "our tracking numbers not on the fulfillment — nothing removed"
    tracking_info = {"numbers": kept}
    company = next(
        (t.get("company") for t in target.get("trackingInfo") or [] if t.get("company")),
        None,
    )
    if company:
        tracking_info["company"] = company
    _update_tracking(store_id, fulfillment_gid, tracking_info, notify=False)
    return "tracking removed (order stays fulfilled)"


def find_fulfillment_by_tracking(store_id, order_gid, tracking_number):
    """Fallback for shipments created before the fulfillment id was stored."""
    for f in _active_fulfillments(store_id, order_gid):
        numbers = [t.get("number") for t in f.get("trackingInfo") or []]
        if tracking_number in numbers:
            return f["id"]
    return None

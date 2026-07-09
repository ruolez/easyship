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
      trackingInfo { number }
    }
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
    }


def fulfill_order(store_id, order_gid, tracking_number, courier_name):
    data = _graphql(store_id, FULFILLMENT_ORDERS_QUERY, {"id": order_gid})
    order = data.get("order")
    if not order:
        raise ShopifyError("Order not found for fulfillment")
    open_fos = [
        fo for fo in order["fulfillmentOrders"]["nodes"]
        if fo["status"] in ("OPEN", "IN_PROGRESS")
    ]
    if not open_fos:
        raise ShopifyError("No open fulfillment orders — order may already be fulfilled")
    fulfillment = {
        "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": fo["id"]} for fo in open_fos],
        "trackingInfo": {
            "company": courier_name or "Other",
            "number": tracking_number,
        },
        "notifyCustomer": True,
    }
    data = _graphql(store_id, FULFILLMENT_CREATE_MUTATION, {"fulfillment": fulfillment})
    result = data["fulfillmentCreate"]
    if result.get("userErrors"):
        raise ShopifyError("; ".join(e["message"] for e in result["userErrors"]))
    return result["fulfillment"]


def cancel_fulfillment(store_id, fulfillment_gid):
    data = _graphql(store_id, FULFILLMENT_CANCEL_MUTATION, {"id": fulfillment_gid})
    result = data["fulfillmentCancel"]
    if result.get("userErrors"):
        raise ShopifyError("; ".join(e["message"] for e in result["userErrors"]))
    return result["fulfillment"]


def find_fulfillment_by_tracking(store_id, order_gid, tracking_number):
    """Fallback for shipments created before the fulfillment id was stored."""
    data = _graphql(store_id, ORDER_FULFILLMENTS_QUERY, {"id": order_gid})
    order = data.get("order")
    if not order:
        raise ShopifyError("Order not found")
    for f in order.get("fulfillments") or []:
        if f.get("status") == "CANCELLED":
            continue
        numbers = [t.get("number") for t in f.get("trackingInfo") or []]
        if tracking_number in numbers:
            return f["id"]
    return None

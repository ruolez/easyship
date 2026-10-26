import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

import config
import db
import order_gate
import profit


class ShopifyError(Exception):
    pass


class ShopifyUnavailable(ShopifyError):
    """Shopify could not be reached or was overloaded — a retry later can succeed."""


RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (1.0, 3.0)
# Throttling is Shopify pacing us, not an outage: wait as long as its
# throttle status says the bucket needs, many times over if need be.
THROTTLE_RETRIES = 20
THROTTLE_WAIT_BOUNDS = (0.2, 10.0)
_sleep = time.sleep


class ShopifyCostExceeded(ShopifyError):
    """The query asks for more than Shopify's single-query cost limit."""


def _store(store_id):
    store = db.query("SELECT * FROM shopify_stores WHERE id = %s", (store_id,), one=True)
    if not store:
        raise ShopifyError("Shopify store not found")
    return store


def throttle_wait(extensions):
    """Seconds until the store's bucket holds the query's requested cost, from
    the cost block Shopify sends with a THROTTLED error."""
    cost = (extensions or {}).get("cost") or {}
    status = cost.get("throttleStatus") or {}
    rate, available, requested = status.get("restoreRate"), status.get("currentlyAvailable"), cost.get("requestedQueryCost")
    if not rate or available is None or requested is None:
        return RETRY_BACKOFF_SECONDS[0]
    low, high = THROTTLE_WAIT_BOUNDS
    return min(high, max(low, (requested - available) / rate + random.uniform(0, 0.25)))


def _graphql(store_id, query, variables=None):
    return _graphql_for(_store(store_id), query, variables)


def _graphql_for(store, query, variables=None):
    """_graphql for a store row already read, so worker threads need no DB."""
    url = f"https://{store['shop_domain']}/admin/api/{config.SHOPIFY_API_VERSION}/graphql.json"
    failures, throttles, last_transient = 0, 0, None
    while True:
        try:
            resp = requests.post(
                url,
                headers={"X-Shopify-Access-Token": store["access_token"]},
                json={"query": query, "variables": variables or {}},
                timeout=30,
            )
        except requests.RequestException as e:
            last_transient = f"Shopify request failed: {e}"
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                last_transient = f"Shopify returned {resp.status_code}: {resp.text[:300]}"
            elif resp.status_code != 200:
                raise ShopifyError(f"Shopify returned {resp.status_code}: {resp.text[:300]}")
            else:
                data = resp.json()
                errors = data.get("errors")
                if not errors:
                    return data["data"]
                codes = {(e.get("extensions") or {}).get("code") for e in errors}
                if "THROTTLED" not in codes:
                    if "MAX_COST_EXCEEDED" in codes:
                        raise ShopifyCostExceeded(f"Shopify GraphQL error: {errors}")
                    raise ShopifyError(f"Shopify GraphQL error: {errors}")
                throttles += 1
                if throttles > THROTTLE_RETRIES:
                    raise ShopifyUnavailable(f"Shopify kept throttling the request: {errors}")
                _sleep(throttle_wait(data.get("extensions")))
                continue
        failures += 1
        if failures >= RETRY_ATTEMPTS:
            raise ShopifyUnavailable(f"Shopify unavailable after {RETRY_ATTEMPTS} attempts — {last_transient}")
        _sleep(RETRY_BACKOFF_SECONDS[min(failures - 1, len(RETRY_BACKOFF_SECONDS) - 1)])


ORDERS_QUERY = """
query openOrders($query: String!) {
  orders(first: 50, query: $query, sortKey: CREATED_AT, reverse: true) {
    nodes {
      id
      name
      createdAt
      displayFulfillmentStatus
      displayFinancialStatus
      tags
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
    displayFinancialStatus
    fullyPaid
    totalOutstandingSet { shopMoney { amount currencyCode } }
    fulfillmentOrders(first: 10) {
      nodes { id status fulfillmentHolds { reason reasonNotes } }
    }
    tags
    note
    currencyCode
    currentSubtotalPriceSet { shopMoney { amount } }
    currentShippingPriceSet { shopMoney { amount } }
    totalShippingPriceSet { shopMoney { amount } }
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
      pageInfo { hasNextPage }
      nodes {
        title
        sku
        quantity
        currentQuantity
        unfulfilledQuantity
        originalUnitPriceSet { shopMoney { amount } }
        discountedUnitPriceAfterAllDiscountsSet { shopMoney { amount } }
        variant {
          inventoryItem {
            unitCost { amount }
            measurement { weight { unit value } }
          }
        }
      }
    }
  }
}
"""

# The buy re-checks the gate live; only what order_gate needs, no line items.
ORDER_GATE_QUERY = """
query orderGate($id: ID!) {
  order(id: $id) {
    displayFulfillmentStatus
    displayFinancialStatus
    fullyPaid
    totalOutstandingSet { shopMoney { amount currencyCode } }
    fulfillmentOrders(first: 10) {
      nodes { id status fulfillmentHolds { reason reasonNotes } }
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
            "tags": node.get("tags") or [],
            "gate_label": order_gate.from_shopify_order(node)["label"],
        })
    return orders


ORDER_ID_BY_NAME_QUERY = """
query orderByName($query: String!) {
  orders(first: 1, query: $query) {
    nodes { id name }
  }
}
"""


def resolve_order(store_id, number):
    """Look up an order by its number (with or without the # prefix).
    Returns {"id": gid, "name": real order name} or None."""
    number = (number or "").strip().lstrip("#")
    data = _graphql(store_id, ORDER_ID_BY_NAME_QUERY, {"query": f"name:#{number}"})
    nodes = data["orders"]["nodes"]
    if not nodes:
        data = _graphql(store_id, ORDER_ID_BY_NAME_QUERY, {"query": f"name:{number}"})
        nodes = data["orders"]["nodes"]
    return nodes[0] if nodes else None


def find_order_by_number(store_id, number):
    order = resolve_order(store_id, number)
    return order["id"] if order else None


def _amount(money_set):
    return ((money_set or {}).get("shopMoney") or {}).get("amount")


def economics_from_order(order):
    """Whole-order economics from the order's current (post-edit) totals:
    revenue is the discounted subtotal plus the shipping the customer paid,
    cost is each line's current quantity at its variant's inventory unit cost."""
    currency = order.get("currencyCode")
    line_items = order.get("lineItems") or {}
    if (line_items.get("pageInfo") or {}).get("hasNextPage"):
        return profit.unavailable("lines_truncated", currency)
    lines = []
    for li in line_items.get("nodes") or []:
        qty = li.get("currentQuantity")
        inventory_item = (li.get("variant") or {}).get("inventoryItem") or {}
        lines.append({
            "description": li.get("title"),
            "sku": li.get("sku"),
            "quantity": li.get("quantity") if qty is None else qty,
            "unit_price": (_amount(li.get("discountedUnitPriceAfterAllDiscountsSet"))
                           or _amount(li.get("originalUnitPriceSet"))),
            "unit_cost": (inventory_item.get("unitCost") or {}).get("amount"),
        })
    shipping = _amount(order.get("currentShippingPriceSet"))
    if shipping is None:
        shipping = _amount(order.get("totalShippingPriceSet"))
    return profit.compute_economics(
        lines, _amount(order.get("currentSubtotalPriceSet")), shipping, currency)


def get_order(store_id, order_gid):
    data = _graphql(store_id, ORDER_DETAIL_QUERY, {"id": order_gid})
    order = data.get("order")
    if not order:
        raise ShopifyError("Order not found")
    addr = order.get("shippingAddress") or {}
    no_company = bool(_store(store_id).get("no_company"))
    existing_tracking = []
    for f in order.get("fulfillments") or []:
        if f.get("status") == "CANCELLED":
            continue
        for t in f.get("trackingInfo") or []:
            n = (t.get("number") or "").strip()
            if n and n != "0" and n not in existing_tracking:
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
        "tags": order.get("tags") or [],
        "note": (order.get("note") or "").strip(),
        "destination": {
            "company": None if no_company else addr.get("company"),
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
        "economics": economics_from_order(order),
        "gate": order_gate.from_shopify_order(order),
    }


def get_order_gate(store_id, order_gid):
    data = _graphql(store_id, ORDER_GATE_QUERY, {"id": order_gid})
    order = data.get("order")
    if not order:
        raise ShopifyError("Order not found")
    return order_gate.from_shopify_order(order)


# ---------- Pick lists ----------
# Only what eligibility, the rules and grouping by barcode need. Shopify sizes
# a query's cost by its `first:` arguments, so few line items per order let a
# page carry 20 orders under the 1000-point limit; the rare longer order has
# its remaining lines fetched afterwards.
PICKLIST_LINE_FRAGMENT = """
fragment PicklistLine on LineItem {
  title
  sku
  quantity
  currentQuantity
  refundableQuantity
  unfulfilledQuantity
  requiresShipping
  variant { barcode }
}
"""

PICKLIST_ORDER_FRAGMENT = """
fragment PicklistOrder on Order {
  id
  name
  createdAt
  updatedAt
  cancelledAt
  closed
  displayFinancialStatus
  displayFulfillmentStatus
  tags
  totalPriceSet { shopMoney { amount } }
  shippingLine { title }
  customer { id displayName tags }
  shippingAddress { name address1 address2 city provinceCode zip countryCodeV2 }
  billingAddress { address1 address2 city provinceCode zip countryCodeV2 }
  fulfillmentOrders(first: 5) { nodes { status } }
  risk { recommendation assessments { riskLevel } }
  lineItems(first: 15) {
    pageInfo { hasNextPage endCursor }
    nodes { ...PicklistLine }
  }
}
""" + PICKLIST_LINE_FRAGMENT
PICKLIST_LINES_PER_ORDER = 15  # the lineItems(first:) above

PICKLIST_ORDERS_QUERY = """
query picklistOrders($query: String!, $first: Int!, $sortKey: OrderSortKeys!, $after: String) {
  orders(first: $first, after: $after, query: $query, sortKey: $sortKey, reverse: true) {
    pageInfo { hasNextPage endCursor }
    nodes { ...PicklistOrder }
  }
}
""" + PICKLIST_ORDER_FRAGMENT

PICKLIST_ORDERS_BY_ID_QUERY = """
query picklistOrdersById($ids: [ID!]!) {
  nodes(ids: $ids) { ...PicklistOrder }
}
""" + PICKLIST_ORDER_FRAGMENT

PICKLIST_LINES_BY_ID_QUERY = """
query picklistLinesById($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Order {
      id
      lineItems(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes { ...PicklistLine }
      }
    }
  }
}
""" + PICKLIST_LINE_FRAGMENT

PICKLIST_ORDER_LINES_QUERY = """
query picklistOrderLines($id: ID!, $after: String) {
  order(id: $id) {
    lineItems(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { ...PicklistLine }
    }
  }
}
""" + PICKLIST_LINE_FRAGMENT

# Only recent orders are candidates; older unfulfilled ones are left to the Orders page.
PICKLIST_DAYS = 30
PICKLIST_MAX_PAGES = 100
PICKLIST_PAGE = 20
# A store's days are read as this many date windows at once; Shopify's
# throttle (much higher on Plus) decides how many actually run side by side.
PICKLIST_WINDOWS = 6
PICKLIST_IDS_CHUNK = 10
PICKLIST_LINES_CHUNK = 4


def _complete_line_items(store, nodes):
    """Fetch the rest of the line items of the orders whose first page was not
    all: a few orders per request, then page on for any still longer."""
    partial = [n for n in nodes if ((n.get("lineItems") or {}).get("pageInfo") or {}).get("hasNextPage")]
    for i in range(0, len(partial), PICKLIST_LINES_CHUNK):
        chunk = partial[i:i + PICKLIST_LINES_CHUNK]
        data = _graphql_for(store, PICKLIST_LINES_BY_ID_QUERY, {"ids": [n["id"] for n in chunk]})
        fetched = {m["id"]: m["lineItems"] for m in data.get("nodes") or [] if m}
        for n in chunk:
            n["lineItems"] = fetched.get(n["id"], n["lineItems"])
    for node in nodes:
        page = node.get("lineItems") or {}
        lines = list(page.get("nodes") or [])
        info = page.get("pageInfo") or {}
        while info.get("hasNextPage"):
            data = _graphql_for(store, PICKLIST_ORDER_LINES_QUERY, {"id": node["id"], "after": info["endCursor"]})
            more = ((data.get("order") or {}).get("lineItems")) or {}
            lines.extend(more.get("nodes") or [])
            info = more.get("pageInfo") or {}
        node["lineItems"] = {"nodes": lines}
    return nodes


def _order_pages(store, search, sort_key):
    """Every order the search finds, newest first. A page Shopify prices over
    its single-query limit is asked for again at half the size."""
    orders, after, first = [], None, PICKLIST_PAGE
    for _ in range(PICKLIST_MAX_PAGES):
        try:
            data = _graphql_for(store, PICKLIST_ORDERS_QUERY,
                                {"query": search, "first": first, "sortKey": sort_key, "after": after})
        except ShopifyCostExceeded:
            if first == 1:
                raise
            first //= 2
            continue
        page = data["orders"]
        orders.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    return orders


def _search_time(when):
    return f"'{when.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}'"


def picklist_since(now):
    """The first moment of the pick-list window: midnight UTC PICKLIST_DAYS ago."""
    return (now - timedelta(days=PICKLIST_DAYS)).astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)


def picklist_windows(since, until, count):
    """[since, until) cut into `count` equal (start, end) windows, oldest first.
    The last one has no end, so orders placed while it loads still count."""
    step = (until - since) / count
    return [(since + step * i, since + step * (i + 1) if i < count - 1 else None) for i in range(count)]


def picklist_search(since, until=None):
    """Shopify's search narrows the list; picklists.ineligible_reason has the
    final say."""
    created = f"created_at:>={_search_time(since)}" + (f" created_at:<{_search_time(until)}" if until else "")
    return (f"{created} status:open fulfillment_status:unfulfilled "
            "(financial_status:paid OR financial_status:partially_refunded)")


def list_picklist_orders(store_id, now=None):
    """The store's open, unfulfilled, paid orders of the last PICKLIST_DAYS
    days, as raw nodes with all their line items. The days are read as
    windows side by side; every page goes through _graphql's retries."""
    store = _store(store_id)
    now = now or datetime.now(timezone.utc)
    windows = picklist_windows(picklist_since(now), now, PICKLIST_WINDOWS)
    with ThreadPoolExecutor(max_workers=len(windows)) as pool:
        pages = list(pool.map(lambda w: _order_pages(store, picklist_search(*w), "CREATED_AT"), windows))
    orders = {}
    for nodes in pages:
        for node in nodes:
            orders.setdefault(node["id"], node)
    return _complete_line_items(store, list(orders.values()))


def list_changed_picklist_orders(store_id, changed_since, now=None):
    """Every order of the last PICKLIST_DAYS days changed since changed_since,
    whatever its status now, so orders that were fulfilled, cancelled or closed
    in the meantime show up too."""
    store = _store(store_id)
    now = now or datetime.now(timezone.utc)
    search = f"updated_at:>={_search_time(changed_since)} created_at:>={_search_time(picklist_since(now))}"
    return _complete_line_items(store, _order_pages(store, search, "UPDATED_AT"))


def get_picklist_orders(store_id, order_gids):
    """The given orders read live (for the pick list's final check); ids that
    no longer resolve to an order are simply missing from the result."""
    store = _store(store_id)
    orders = []
    for i in range(0, len(order_gids), PICKLIST_IDS_CHUNK):
        data = _graphql_for(store, PICKLIST_ORDERS_BY_ID_QUERY, {"ids": order_gids[i:i + PICKLIST_IDS_CHUNK]})
        orders.extend(n for n in data.get("nodes") or [] if n)
    return _complete_line_items(store, orders)


ORDER_WINDOW_LINE_FRAGMENT = """
fragment OrderWindowLine on LineItem {
  title
  variantTitle
  sku
  quantity
  currentQuantity
  refundableQuantity
  unfulfilledQuantity
  requiresShipping
  originalUnitPriceSet { shopMoney { amount } }
  discountedUnitPriceAfterAllDiscountsSet { shopMoney { amount } }
  image { url(transform: { maxWidth: 96, maxHeight: 96 }) altText }
  variant {
    barcode
    inventoryItem {
      unitCost { amount }
      measurement { weight { unit value } }
    }
  }
}
"""

ORDER_FRAUD_QUERY = """
query orderFraud($id: ID!) {
  order(id: $id) {
    id
    name
    createdAt
    email
    phone
    tags
    currencyCode
    displayFinancialStatus
    displayFulfillmentStatus
    totalPriceSet { shopMoney { amount currencyCode } }
    currentSubtotalPriceSet { shopMoney { amount } }
    currentShippingPriceSet { shopMoney { amount } }
    totalShippingPriceSet { shopMoney { amount } }
    subtotalLineItemsQuantity
    shippingLine { title }
    customer {
      id
      displayName
      numberOfOrders
      createdAt
      tags
      amountSpent { amount currencyCode }
    }
    billingAddress { firstName lastName address1 address2 city provinceCode countryCodeV2 zip phone }
    shippingAddress { firstName lastName address1 address2 city provinceCode countryCodeV2 zip phone }
    lineItems(first: 100) {
      pageInfo { hasNextPage endCursor }
      nodes { ...OrderWindowLine }
    }
    transactions(first: 20) {
      kind
      status
      gateway
      paymentDetails { ... on CardPaymentDetails { name company } }
    }
    clientIp
    risk {
      recommendation
      assessments {
        riskLevel
        facts { description sentiment }
        provider { title }
      }
    }
  }
}
""" + ORDER_WINDOW_LINE_FRAGMENT

ORDER_WINDOW_LINES_QUERY = """
query orderWindowLines($id: ID!, $after: String) {
  order(id: $id) {
    lineItems(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { ...OrderWindowLine }
    }
  }
}
""" + ORDER_WINDOW_LINE_FRAGMENT


def get_order_fraud(store_id, order_gid):
    """The raw order node behind the Pick lists fraud window."""
    data = _graphql(store_id, ORDER_FRAUD_QUERY, {"id": order_gid})
    order = data.get("order")
    if not order:
        raise ShopifyError("Order not found")
    page = order.get("lineItems") or {}
    lines, info = list(page.get("nodes") or []), page.get("pageInfo") or {}
    while info.get("hasNextPage"):
        more = ((_graphql(store_id, ORDER_WINDOW_LINES_QUERY, {"id": order_gid, "after": info["endCursor"]})
                 .get("order") or {}).get("lineItems")) or {}
        lines.extend(more.get("nodes") or [])
        info = more.get("pageInfo") or {}
    order["lineItems"] = {"nodes": lines}
    return order


# Shopify returns at most 250 shop tags; anything else can still be typed.
SHOP_TAGS_QUERIES = {
    "order": ("query picklistOrderTags { shop { orderTags(first: 250, sort: POPULAR) { nodes } } }", "orderTags"),
    "customer": ("query picklistCustomerTags { shop { customerTags(first: 250) { nodes } } }", "customerTags"),
}
TAGS_ADD_MUTATION = """
mutation picklistTagsAdd($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) { userErrors { field message } }
}
"""
TAGS_REMOVE_MUTATION = """
mutation picklistTagsRemove($id: ID!, $tags: [String!]!) {
  tagsRemove(id: $id, tags: $tags) { userErrors { field message } }
}
"""


def list_shop_tags(store_id, kind):
    """The store's order or customer tags, most used first where Shopify can sort them."""
    query, field = SHOP_TAGS_QUERIES[kind]
    data = _graphql(store_id, query)
    return list(((data.get("shop") or {}).get(field) or {}).get("nodes") or [])


def change_tags(store_id, resource_gid, add=(), remove=()):
    """Add and remove tags on an order or customer. Needs write_orders /
    write_customers on the store's token."""
    for mutation, field, tags in ((TAGS_ADD_MUTATION, "tagsAdd", add), (TAGS_REMOVE_MUTATION, "tagsRemove", remove)):
        if not tags:
            continue
        data = _graphql(store_id, mutation, {"id": resource_gid, "tags": list(tags)})
        errors = (data.get(field) or {}).get("userErrors") or []
        if errors:
            raise ShopifyError("; ".join(e.get("message") or "" for e in errors))


# Shopify builds tracking links only for carrier names spelled exactly as on
# its supported-companies list, and other apps on the store (Tracktor) rewrite
# the tracking info — dropping every number but one — when they see anything
# else. Ordered: the first keyword found in the provider's name wins.
SHOPIFY_TRACKING_COMPANIES = (
    ("usps", "USPS"),
    ("stamps", "USPS"),
    ("endicia", "USPS"),
    ("fedex", "FedEx"),
    ("dhlecommerce", "DHL eCommerce"),
    ("dhl", "DHL Express"),
    ("ups", "UPS"),
    ("canadapost", "Canada Post"),
    ("ontrac", "OnTrac"),
    ("lasership", "Lasership"),
)


def tracking_company(umbrella_name, courier_name):
    for name in (umbrella_name, courier_name):
        squashed = "".join(ch for ch in (name or "").lower() if ch.isalnum())
        for keyword, company in SHOPIFY_TRACKING_COMPANIES:
            if keyword in squashed:
                return company
    return courier_name or "Other"


def fulfill_order(store_id, order_gid, tracking_number, courier_name, all_numbers=None, umbrella_name=None):
    company = tracking_company(umbrella_name, courier_name)
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
        return _append_tracking(store_id, order_gid, numbers, company)
    tracking_info = {"company": company}
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
    created = result["fulfillment"]
    if all_numbers and len(all_numbers) > 1 and created and created.get("id"):
        # fulfillmentCreate has been seen keeping only one of the numbers;
        # fulfillmentTrackingInfoUpdate reliably sets the full list, so re-assert
        # every box's number on the new fulfillment (no second customer email).
        # If this follow-up fails, the writeback is marked errored and a retry
        # lands in _append_tracking, which merges the missing numbers back in.
        _update_tracking(
            store_id, created["id"],
            {"company": company, "numbers": list(all_numbers)},
            notify=False,
        )
    return created


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


def _append_tracking(store_id, order_gid, numbers, new_company):
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
    tracking_info = {"company": tracking_company(None, company) if company else new_company, "numbers": merged}
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

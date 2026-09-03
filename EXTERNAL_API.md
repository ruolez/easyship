# EasyShip External Order Lookup API

Read-only API for external systems (e.g. profit calculation) to fetch shipping costs, tracking numbers, and parcel details for orders shipped through EasyShip.

## Endpoint

```
POST http://<easyship-host>:<port>/api/external/orders/lookup
Content-Type: application/json
```

- The port depends on the deployment: production installs (via `install.sh`) default to **80** for HTTP and **443** for HTTPS; a local dev checkout defaults to **5557**/**5558**. The actual values are `APP_PORT` / `APP_HTTPS_PORT` in the server's `.env`. When the port is 80 you can omit it from the URL.
- HTTPS uses a self-signed certificate — disable TLS verification or trust the cert.
- **No authentication.** The API is intended for LAN use only. Do not expose these ports to the internet.
- The endpoint is read-only; it never modifies shipping data.

## Request

```json
{ "order_numbers": ["1001", "#1002", "INV-4521"] }
```

Rules:

- `order_numbers` must be a non-empty JSON array of strings (integers are tolerated), **max 500 entries per request**. Batch larger sets into multiple requests.
- Matching is **case-insensitive** and a **leading `#` is ignored**, so `"1001"`, `"#1001"`, and `"#1001".lower()` all match Shopify order `#1001`.
- An order number is matched against BOTH identifier types: Shopify order names and BackOffice invoice numbers. You do not need to know which system an order came from.
- Duplicate entries in the request are collapsed to one result.

## Response — `200 OK`

```json
{
  "orders": [
    {
      "order_number": "#1001",
      "source": "shopify",
      "store_name": "My Store",
      "db_name": null,
      "status": "fulfilled",
      "total_shipping_cost": 20.51,
      "tracking_numbers": ["9400...0001", "9400...0002"],
      "shipments": [
        {
          "group_id": "a1b2c3",
          "courier": "USPS Priority Mail",
          "courier_umbrella": "USPS",
          "label_created_at": "2026-08-30T14:05:00Z",
          "shipping_cost": 20.51,
          "boxes": [
            {
              "box_number": 1,
              "box_total": 2,
              "box_size": "12×10×6",
              "length": 12.0,
              "width": 10.0,
              "height": 6.0,
              "weight_lb": 4.5,
              "shipping_cost": 9.21,
              "tracking_number": "9400...0001"
            }
          ]
        }
      ]
    }
  ],
  "not_found": ["INV-4521"]
}
```

### Field reference

Order level:

| Field | Type | Meaning |
|---|---|---|
| `order_number` | string | Echoed back exactly as you sent it. |
| `source` | `"shopify"` \| `"backoffice"` | Which identifier type matched. |
| `store_name` | string \| null | Shopify store name (when source is shopify). |
| `db_name` | string \| null | BackOffice database name (when source is backoffice). |
| `status` | `"label_created"` \| `"fulfilled"` | `fulfilled` means tracking was also written back to the source system. |
| `total_shipping_cost` | number | **Sum of all label costs for the order, in USD.** Use this for profit calculation. |
| `tracking_numbers` | string[] | All tracking numbers across all boxes/shipments, deduplicated. |
| `shipments` | array | One entry per label purchase (see below). |

Shipment level (one per label purchase; an order re-shipped later has two entries, and both are included in `total_shipping_cost`):

| Field | Type | Meaning |
|---|---|---|
| `group_id` | string | Internal id grouping the boxes of one label purchase. |
| `courier` | string \| null | Full service name, e.g. `"USPS Priority Mail"`. |
| `courier_umbrella` | string \| null | Carrier family, e.g. `"USPS"`, `"FedEx"`. |
| `label_created_at` | string \| null | ISO-8601 UTC timestamp (`...Z`) of label purchase. |
| `shipping_cost` | number | Cost of this label purchase (sum of its boxes). |
| `boxes` | array | One entry per physical box. |

Box level:

| Field | Type | Meaning |
|---|---|---|
| `box_number` / `box_total` | int | Box 1 of N. |
| `box_size` | string \| null | `"L×W×H"` in inches (separator is `×`, U+00D7). `null` when dimensions were not recorded. |
| `length`, `width`, `height` | number \| null | Inches. May be `null` (dimensions are optional at ship time). |
| `weight_lb` | number \| null | Pounds. |
| `shipping_cost` | number \| null | This box's share of the label cost, USD. |
| `tracking_number` | string \| null | This box's tracking number. |

## Semantics you must handle

1. **Every requested number appears exactly once** — either in `orders` or in `not_found`. `not_found` means: no purchased label exists for that order (unknown order, or its shipment is still a draft/quote, or the label was voided/errored). It does NOT distinguish "order doesn't exist" from "not shipped yet" — treat it as "no shipping cost available yet" and retry later if the order is recent.
2. **Only real spend is returned.** Draft, rated-but-not-purchased, voided, and errored shipments are excluded. `total_shipping_cost` is money actually spent on labels.
3. **Multi-box orders** return multiple `boxes` entries; **re-shipped orders** return multiple `shipments` entries. If you only care about cost per order, just read `total_shipping_cost` and ignore the nesting.
4. **Nullable fields**: dimensions, weight, `box_size`, courier, and `label_created_at` can be `null`. Never assume they are present.
5. Costs are USD with standard JSON numbers (e.g. `11.3`, not `"11.30"`).

## Errors

All errors return JSON `{"error": "<message>"}`:

| Status | Cause |
|---|---|
| 400 | Body is not a JSON object / `order_numbers` missing, empty, or not a list / more than 500 entries / non-string entries |
| 405 | Wrong HTTP method (must be POST) |
| 5xx | Server/database problem — retry with backoff |

There is no rate limit, but be a good citizen: batch lookups (up to 500 per call) instead of one request per order.

## Health check

`GET http://<easyship-host>:<port>/api/health` → `{"status": "ok"}` — use this to verify connectivity before syncing.

## Example client (Python)

```python
import requests

EASYSHIP_URL = "http://192.168.89.14"  # production default (port 80); use :5557 for a dev checkout

def fetch_shipping_costs(order_numbers: list[str]) -> dict[str, dict]:
    """Returns {order_number_as_sent: order_payload} for found orders."""
    results = {}
    for i in range(0, len(order_numbers), 500):
        batch = order_numbers[i:i + 500]
        r = requests.post(
            f"{EASYSHIP_URL}/api/external/orders/lookup",
            json={"order_numbers": batch},
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
        for order in payload["orders"]:
            results[order["order_number"]] = order
        # payload["not_found"] lists numbers with no purchased label yet
    return results

orders = fetch_shipping_costs(["1001", "#1002", "INV-4521"])
for number, o in orders.items():
    print(number, o["total_shipping_cost"], o["tracking_numbers"])
```

## Example (curl)

```bash
curl -s http://192.168.89.14/api/external/orders/lookup \
  -H 'Content-Type: application/json' \
  -d '{"order_numbers":["1001","INV-4521"]}'
```

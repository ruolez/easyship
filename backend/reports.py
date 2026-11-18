"""Shipping spend from the parcel log and one BackOffice database, merged
into one list of rows {source, date, store, carrier, service, ref, tracking,
cost, shipment} and summarised by period, carrier and store."""
from datetime import timedelta

import backoffice
import db
from shipments_api import CARRIER_SQL, SERVICE_NAME_SQL

SETTING_DB = "reports_backoffice_db_id"
BUCKETS = ("day", "week", "month")
SOURCES = {"parcels": "Parcel log", "backoffice": "BackOffice"}
UNKNOWN = "Unknown"
COUNTED_STATUSES = ("label_created", "fulfilled")


def period_start(day, bucket):
    if bucket == "week":
        return day - timedelta(days=day.weekday())
    if bucket == "month":
        return day.replace(day=1)
    return day


def next_period(start, bucket):
    if bucket == "week":
        return start + timedelta(days=7)
    if bucket == "month":
        return (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start + timedelta(days=1)


def merge_names(rows, field):
    """One spelling per name, ignoring case and outer spaces; the parcel log's
    spelling wins, empty becomes Unknown. Rewrites the rows in place."""
    spelling = {}
    for row in sorted(rows, key=lambda r: r["source"] != "parcels"):
        name = (row[field] or "").strip() or UNKNOWN
        spelling.setdefault(name.lower(), name)
    for row in rows:
        row[field] = spelling[((row[field] or "").strip() or UNKNOWN).lower()]
    return rows


def _tracking_key(value):
    return (value or "").strip().upper()


def drop_duplicates(bo_rows, parcel_tracking, parcel_invoice_ids):
    """BackOffice invoices whose shipment is already in the parcel log go.
    Matched by tracking number; by invoice only when the invoice has none,
    because a re-ship keeps the invoice's earlier (real) TrackingNo and cost."""
    tracking = {_tracking_key(t) for t in parcel_tracking if t}
    kept = []
    for row in bo_rows:
        if row["tracking"]:
            if _tracking_key(row["tracking"]) in tracking:
                continue
        elif row["invoice_id"] in parcel_invoice_ids:
            continue
        kept.append(row)
    return kept


def filter_rows(rows, carriers=(), stores=(), sources=()):
    """Empty selection = everything; names compare ignoring case."""
    carriers = {c.lower() for c in carriers}
    stores = {s.lower() for s in stores}
    sources = set(sources)
    return [r for r in rows
            if (not carriers or r["carrier"].lower() in carriers)
            and (not stores or r["store"].lower() in stores)
            and (not sources or r["source"] in sources)]


def _breakdown(rows, field, total_cost):
    groups = {}
    for r in rows:
        g = groups.setdefault(r[field], {"cost": 0.0, "shipments": set()})
        g["cost"] += r["cost"]
        g["shipments"].add(r["shipment"])
    out = [{
        "name": name,
        "cost": round(g["cost"], 2),
        "shipments": len(g["shipments"]),
        "avg": round(g["cost"] / len(g["shipments"]), 2),
        "share": round(g["cost"] / total_cost * 100, 1) if total_cost else 0.0,
    } for name, g in groups.items()]
    return sorted(out, key=lambda x: (-x["cost"], x["name"].lower()))


def summarize(rows, bucket, date_from, date_to):
    """Totals, per carrier, per store and per period (every period of the
    range, empty ones included, so a chart shows the gaps)."""
    cost = sum(r["cost"] for r in rows)
    shipments = len({r["shipment"] for r in rows})
    periods = {}
    start = period_start(date_from, bucket)
    while start <= date_to:
        periods[start] = {"cost": 0.0, "shipments": set(), "carriers": {}, "stores": {}}
        start = next_period(start, bucket)
    for r in rows:
        p = periods[period_start(r["date"], bucket)]
        p["cost"] += r["cost"]
        p["shipments"].add(r["shipment"])
        p["carriers"][r["carrier"]] = p["carriers"].get(r["carrier"], 0.0) + r["cost"]
        p["stores"][r["store"]] = p["stores"].get(r["store"], 0.0) + r["cost"]
    return {
        "totals": {"cost": round(cost, 2), "shipments": shipments,
                   "avg": round(cost / shipments, 2) if shipments else 0.0},
        "by_carrier": _breakdown(rows, "carrier", cost),
        "by_store": _breakdown(rows, "store", cost),
        "by_period": [{
            "period": start.isoformat(),
            "cost": round(p["cost"], 2),
            "shipments": len(p["shipments"]),
            "carriers": {k: round(v, 2) for k, v in p["carriers"].items()},
            "stores": {k: round(v, 2) for k, v in p["stores"].items()},
        } for start, p in periods.items()],
    }


def parcel_spend_rows(date_from, date_to):
    rows = db.query(
        f"""SELECT s.id, s.group_id, (s.created_at AT TIME ZONE 'America/Chicago')::date AS day,
                   {SERVICE_NAME_SQL} AS store, {CARRIER_SQL} AS carrier, s.courier_name AS service,
                   COALESCE(s.shopify_order_name, s.backoffice_invoice_number) AS ref,
                   s.tracking_number, s.shipping_cost
            FROM shipments s
            LEFT JOIN shopify_stores ss ON ss.id = s.shopify_store_id
            LEFT JOIN backoffice_dbs bd ON bd.id = s.backoffice_db_id
            WHERE s.status = ANY(%s) AND s.shipping_cost IS NOT NULL
              AND (s.created_at AT TIME ZONE 'America/Chicago')::date BETWEEN %s AND %s""",
        (list(COUNTED_STATUSES), date_from, date_to),
    )
    return [{
        "source": "parcels",
        "date": r["day"],
        "store": r["store"] or "",
        "carrier": r["carrier"] or "",
        "service": r["service"] or "",
        "ref": r["ref"] or f"#{r['id']}",
        "tracking": r["tracking_number"] or "",
        "cost": float(r["shipping_cost"]),
        "shipment": r["group_id"] or f"#{r['id']}",
    } for r in rows]


def parcel_matches(db_id, invoice_ids, tracking):
    """(tracking numbers, invoice ids of db_id) of live parcels among the ones
    given, so the lookup isn't tied to the report's date range."""
    if not invoice_ids and not tracking:
        return set(), set()
    rows = db.query(
        """SELECT backoffice_db_id, backoffice_invoice_id, tracking_number FROM shipments
           WHERE status <> 'voided'
             AND ((backoffice_db_id = %s AND backoffice_invoice_id = ANY(%s))
                  OR tracking_number = ANY(%s))""",
        (db_id, list(invoice_ids), sorted({v for t in tracking for v in (t, _tracking_key(t))})),
    )
    return ({r["tracking_number"] for r in rows if r["tracking_number"]},
            {r["backoffice_invoice_id"] for r in rows if r["backoffice_db_id"] == db_id})


def backoffice_spend_rows(db_id, store_name, date_from, date_to):
    invoices = backoffice.shipping_spend_invoices(db_id, date_from, date_to)
    tracking, invoice_ids = parcel_matches(
        db_id, {i["invoice_id"] for i in invoices}, {i["tracking"] for i in invoices if i["tracking"]})
    return [{
        "source": "backoffice",
        "date": i["date"],
        "store": store_name,
        "carrier": i["carrier"],
        "service": "",
        "ref": i["invoice_number"] or f"#{i['invoice_id']}",
        "tracking": i["tracking"],
        "cost": i["cost"],
        "shipment": f"bo:{i['invoice_id']}",
        "invoice_id": i["invoice_id"],
    } for i in drop_duplicates(invoices, tracking, invoice_ids)]


def reports_db_id():
    raw = (db.get_setting(SETTING_DB) or "").strip()
    return int(raw) if raw.isdigit() else None


def spend_rows(date_from, date_to):
    """Both sources, names merged. The parcel log always answers; a missing or
    unreachable BackOffice becomes a notice instead of an error."""
    rows = parcel_spend_rows(date_from, date_to)
    notices = []
    db_id = reports_db_id()
    if db_id is None:
        notices.append("No BackOffice database is chosen for Reports (Settings › Reports): "
                       "showing the parcel log only.")
    else:
        try:
            cfg = backoffice.get_db_config(db_id)
            rows += backoffice_spend_rows(db_id, cfg["name"], date_from, date_to)
        except Exception as e:  # noqa: BLE001 — any BackOffice failure leaves the parcel log
            notices.append(f"BackOffice could not be read ({e}): showing the parcel log only.")
    merge_names(rows, "carrier")
    merge_names(rows, "store")
    return rows, notices

"""Shipping spend from the parcel log, one BackOffice database and a Google
Sheet tab, merged into one list of rows {source, date, store, carrier,
sales_rep, service, ref, tracking, cost, shipment} and summarised by period,
carrier and store."""
import csv
import io
import re
from datetime import date, datetime, timedelta

import requests

import backoffice
import db
import verify
from shipments_api import CARRIER_SQL, SERVICE_NAME_SQL

SETTING_DB = "reports_backoffice_db_id"
SETTING_SHEET = "reports_sheet_url"
BUCKETS = ("day", "week", "month")
SOURCES = {"parcels": "Parcel log", "backoffice": "BackOffice", "sheet": "Google Sheet"}
UNKNOWN = "Unknown"
NO_REP = "No sales rep"
COUNTED_STATUSES = ("label_created", "fulfilled")

# Postage providers and ShipStation's own carrier accounts are not carriers:
# a label bought through Endicia or Stamps.com is a USPS label.
USPS_RESELLERS = {"endicia", "stamps.com", "stamps_com", "stamps", "usps"}
ACCOUNT_SUFFIXES = (" by shipstation", " one balance")


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


def carrier_name(raw):
    """The carrier a label was shipped with, from the name the provider gave
    it: "Endicia" / "Stamps.com" are USPS, "UPS by ShipStation" is UPS, and a
    ShipStation account nickname ("UPS · East") is dropped."""
    name = (raw or "").split(" · ")[0].strip()
    lowered = name.lower()
    for suffix in ACCOUNT_SUFFIXES:
        if lowered.endswith(suffix):
            name = name[:-len(suffix)].strip()
            lowered = name.lower()
    return "USPS" if lowered in USPS_RESELLERS else name


def merge_names(rows, field, empty=UNKNOWN):
    """One spelling per name, ignoring case and outer spaces; the parcel log's
    spelling wins, empty becomes `empty`. Rewrites the rows in place."""
    spelling = {}
    for row in sorted(rows, key=lambda r: r["source"] != "parcels"):
        name = (row[field] or "").strip() or empty
        spelling.setdefault(name.lower(), name)
    for row in rows:
        row[field] = spelling[((row[field] or "").strip() or empty).lower()]
    return rows


def _tracking_key(value):
    return (value or "").strip().upper()


def drop_duplicates(rows, known_tracking, shipped, key="invoice_id"):
    """Rows whose shipment another source already has go. Matched by tracking
    number; by invoice / order (`key`) only when the row has none, because a
    re-ship keeps the invoice's earlier (real) TrackingNo and cost."""
    tracking = {_tracking_key(t) for t in known_tracking if t}
    kept = []
    for row in rows:
        if row["tracking"]:
            if _tracking_key(row["tracking"]) in tracking:
                continue
        elif row[key] in shipped:
            continue
        kept.append(row)
    return kept


def filter_rows(rows, carriers=(), stores=(), sources=(), reps=()):
    """Empty selection = everything; names compare ignoring case."""
    carriers = {c.lower() for c in carriers}
    stores = {s.lower() for s in stores}
    reps = {r.lower() for r in reps}
    sources = set(sources)
    return [r for r in rows
            if (not carriers or r["carrier"].lower() in carriers)
            and (not stores or r["store"].lower() in stores)
            and (not reps or r["sales_rep"].lower() in reps)
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
                   s.tracking_number, s.shipping_cost, s.backoffice_db_id, s.backoffice_invoice_id
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
        "carrier": carrier_name(r["carrier"]),
        "service": r["service"] or "",
        "ref": r["ref"] or f"#{r['id']}",
        "tracking": r["tracking_number"] or "",
        "cost": float(r["shipping_cost"]),
        "shipment": r["group_id"] or f"#{r['id']}",
        "sales_rep": "",
        "bo_db_id": r["backoffice_db_id"],
        "invoice_id": r["backoffice_invoice_id"],
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
        "carrier": carrier_name(i["carrier"]),
        "service": "",
        "ref": i["invoice_number"] or f"#{i['invoice_id']}",
        "tracking": i["tracking"],
        "cost": i["cost"],
        "shipment": f"bo:{i['invoice_id']}",
        "invoice_id": i["invoice_id"],
        "sales_rep": i["sales_rep"],
    } for i in drop_duplicates(invoices, tracking, invoice_ids)]


def add_parcel_sales_reps(rows, db_id):
    """Parcels shipped for an invoice of the Reports database take that
    invoice's sales rep; Shopify orders and other databases have none."""
    linked = [r for r in rows if r["source"] == "parcels" and r.get("bo_db_id") == db_id and r.get("invoice_id")]
    if not linked:
        return
    reps = backoffice.invoice_sales_reps(db_id, {r["invoice_id"] for r in linked})
    for r in linked:
        r["sales_rep"] = reps.get(r["invoice_id"], "")


class SheetError(Exception):
    pass


SHEET_ID = re.compile(r"^https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,})(?:[/?#]|$)")
SHEET_GID = re.compile(r"[?#&]gid=(\d+)")
# Header (lower case) per field; order, cost and date must be there.
SHEET_COLUMNS = {"order": "order id", "carrier": "tracking_company", "tracking": "track num",
                 "link_text": "track your package", "cost": "shipping cost", "date": "date"}
SHEET_REQUIRED = {"order": "Order ID", "cost": "Shipping Cost", "date": "DATE"}


def sheet_csv_url(link):
    """The CSV export of the tab a Google Sheets link points at (gid 0 = the
    first tab), or None when it isn't one. Only this URL is ever fetched."""
    match = SHEET_ID.match((link or "").strip())
    if not match:
        return None
    gid = SHEET_GID.search(link)
    return (f"https://docs.google.com/spreadsheets/d/{match.group(1)}"
            f"/export?format=csv&gid={gid.group(1) if gid else 0}")


def _sheet_date(value):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def _sheet_cost(value):
    try:
        return float(value.replace("$", "").replace(",", ""))
    except ValueError:
        return None


def _store_name(order, stores):
    try:
        store = verify.detect_store(order, stores)
    except ValueError:  # two stores share the prefix: don't guess
        return ""
    return store["name"] if store else ""


def parse_sheet(text, stores):
    """Spend rows from the sheet's CSV. A row needs an Order ID, a cost above
    zero and a date; the store comes from the order-number prefix."""
    reader = csv.reader(io.StringIO(text))
    header = [h.strip().lower() for h in next(reader, [])]
    missing = [label for field, label in SHEET_REQUIRED.items() if SHEET_COLUMNS[field] not in header]
    if missing:
        raise ValueError(f"the sheet has no {', '.join(missing)} column")
    index = {f: header.index(name) for f, name in SHEET_COLUMNS.items() if name in header}

    def cell(row, field):
        i = index.get(field)
        return row[i].strip() if i is not None and i < len(row) else ""

    rows = []
    for r in reader:
        order = cell(r, "order")
        cost = _sheet_cost(cell(r, "cost"))
        day = _sheet_date(cell(r, "date"))
        if not order or not cost or cost <= 0 or not day:
            continue
        tracking = cell(r, "tracking")
        if not tracking and cell(r, "link_text").lower() != "track your package":
            tracking = cell(r, "link_text")  # older rows keep the number in that column
        order_key = order.lstrip("#").upper()
        rows.append({
            "source": "sheet",
            "date": day,
            "store": _store_name(order, stores),
            "carrier": carrier_name(cell(r, "carrier")),
            "service": "",
            "ref": order,
            "tracking": tracking,
            "cost": cost,
            "shipment": f"sheet:{order_key}",
            "sales_rep": "",
            "order_key": order_key,
        })
    return rows


def sheet_link():
    return (db.get_setting(SETTING_SHEET) or "").strip()


def sheet_stores():
    return db.query("SELECT name, prefix FROM shopify_stores") or []


def fetch_sheet(url):
    try:
        res = requests.get(url, timeout=20)
    except requests.RequestException as e:
        raise SheetError(f"Google did not answer: {e.__class__.__name__}")
    if res.status_code in (401, 403):
        raise SheetError("it isn't shared with \u201cAnyone with the link\u201d")
    if res.status_code == 404:
        raise SheetError("the sheet or tab was not found")
    if res.status_code != 200 or "text/csv" not in res.headers.get("Content-Type", ""):
        raise SheetError(f"Google answered {res.status_code}")
    res.encoding = "utf-8"
    return res.text


def parcel_order_matches(tracking, order_keys):
    """(tracking numbers, upper-case Shopify order names) of live parcels
    among the ones given."""
    if not tracking and not order_keys:
        return set(), set()
    rows = db.query(
        """SELECT tracking_number, UPPER(LTRIM(shopify_order_name, '#')) AS order_key FROM shipments
           WHERE status <> 'voided'
             AND (tracking_number = ANY(%s) OR LOWER(LTRIM(shopify_order_name, '#')) = ANY(%s))""",
        (sorted({v for t in tracking for v in (t, t.lower())}), sorted(k.lower() for k in order_keys)),
    )
    return ({_tracking_key(r["tracking_number"]) for r in rows if r["tracking_number"]},
            {r["order_key"] for r in rows if r["order_key"] in order_keys})


def sheet_spend_rows(link, date_from, date_to, other_rows):
    """The sheet's rows in the range that neither the parcel log nor
    BackOffice already has (same tracking, or no tracking and the order was
    shipped through EasyShip)."""
    url = sheet_csv_url(link)
    if not url:
        raise SheetError("the link in Settings › Reports is not a Google Sheets link")
    try:
        rows = parse_sheet(fetch_sheet(url), sheet_stores())
    except ValueError as e:
        raise SheetError(str(e))
    rows = [r for r in rows if date_from <= r["date"] <= date_to]
    tracking, orders = parcel_order_matches(
        {_tracking_key(r["tracking"]) for r in rows if r["tracking"]},
        {r["order_key"] for r in rows if not r["tracking"]})
    tracking |= {r["tracking"] for r in other_rows if r["source"] == "backoffice" and r["tracking"]}
    return drop_duplicates(rows, tracking, orders, key="order_key")


def sources_key():
    """What the report reads from; part of the cache key."""
    return reports_db_id(), sheet_link()


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
                       "its invoices are left out.")
    else:
        try:
            cfg = backoffice.get_db_config(db_id)
            rows += backoffice_spend_rows(db_id, cfg["name"], date_from, date_to)
            add_parcel_sales_reps(rows, db_id)
        except Exception as e:  # noqa: BLE001 — any BackOffice failure leaves the parcel log
            notices.append(f"BackOffice could not be read ({e}): its invoices are left out.")
    link = sheet_link()
    if link:
        try:
            rows += sheet_spend_rows(link, date_from, date_to, rows)
        except SheetError as e:
            notices.append(f"The Google Sheet could not be read ({e}): its rows are left out.")
    merge_names(rows, "carrier")
    merge_names(rows, "store")
    merge_names(rows, "sales_rep", empty=NO_REP)
    return rows, notices

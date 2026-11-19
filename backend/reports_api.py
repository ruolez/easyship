import csv
import io
import threading
import time
from datetime import date

from flask import Blueprint, Response, jsonify, request

import reports
from auth import section_required
from util import api_error

bp = Blueprint("reports", __name__, url_prefix="/api/reports")

MAX_RANGE_DAYS = 731
ROWS_PAGE_MAX = 500
CACHE_SECONDS = 60

# The summary, the detail pages and the CSV of one range are asked for one
# after another; the BackOffice read behind them is the slow part.
_cache = {}
_cache_lock = threading.Lock()


def _cached_rows(date_from, date_to, fresh):
    # The chosen database and sheet are part of the key so a Settings change shows at once.
    key = (date_from, date_to, reports.sources_key())
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not fresh and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    result = reports.spend_rows(date_from, date_to)
    with _cache_lock:
        for k in [k for k, (at, _) in _cache.items() if now - at >= CACHE_SECONDS]:
            del _cache[k]
        _cache[key] = (now, result)
    return result


def _list_arg(name):
    return [v for v in (request.args.get(name) or "").split(",") if v.strip()]


def _params():
    """(date_from, date_to, bucket) from the query string; ValueError if bad."""
    try:
        date_from = date.fromisoformat((request.args.get("from") or "").strip())
        date_to = date.fromisoformat((request.args.get("to") or "").strip())
    except ValueError:
        raise ValueError("Choose a start and end date")
    if date_from > date_to:
        raise ValueError("The start date is after the end date")
    if (date_to - date_from).days >= MAX_RANGE_DAYS:
        raise ValueError("Choose a range of two years or less")
    bucket = request.args.get("bucket") or "day"
    if bucket not in reports.BUCKETS:
        raise ValueError("Group by day, week or month")
    return date_from, date_to, bucket


def _report():
    date_from, date_to, bucket = _params()
    rows, notices = _cached_rows(date_from, date_to, request.args.get("fresh") == "1")
    selected = reports.filter_rows(rows, _list_arg("carrier"), _list_arg("store"), _list_arg("source"),
                                   _list_arg("rep"))
    return date_from, date_to, bucket, rows, selected, notices


def _names(rows, field):
    return sorted({r[field] for r in rows}, key=str.lower)


@bp.get("/spend")
@section_required("reports")
def spend():
    try:
        date_from, date_to, bucket, rows, selected, notices = _report()
    except ValueError as e:
        return api_error(str(e))
    return jsonify({
        **reports.summarize(selected, bucket, date_from, date_to),
        "options": {
            "carriers": _names(rows, "carrier"),
            "stores": _names(rows, "store"),
            "reps": _names(rows, "sales_rep"),
            "sources": [{"value": k, "label": v} for k, v in reports.SOURCES.items()],
        },
        "notices": notices,
    })


ROW_SORTS = {
    "date": lambda r: (r["date"], r["ref"]),
    "cost": lambda r: (r["cost"], r["date"]),
    "carrier": lambda r: (r["carrier"].lower(), r["date"]),
    "store": lambda r: (r["store"].lower(), r["date"]),
    "rep": lambda r: (r["sales_rep"].lower(), r["date"]),
}


def _row_json(r):
    return {"date": r["date"].isoformat(), "source": r["source"], "store": r["store"],
            "carrier": r["carrier"], "service": r["service"], "ref": r["ref"],
            "tracking": r["tracking"], "cost": r["cost"], "sales_rep": r["sales_rep"]}


def _sorted(rows):
    key = ROW_SORTS.get(request.args.get("sort") or "date", ROW_SORTS["date"])
    return sorted(rows, key=key, reverse=request.args.get("dir", "desc") == "desc")


@bp.get("/spend/rows")
@section_required("reports")
def spend_rows():
    try:
        limit = max(1, min(int(request.args.get("limit") or 100), ROWS_PAGE_MAX))
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        return api_error("Paging must be whole numbers")
    try:
        _, _, _, _, selected, _ = _report()
    except ValueError as e:
        return api_error(str(e))
    page = _sorted(selected)[offset:offset + limit]
    return jsonify({"rows": [_row_json(r) for r in page], "total": len(selected),
                    "offset": offset, "limit": limit})


def _csv_table(kind, date_from, date_to, bucket, selected):
    if kind == "rows":
        header = ["Date", "Source", "Store", "Sales rep", "Carrier", "Service", "Reference", "Tracking", "Cost"]
        body = [[r["date"].isoformat(), reports.SOURCES[r["source"]], r["store"], r["sales_rep"], r["carrier"],
                 r["service"], r["ref"], r["tracking"], f"{r['cost']:.2f}"] for r in _sorted(selected)]
        return header, body
    summary = reports.summarize(selected, bucket, date_from, date_to)
    if kind in ("carrier", "store"):
        header = [kind.capitalize(), "Shipments", "Spend", "Average", "Share %"]
        body = [[g["name"], g["shipments"], f"{g['cost']:.2f}", f"{g['avg']:.2f}", f"{g['share']:.1f}"]
                for g in summary[f"by_{kind}"]]
        return header, body
    carriers = [g["name"] for g in summary["by_carrier"]]
    header = ["Period", "Shipments", "Spend"] + carriers
    body = [[p["period"], p["shipments"], f"{p['cost']:.2f}"]
            + [f"{p['carriers'].get(c, 0):.2f}" for c in carriers] for p in summary["by_period"]]
    return header, body


@bp.get("/spend.csv")
@section_required("reports")
def spend_csv():
    kind = request.args.get("kind") or "rows"
    if kind not in ("rows", "carrier", "store", "period"):
        return api_error("Unknown export")
    try:
        date_from, date_to, bucket, _, selected, _ = _report()
    except ValueError as e:
        return api_error(str(e))
    header, body = _csv_table(kind, date_from, date_to, bucket, selected)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(body)
    name = f"shipping-spend-{kind}-{date_from.isoformat()}-to-{date_to.isoformat()}.csv"
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})

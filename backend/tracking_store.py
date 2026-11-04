"""Carrier tracking events: storage, and each parcel's latest status.

Providers hand over flat event dicts (see providers/uds.normalize_tracking):
{tracking_number, event_id, status_code, status, description, detail,
 exception_detail, event_at, location, signed_by, left_at, vpod_url,
 signature_png, photos, ...}. Events are kept per (provider, number,
event_id), so reading the same events twice changes nothing.
"""
import json
from datetime import datetime

import db

DELIVERED = "delivered"
STORED_FIELDS = ("status_code", "status", "description", "detail", "exception_detail", "location",
                 "signed_by", "left_at", "vpod_url", "signature_png")


def parse_time(value):
    """An aware datetime from the carrier's ISO time, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def summarize(events):
    """(latest status, its time, first delivered time) for one parcel's
    events. Latest = newest event_at, then highest event id; events without a
    time count as oldest."""
    if not events:
        return None, None, None
    def key(e):
        at = parse_time(e.get("event_at"))
        return (at.timestamp() if at else float("-inf"), e.get("event_id") or 0)

    latest = max(events, key=key)
    delivered = sorted(at for at in (parse_time(e.get("event_at")) for e in events
                                     if e.get("status") == DELIVERED) if at)
    return latest.get("status"), parse_time(latest.get("event_at")), (delivered[0] if delivered else None)


def save(provider, events):
    """Store new events and refresh the matching parcels' tracking status.
    Returns the tracking numbers whose parcels were updated."""
    numbers = []
    for e in events or []:
        number = str(e.get("tracking_number") or "").strip()
        if not number or e.get("event_id") is None:
            continue
        db.execute(
            """INSERT INTO carrier_tracking_events
                 (provider, tracking_number, event_id, status_code, status, description, detail,
                  exception_detail, event_at, location, signed_by, left_at, vpod_url, signature_png,
                  photos, raw)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (provider, tracking_number, event_id) DO NOTHING""",
            (provider, number, int(e["event_id"]),
             *(e.get(f) or None for f in STORED_FIELDS[:5]),
             parse_time(e.get("event_at")),
             *(e.get(f) or None for f in STORED_FIELDS[5:]),
             json.dumps(e.get("photos") or []),
             json.dumps({k: e.get(k) for k in ("rescheduled_for", "order_ref", "delivery_date")})),
        )
        if number not in numbers:
            numbers.append(number)
    updated = []
    for number in numbers:
        stored = events_for(provider, [number])
        status, at, delivered_at = summarize(stored)
        row = db.execute(
            """UPDATE shipments SET tracking_status = %s, tracking_status_at = %s,
                      delivered_at = %s, updated_at = now()
               WHERE provider = %s AND (tracking_number = %s OR tracking_numbers ? %s)
               RETURNING id""",
            (status, at, delivered_at, provider, number, number),
            returning=True,
        )
        if row:
            updated.append(number)
    return updated


def events_for(provider, numbers):
    """Stored events for these numbers, newest first."""
    numbers = [n for n in numbers if n]
    if not numbers:
        return []
    rows = db.query(
        """SELECT tracking_number, event_id, status_code, status, description, detail,
                  exception_detail, event_at, location, signed_by, left_at, vpod_url,
                  signature_png, photos, raw
           FROM carrier_tracking_events
           WHERE provider = %s AND tracking_number = ANY(%s)
           ORDER BY event_at DESC NULLS LAST, event_id DESC""",
        (provider, numbers),
    ) or []
    out = []
    for r in rows:
        event = dict(r)
        at = event.get("event_at")
        event["event_at"] = at.isoformat() if hasattr(at, "isoformat") else at
        out.append(event)
    return out

"""The Shopify orders behind the Pick lists page, kept between loads.

A load shows what was read last time, then asks Shopify only for the orders
changed since. Holds and customer tags can change without touching an order's
updatedAt, so the whole window is read again once FULL_EVERY has passed (or on
Refresh); preview and create always re-read their orders live anyway.
"""
import json
from datetime import datetime, timedelta, timezone

import psycopg2.extras

import db
import order_gate
from shopify_client import list_changed_picklist_orders, list_picklist_orders, picklist_since

# A sync this recent is used as it is (a second tab, a quick reload).
FRESH = timedelta(seconds=15)
FULL_EVERY = timedelta(hours=1)
# Shopify's search index trails its writes; changes are asked for from this
# long before the previous read started.
OVERLAP = timedelta(minutes=2)
LOCK_NAMESPACE = 7301
CACHED_FULFILLMENT = ("UNFULFILLED", "ON_HOLD")


def sync_mode(state, now, full=False):
    """How to bring a store's cached orders up to date: "full", "changes" or
    "none". state is its picklist_store_sync row, None before the first read."""
    if not state:
        return "full"
    if now - (state["full_at"] if full else state["synced_at"]) < FRESH:
        return "none"
    if full or now - state["full_at"] >= FULL_EVERY:
        return "full"
    return "changes"


def worth_caching(node):
    """Whether the order could still go on a pick list — what the whole read's
    Shopify search keeps. Held orders stay, since a release may not touch the
    order's updatedAt."""
    return (not node.get("cancelledAt") and not node.get("closed")
            and node.get("displayFinancialStatus") in order_gate.PAID_STATUSES
            and node.get("displayFulfillmentStatus") in CACHED_FULFILLMENT)


def _state(store_id):
    return db.query("SELECT * FROM picklist_store_sync WHERE store_id = %s", (store_id,), one=True)


def _save(store_id, nodes, now, whole):
    """Store what Shopify returned in one transaction. A whole read replaces
    the store's orders; a read of changes updates the orders it returned and
    drops those that can no longer be picked or have aged out of the window."""
    by_id = {n["id"]: n for n in nodes}
    keep = [n for n in by_id.values() if worth_caching(n)]
    gone = [gid for gid, n in by_id.items() if not worth_caching(n)]
    conn = db.get_db()
    try:
        with conn.cursor() as cur:
            if whole:
                cur.execute("DELETE FROM picklist_order_cache WHERE store_id = %s", (store_id,))
            else:
                cur.execute(
                    "DELETE FROM picklist_order_cache WHERE store_id = %s AND (order_gid = ANY(%s) OR created_at < %s)",
                    (store_id, gone, picklist_since(now)))
            if keep:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO picklist_order_cache (store_id, order_gid, created_at, node) VALUES %s
                       ON CONFLICT (store_id, order_gid) DO UPDATE
                       SET created_at = EXCLUDED.created_at, node = EXCLUDED.node""",
                    [(store_id, n["id"], n["createdAt"], json.dumps(n)) for n in keep],
                )
            cur.execute(
                """INSERT INTO picklist_store_sync (store_id, changed_since, synced_at, full_at)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (store_id) DO UPDATE SET changed_since = EXCLUDED.changed_since,
                       synced_at = EXCLUDED.synced_at,
                       full_at = CASE WHEN %s THEN EXCLUDED.full_at ELSE picklist_store_sync.full_at END""",
                (store_id, now - OVERLAP, now, now, whole),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def sync_store(store_id, full=False):
    """Bring one store's cached orders up to date; returns the mode used.
    One sync per store at a time across workers: a request that waited for
    another one finds the store fresh and is done."""
    conn = db.get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s, %s)", (LOCK_NAMESPACE, store_id))
    conn.commit()  # the lock is the session's; no transaction stays open while Shopify answers
    try:
        now = datetime.now(timezone.utc)
        state = _state(store_id)
        mode = sync_mode(state, now, full)
        if mode == "full":
            _save(store_id, list_picklist_orders(store_id, now), now, whole=True)
        elif mode == "changes":
            _save(store_id, list_changed_picklist_orders(store_id, state["changed_since"], now), now, whole=False)
        return mode
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s, %s)", (LOCK_NAMESPACE, store_id))
        conn.commit()


def cached_orders(store_ids, now=None):
    """[(store_id, raw order node)] cached for the stores, inside the window."""
    rows = db.query(
        "SELECT store_id, node FROM picklist_order_cache WHERE store_id = ANY(%s) AND created_at >= %s",
        (list(store_ids), picklist_since(now or datetime.now(timezone.utc))),
    )
    return [(r["store_id"], r["node"]) for r in rows or []]


def synced_at(store_ids):
    """store_id → when its orders were last read from Shopify (absent: never)."""
    rows = db.query("SELECT store_id, synced_at FROM picklist_store_sync WHERE store_id = ANY(%s)",
                    (list(store_ids),))
    return {r["store_id"]: r["synced_at"] for r in rows or []}

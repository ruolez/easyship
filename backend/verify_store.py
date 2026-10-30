"""Verifications in Postgres. Every change runs in one transaction holding the
verification's row lock, so two packers on one order never lose a count, and
an event's client_id makes a retried request count once."""
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

import db
import verify

QTY_EVENTS = ("scan", "count", "check_all", "confirm", "force_line")
HISTORY_LIMIT = 200


class VerifyError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@contextmanager
def transaction():
    conn = db.get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _lines(cur, verification_id):
    cur.execute(
        """SELECT id, line_key AS key, barcode, title, sku, qty_required, qty_verified, scanned, forced, skip_reason
           FROM verification_lines WHERE verification_id = %s ORDER BY position""",
        (verification_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def _lock(cur, verification_id):
    cur.execute("SELECT * FROM verifications WHERE id = %s FOR UPDATE", (verification_id,))
    row = cur.fetchone()
    if not row:
        raise VerifyError("Verification not found", 404)
    return dict(row)


def _event(cur, verification_id, user_id, kind, **fields):
    cur.execute(
        """INSERT INTO verification_events
               (verification_id, line_id, user_id, kind, result, barcode, units, qty_before, qty_after,
                detail, client_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (verification_id, fields.get("line_id"), user_id, kind, fields.get("result"), fields.get("barcode"),
         fields.get("units", 0), fields.get("qty_before"), fields.get("qty_after"),
         psycopg2.extras.Json(fields["detail"]) if fields.get("detail") is not None else None,
         fields.get("client_id")),
    )
    return cur.fetchone()["id"]


def _replayed(cur, client_id):
    """The event a retried request already recorded, or None."""
    if not client_id:
        return None
    cur.execute("SELECT * FROM verification_events WHERE client_id = %s", (client_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def _insert_lines(cur, verification_id, lines, start=0):
    for i, line in enumerate(lines, start):
        cur.execute(
            """INSERT INTO verification_lines
                   (verification_id, position, line_key, barcode, title, sku, qty_required, qty_verified,
                    scanned, forced, skip_reason)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (verification_id, i, line["key"], line["barcode"], line["title"], line["sku"], line["qty_required"],
             line["qty_verified"], line["scanned"], line["forced"], line["skip_reason"]),
        )


def _sync_lines(cur, verification_id, old_lines, new_lines):
    """Bring stored lines in line with the order as it is now. Returns what changed."""
    merged = verify.merge_lines(old_lines, new_lines)
    old = {line["key"]: line for line in old_lines}
    new_keys = {line["key"] for line in merged}
    changes = []
    for position, line in enumerate(merged):
        prev = old.get(line["key"])
        if prev is None:
            _insert_lines(cur, verification_id, [line], position)
            changes.append({"title": line["title"], "from": 0, "to": line["qty_required"]})
            continue
        if prev["qty_required"] != line["qty_required"]:
            changes.append({"title": line["title"], "from": prev["qty_required"], "to": line["qty_required"]})
        cur.execute(
            """UPDATE verification_lines SET position = %s, title = %s, sku = %s, barcode = %s,
                   qty_required = %s, qty_verified = %s, skip_reason = %s
               WHERE id = %s""",
            (position, line["title"], line["sku"], line["barcode"], line["qty_required"],
             line["qty_verified"], line["skip_reason"], prev["id"]),
        )
    for key, prev in old.items():
        if key not in new_keys:
            changes.append({"title": prev["title"], "from": prev["qty_required"], "to": 0})
            cur.execute("DELETE FROM verification_lines WHERE id = %s", (prev["id"],))
    return changes


def _complete_if_done(cur, verification, user_id):
    lines = _lines(cur, verification["id"])
    if verification["status"] != "in_progress" or not verify.is_complete(lines):
        return False
    cur.execute(
        """UPDATE verifications SET status = 'verified', verified_by = %s, verified_at = now(), updated_at = now(),
               forced = %s,
               shopify_tag_state = CASE WHEN source = 'shopify' THEN 'pending' END, shopify_tag_error = NULL,
               dbadmin_state = CASE WHEN source = 'backoffice' THEN 'pending' END, dbadmin_error = NULL
           WHERE id = %s""",
        (user_id, any(line["forced"] for line in lines), verification["id"]),
    )
    _event(cur, verification["id"], user_id, "complete")
    return True


def open_order(order, lines, user_id):
    """Start verifying an order, or resume the live verification with its lines
    brought up to date. A verified order whose lines now need more goes back to
    in progress. Returns (verification id, {"changes": [...], "reopened": bool})."""
    with transaction() as cur:
        cur.execute("SELECT id FROM verifications WHERE order_key = %s AND status <> 'reset' FOR UPDATE",
                    (order["order_key"],))
        row = cur.fetchone()
        if row is None:
            try:
                cur.execute("SAVEPOINT open_order")
                cur.execute(
                    """INSERT INTO verifications (order_key, source, order_name, shopify_store_id, shopify_order_id,
                           source_db, quotation_id, customer, started_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (order["order_key"], order["source"], order["order_name"], order.get("shopify_store_id"),
                     order.get("shopify_order_id"), order.get("source_db"), order.get("quotation_id"),
                     order.get("customer"), user_id),
                )
            except psycopg2.errors.UniqueViolation:
                cur.execute("ROLLBACK TO SAVEPOINT open_order")
                raise VerifyError("Someone just opened this order — scan it again", 409)
            verification_id = cur.fetchone()["id"]
            _insert_lines(cur, verification_id, lines)
            _event(cur, verification_id, user_id, "start")
            verification = _lock(cur, verification_id)
            _complete_if_done(cur, verification, user_id)
            return verification_id, {"changes": [], "reopened": False}

        verification = _lock(cur, row["id"])
        changes = _sync_lines(cur, verification["id"], _lines(cur, verification["id"]), lines)
        cur.execute(
            """UPDATE verifications SET customer = COALESCE(%s, customer), shopify_order_id = COALESCE(%s, shopify_order_id),
                   quotation_id = COALESCE(%s, quotation_id), source_db = COALESCE(%s, source_db), updated_at = now()
               WHERE id = %s""",
            (order.get("customer"), order.get("shopify_order_id"), order.get("quotation_id"),
             order.get("source_db"), verification["id"]),
        )
        reopened = False
        if changes:
            _event(cur, verification["id"], user_id, "lines_changed", detail={"changes": changes})
            if verification["status"] == "verified" and not verify.is_complete(_lines(cur, verification["id"])):
                cur.execute(
                    """UPDATE verifications SET status = 'in_progress', verified_by = NULL, verified_at = NULL,
                           shopify_tag_state = NULL, dbadmin_state = NULL WHERE id = %s""",
                    (verification["id"],),
                )
                _event(cur, verification["id"], user_id, "reopened")
                reopened = True
        verification = _lock(cur, verification["id"])
        _complete_if_done(cur, verification, user_id)
        return verification["id"], {"changes": changes, "reopened": reopened}


def _require_in_progress(verification):
    if verification["status"] == "verified":
        raise VerifyError("This order is already verified — reset it to verify again", 409)
    if verification["status"] != "in_progress":
        raise VerifyError("This verification was reset — scan the order again", 409)


def _line(lines, line_id):
    for line in lines:
        if line["id"] == line_id:
            return line
    raise VerifyError("Item not found on this order", 404)


def scan(verification_id, code, cases, user_id, client_id=None):
    """Judge and record one scan. Returns the judgement plus completed: bool."""
    with transaction() as cur:
        replay = _replayed(cur, client_id)
        if replay:
            return {"result": replay["result"], "line_id": replay["line_id"], "units": replay["units"],
                    "replayed": True, "completed": False, **(replay["detail"] or {})}
        verification = _lock(cur, verification_id)
        _require_in_progress(verification)
        lines = _lines(cur, verification_id)
        judged = verify.judge_scan(lines, code, cases)
        line = next((l for l in lines if l["key"] == judged["line_key"]), None)
        detail = {"case_units": judged["case_units"], "left": judged["left"]}
        before = line["qty_verified"] if line else None
        after = before + judged["units"] if line else None
        if judged["units"]:
            cur.execute("UPDATE verification_lines SET qty_verified = %s, scanned = TRUE WHERE id = %s",
                        (after, line["id"]))
        _event(cur, verification_id, user_id, "scan", line_id=line["id"] if line else None, result=judged["result"],
               barcode=(code or "").strip()[:200], units=judged["units"], qty_before=before,
               qty_after=after if judged["units"] else None, detail=detail, client_id=client_id)
        completed = _complete_if_done(cur, verification, user_id)
        return {"result": judged["result"], "line_id": line["id"] if line else None, "units": judged["units"],
                "completed": completed, **detail}


def set_line_qty(verification_id, line_id, kind, user_id, qty=None, client_id=None):
    """A typed count, Check all, a tap confirm or a forced line. Returns completed: bool."""
    with transaction() as cur:
        if _replayed(cur, client_id):
            return False
        verification = _lock(cur, verification_id)
        _require_in_progress(verification)
        line = _line(_lines(cur, verification_id), line_id)
        if kind == "count":
            error = verify.count_error(line, qty)
        elif kind == "check_all":
            error = verify.count_error(line, line["qty_required"])
            qty = line["qty_required"]
        elif kind == "confirm":
            error = None if verify.needs_tap(line) else "Scan this item instead"
            qty = line["qty_required"]
        else:
            error = None if verify.is_active(line) else "This item does not need verifying"
            qty = line["qty_required"]
        if error:
            raise VerifyError(error)
        cur.execute("UPDATE verification_lines SET qty_verified = %s, forced = forced OR %s WHERE id = %s",
                    (qty, kind == "force_line", line_id))
        _event(cur, verification_id, user_id, kind, line_id=line_id, units=qty - line["qty_verified"],
               qty_before=line["qty_verified"], qty_after=qty, client_id=client_id)
        return _complete_if_done(cur, verification, user_id)


def force_order(verification_id, user_id):
    with transaction() as cur:
        verification = _lock(cur, verification_id)
        _require_in_progress(verification)
        open_lines = [l for l in _lines(cur, verification_id) if verify.remaining(l) > 0]
        for line in open_lines:
            cur.execute("UPDATE verification_lines SET qty_verified = qty_required, forced = TRUE WHERE id = %s",
                        (line["id"],))
        _event(cur, verification_id, user_id, "force_order",
               detail={"lines": [{"title": l["title"], "from": l["qty_verified"], "to": l["qty_required"]}
                                 for l in open_lines]})
        return _complete_if_done(cur, verification, user_id)


def undo(verification_id, user_id):
    """Take back the last count change (scan, count, Check all, confirm, forced line)."""
    with transaction() as cur:
        verification = _lock(cur, verification_id)
        _require_in_progress(verification)
        cur.execute(
            """SELECT e.*, l.qty_verified FROM verification_events e
               JOIN verification_lines l ON l.id = e.line_id
               WHERE e.verification_id = %s AND e.undone_at IS NULL AND e.kind = ANY(%s) AND e.qty_after IS NOT NULL
               ORDER BY e.id DESC LIMIT 1""",
            (verification_id, list(QTY_EVENTS)),
        )
        last = cur.fetchone()
        if not last:
            raise VerifyError("Nothing to undo")
        if last["qty_verified"] != last["qty_after"]:
            raise VerifyError("This item changed since — undo is not possible", 409)
        cur.execute("UPDATE verification_events SET undone_at = now() WHERE id = %s", (last["id"],))
        cur.execute(
            """UPDATE verification_lines SET qty_verified = %s,
                   scanned = EXISTS (SELECT 1 FROM verification_events WHERE line_id = %s AND kind = 'scan'
                                     AND units > 0 AND undone_at IS NULL),
                   forced = EXISTS (SELECT 1 FROM verification_events WHERE line_id = %s AND kind = 'force_line'
                                    AND undone_at IS NULL)
               WHERE id = %s""",
            (last["qty_before"], last["line_id"], last["line_id"], last["line_id"]),
        )
        _event(cur, verification_id, user_id, "undo", line_id=last["line_id"], units=-last["units"],
               detail={"event_id": last["id"], "kind": last["kind"]})
        return {"kind": last["kind"], "line_id": last["line_id"], "units": last["units"]}


def reset(verification_id, user_id):
    with transaction() as cur:
        verification = _lock(cur, verification_id)
        if verification["status"] == "reset":
            return
        cur.execute("UPDATE verifications SET status = 'reset', updated_at = now() WHERE id = %s", (verification_id,))
        _event(cur, verification_id, user_id, "reset", detail={"was": verification["status"]})


def set_writeback(verification_id, target, state, error=None):
    column = {"shopify": "shopify_tag", "dbadmin": "dbadmin"}[target]
    db.execute(f"UPDATE verifications SET {column}_state = %s, {column}_error = %s WHERE id = %s",
               (state, (error or "")[:1000] or None, verification_id))


def get(verification_id):
    row = db.query(
        """SELECT v.*, su.username AS started_by_name, vu.username AS verified_by_name
           FROM verifications v
           LEFT JOIN users su ON su.id = v.started_by
           LEFT JOIN users vu ON vu.id = v.verified_by
           WHERE v.id = %s""",
        (verification_id,), one=True,
    )
    if not row:
        raise VerifyError("Verification not found", 404)
    with transaction() as cur:
        row["lines"] = _lines(cur, verification_id)
    return row


def live_by_key(order_key):
    return db.query("SELECT * FROM verifications WHERE order_key = %s AND status <> 'reset'", (order_key,), one=True)


def live_lines(verification_id):
    with transaction() as cur:
        return _lines(cur, verification_id)


def events(verification_id):
    return db.query(
        """SELECT e.id, e.kind, e.result, e.barcode, e.units, e.qty_before, e.qty_after, e.detail, e.undone_at,
                  e.created_at, u.username, l.title
           FROM verification_events e
           LEFT JOIN users u ON u.id = e.user_id
           LEFT JOIN verification_lines l ON l.id = e.line_id
           WHERE e.verification_id = %s ORDER BY e.id""",
        (verification_id,),
    ) or []


def history(filters):
    where, params = [], []
    if filters.get("q"):
        where.append("(v.order_name ILIKE %s OR v.customer ILIKE %s)")
        params += [f"%{filters['q']}%"] * 2
    if filters.get("status"):
        where.append("v.status = %s")
        params.append(filters["status"])
    if filters.get("source"):
        where.append("v.source = %s")
        params.append(filters["source"])
    if filters.get("user_id"):
        where.append("(v.verified_by = %s OR v.started_by = %s)")
        params += [filters["user_id"]] * 2
    if filters.get("forced"):
        where.append("v.forced")
    if filters.get("since"):
        where.append("v.started_at >= %s")
        params.append(filters["since"])
    if filters.get("until"):
        where.append("v.started_at < %s")
        params.append(filters["until"])
    sql = f"""
        SELECT v.id, v.order_name, v.source, v.source_db, v.shopify_store_id, s.name AS store_name, v.customer,
               v.status, v.forced, v.started_at, v.verified_at, v.shopify_tag_state, v.dbadmin_state,
               su.username AS started_by_name, vu.username AS verified_by_name,
               COALESCE(SUM(l.qty_required) FILTER (WHERE l.skip_reason IS NULL), 0) AS units_required,
               COALESCE(SUM(LEAST(l.qty_verified, l.qty_required)) FILTER (WHERE l.skip_reason IS NULL), 0)
                   AS units_verified,
               (SELECT COUNT(*) FROM verification_events e WHERE e.verification_id = v.id AND e.kind = 'scan'
                    AND e.result IN ('wrong', 'over', 'case_over')) AS errors
        FROM verifications v
        LEFT JOIN users su ON su.id = v.started_by
        LEFT JOIN users vu ON vu.id = v.verified_by
        LEFT JOIN shopify_stores s ON s.id = v.shopify_store_id
        LEFT JOIN verification_lines l ON l.verification_id = v.id
        {"WHERE " + " AND ".join(where) if where else ""}
        GROUP BY v.id, s.name, su.username, vu.username
        ORDER BY v.started_at DESC
        LIMIT {HISTORY_LIMIT}"""
    return db.query(sql, tuple(params)) or []


def wrong_barcodes(include_fixed=False):
    return db.query(
        f"""SELECT e.id, e.barcode, e.created_at, e.fixed_at, u.username, fu.username AS fixed_by_name,
                   v.id AS verification_id, v.order_name, v.source, v.source_db, s.name AS store_name
            FROM verification_events e
            JOIN verifications v ON v.id = e.verification_id
            LEFT JOIN users u ON u.id = e.user_id
            LEFT JOIN users fu ON fu.id = e.fixed_by
            LEFT JOIN shopify_stores s ON s.id = v.shopify_store_id
            WHERE e.kind = 'scan' AND e.result = 'wrong' {"" if include_fixed else "AND e.fixed_at IS NULL"}
            ORDER BY e.id DESC LIMIT 500""",
    ) or []


def fix_wrong_barcodes(event_ids, user_id):
    db.execute(
        """UPDATE verification_events SET fixed_at = now(), fixed_by = %s
           WHERE id = ANY(%s) AND kind = 'scan' AND result = 'wrong' AND fixed_at IS NULL""",
        (user_id, list(event_ids)),
    )


def list_cases():
    return db.query(
        """SELECT c.id, c.barcode, c.unit_barcode, c.quantity, c.created_at, u.username AS created_by_name
           FROM case_barcodes c LEFT JOIN users u ON u.id = c.created_by ORDER BY c.barcode""",
    ) or []

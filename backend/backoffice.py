import pymssql

import db


class BackofficeError(Exception):
    pass


def get_db_config(db_id):
    row = db.query("SELECT * FROM backoffice_dbs WHERE id = %s", (db_id,), one=True)
    if not row:
        raise BackofficeError("BackOffice database not found — configure it in Settings")
    return row


def _connect(db_id):
    cfg = get_db_config(db_id)
    try:
        return pymssql.connect(
            server=cfg["host"], port=int(cfg["port"] or 1433), database=cfg["db_name"],
            user=cfg["username"], password=cfg["password"], timeout=15, login_timeout=10,
        )
    except Exception as e:
        raise BackofficeError(f"BackOffice connection failed ({cfg['name']}): {e}")


def _to_float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def list_open_invoices(db_id, days=14, q=""):
    select = """
        SELECT TOP 200 InvoiceID, InvoiceNumber, InvoiceDate, ShipDate, BusinessName,
               Shipto, ShipCity, ShipState, ShipZipCode,
               NoBoxes, TotalWeight, InvoiceTotal, TrackingNo
        FROM Invoices_tbl
    """
    if q:
        # Explicit search: look up by invoice number (or business name) across the
        # whole table — no date window, no open-only filter.
        sql = select + """
            WHERE (LTRIM(RTRIM(InvoiceNumber)) = %s
                   OR InvoiceNumber LIKE %s OR BusinessName LIKE %s)
            ORDER BY InvoiceDate DESC
        """
        like = f"%{q}%"
        params = [q.strip(), like, like]
    else:
        sql = select + """
            WHERE (Void IS NULL OR Void = 0)
              AND (TrackingNo IS NULL OR LTRIM(RTRIM(TrackingNo)) = '')
              AND InvoiceDate >= DATEADD(day, -%s, GETDATE())
            ORDER BY InvoiceDate DESC
        """
        params = [int(days)]

    conn = _connect(db_id)
    try:
        with conn.cursor(as_dict=True) as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {
            "invoice_id": r["InvoiceID"],
            "invoice_number": r["InvoiceNumber"],
            "tracking_no": (r["TrackingNo"] or "").strip() or None,
            "ship_date": r["ShipDate"].strftime("%m/%d/%Y") if r["ShipDate"] else
                         (r["InvoiceDate"].strftime("%m/%d/%Y") if r["InvoiceDate"] else ""),
            "business_name": r["BusinessName"],
            "ship_to": ", ".join(filter(None, [
                r["Shipto"], r["ShipCity"], r["ShipState"], r["ShipZipCode"],
            ])),
            "no_boxes": r["NoBoxes"],
            "total_weight": _to_float(r["TotalWeight"]),
            "invoice_total": float(r["InvoiceTotal"]) if r["InvoiceTotal"] is not None else None,
        }
        for r in rows
    ]


def find_invoice_id_by_number(db_id, number):
    """Exact InvoiceNumber match (scan/type-in lookup). Returns InvoiceID or None."""
    conn = _connect(db_id)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT TOP 1 InvoiceID FROM Invoices_tbl
                   WHERE LTRIM(RTRIM(InvoiceNumber)) = %s
                   ORDER BY InvoiceDate DESC""",
                ((number or "").strip(),),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def get_invoice(db_id, invoice_id):
    conn = _connect(db_id)
    try:
        with conn.cursor(as_dict=True) as cur:
            cur.execute(
                """SELECT InvoiceID, InvoiceNumber, BusinessName, Shipto, ShipAddress1,
                          ShipAddress2, ShipContact, ShipCity, ShipState, ShipZipCode,
                          ShipPhoneNo, NoBoxes, TotalWeight, InvoiceTotal, TrackingNo
                   FROM Invoices_tbl WHERE InvoiceID = %s""",
                (invoice_id,),
            )
            inv = cur.fetchone()
            if not inv:
                raise BackofficeError("Invoice not found")
            cur.execute(
                """SELECT ProductSKU, ProductDescription, QtyShipped, QtyOrdered,
                          UnitPrice, ItemWeight
                   FROM InvoicesDetails_tbl
                   WHERE InvoiceID = %s AND (Void IS NULL OR Void = 0)""",
                (invoice_id,),
            )
            lines = cur.fetchall()
    finally:
        conn.close()

    items = []
    for line in lines:
        qty = line["QtyShipped"] if line["QtyShipped"] else line["QtyOrdered"]
        qty = int(qty) if qty else 0
        if qty <= 0:
            continue
        items.append({
            "description": line["ProductDescription"],
            "sku": line["ProductSKU"],
            "quantity": qty,
            "value": float(line["UnitPrice"]) if line["UnitPrice"] is not None else 0,
            "weight": _to_float(line["ItemWeight"]) or 0,
        })

    return {
        "invoice_id": inv["InvoiceID"],
        "invoice_number": inv["InvoiceNumber"],
        "business_name": inv["BusinessName"],
        "tracking_no": inv["TrackingNo"],
        "no_boxes": inv["NoBoxes"],
        "total_weight": _to_float(inv["TotalWeight"]),
        "invoice_total": float(inv["InvoiceTotal"]) if inv["InvoiceTotal"] is not None else None,
        "destination": {
            "company": inv["Shipto"] or inv["BusinessName"],
            "contact": inv["ShipContact"],
            "address1": inv["ShipAddress1"],
            "address2": inv["ShipAddress2"],
            "city": inv["ShipCity"],
            "state": inv["ShipState"],
            "zip": inv["ShipZipCode"],
            "phone": inv["ShipPhoneNo"],
            "country": "US",
        },
        "items": items,
    }


def clear_tracking(db_id, invoice_id, tracking_number, extra_numbers=None):
    """Remove the tracking number we wrote, but only if it still matches —
    never wipe a tracking number someone entered by hand afterwards.
    Numbers we appended to Notes (multi-box extras, or all numbers on a
    re-ship) are removed as well."""
    ours = [n for n in [tracking_number] + list(extra_numbers or []) if n]
    conn = _connect(db_id)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE Invoices_tbl SET TrackingNo = NULL, ShippingCost = NULL
                   WHERE InvoiceID = %s AND LTRIM(RTRIM(TrackingNo)) = %s""",
                (invoice_id, (tracking_number or "").strip()),
            )
            cleared = cur.rowcount > 0
            cur.execute(
                "SELECT Notes FROM Invoices_tbl WHERE InvoiceID = %s", (invoice_id,)
            )
            note_row = cur.fetchone()
            notes = ((note_row or [None])[0] or "")
            removed = False
            if notes:
                parts = [p.strip() for p in notes.split(",")]
                kept = [p for p in parts if p and p not in ours]
                if kept != [p for p in parts if p]:
                    removed = True
                    cur.execute(
                        "UPDATE Invoices_tbl SET Notes = %s WHERE InvoiceID = %s",
                        (",".join(kept) or None, invoice_id),
                    )
            if not cleared and not removed:
                cur.execute(
                    "SELECT TrackingNo FROM Invoices_tbl WHERE InvoiceID = %s", (invoice_id,)
                )
                row = cur.fetchone()
                if row is None:
                    raise BackofficeError(f"Invoice {invoice_id} not found")
                current = (row[0] or "").strip()
                if current and current != (tracking_number or "").strip():
                    raise BackofficeError(
                        f"Invoice {invoice_id} now has a different tracking number "
                        f"({current}) — not cleared"
                    )
        conn.commit()
    finally:
        conn.close()


def write_tracking(db_id, invoice_id, tracking_number, shipping_cost, extra_numbers=None):
    """First box's tracking number goes to TrackingNo; on multi-box shipments the
    remaining numbers are appended to Notes, comma-separated (nvarchar 255).
    If the invoice already carries a different tracking number (re-ship of a
    previously processed order), TrackingNo and ShippingCost stay untouched and
    ALL new numbers go to Notes instead."""
    conn = _connect(db_id)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TrackingNo, Notes FROM Invoices_tbl WHERE InvoiceID = %s",
                (invoice_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise BackofficeError(f"Invoice {invoice_id} not found for tracking update")
            current = (row[0] or "").strip()
            notes = (row[1] or "").strip()
            reship = bool(current) and current != (tracking_number or "").strip()
            if reship:
                to_notes = [tracking_number] + list(extra_numbers or [])
            else:
                if shipping_cost is not None:
                    cur.execute(
                        "UPDATE Invoices_tbl SET TrackingNo = %s, ShippingCost = %s WHERE InvoiceID = %s",
                        (tracking_number, float(shipping_cost), invoice_id),
                    )
                else:
                    cur.execute(
                        "UPDATE Invoices_tbl SET TrackingNo = %s WHERE InvoiceID = %s",
                        (tracking_number, invoice_id),
                    )
                to_notes = list(extra_numbers or [])
            missing = [n for n in to_notes if n and n not in notes]
            if missing:
                extra = ",".join(missing)
                new_notes = f"{notes},{extra}" if notes else extra
                cur.execute(
                    "UPDATE Invoices_tbl SET Notes = %s WHERE InvoiceID = %s",
                    (new_notes[:255], invoice_id),
                )
        conn.commit()
    finally:
        conn.close()

from datetime import timedelta

import pymssql

import db
import profit


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


def _clean_tracking(value):
    """A usable tracking number, or "". BackOffice sometimes stores "0" as a
    placeholder for none — treat it as empty everywhere."""
    v = (value or "").strip()
    return "" if v == "0" else v


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
              AND (TrackingNo IS NULL OR LTRIM(RTRIM(TrackingNo)) IN ('', '0'))
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
            "tracking_no": _clean_tracking(r["TrackingNo"]) or None,
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


def _first_positive(*values):
    for v in values:
        if v is not None and float(v) > 0:
            return float(v)
    return None


def economics_from_invoice(inv, lines):
    """Whole-invoice economics. Shipping is billed as a line item, so revenue
    is the subtotal less discounts; taxes and the label cost this app writes
    into ShippingCost are never revenue. A line's cost is the first positive
    of its own UnitCost, then the item's average, last and unit cost."""
    econ_lines = []
    for line in lines:
        qty = line["QtyShipped"] if line["QtyShipped"] else line["QtyOrdered"]
        econ_lines.append({
            "description": line["ProductDescription"],
            "sku": line["ProductSKU"],
            "quantity": int(qty) if qty else 0,
            "unit_price": float(line["UnitPrice"]) if line["UnitPrice"] is not None else 0,
            "unit_cost": _first_positive(line["UnitCost"], line["AvrCost"], line["LastCost"],
                                         line["ItemUnitCost"]),
        })
    subtotal = inv["InvoiceSubtotal"]
    if subtotal is None:
        subtotal = sum(l["quantity"] * l["unit_price"] for l in econ_lines)
    revenue = float(subtotal) - float(inv["TotalDiscounts"] or 0)
    return profit.compute_economics(econ_lines, revenue, 0, "USD")


def get_invoice(db_id, invoice_id):
    no_company = bool(get_db_config(db_id).get("no_company"))
    conn = _connect(db_id)
    try:
        with conn.cursor(as_dict=True) as cur:
            cur.execute(
                """SELECT InvoiceID, InvoiceNumber, BusinessName, Shipto, ShipAddress1,
                          ShipAddress2, ShipContact, ShipCity, ShipState, ShipZipCode,
                          ShipPhoneNo, NoBoxes, TotalWeight, InvoiceTotal, TrackingNo,
                          InvoiceSubtotal, TotalDiscounts
                   FROM Invoices_tbl WHERE InvoiceID = %s""",
                (invoice_id,),
            )
            inv = cur.fetchone()
            if not inv:
                raise BackofficeError("Invoice not found")
            cur.execute(
                """SELECT d.ProductSKU, d.ProductDescription, d.QtyShipped, d.QtyOrdered,
                          d.UnitPrice, d.ItemWeight, d.UnitCost,
                          i.AvrCost, i.LastCost, i.UnitCost AS ItemUnitCost
                   FROM InvoicesDetails_tbl d
                   LEFT JOIN Items_tbl i ON i.ProductID = d.ProductID
                   WHERE d.InvoiceID = %s AND (d.Void IS NULL OR d.Void = 0)""",
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
        "tracking_no": _clean_tracking(inv["TrackingNo"]) or None,
        "no_boxes": inv["NoBoxes"],
        "total_weight": _to_float(inv["TotalWeight"]),
        "invoice_total": float(inv["InvoiceTotal"]) if inv["InvoiceTotal"] is not None else None,
        "destination": {
            "company": None if no_company else (inv["Shipto"] or inv["BusinessName"]),
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
        "economics": economics_from_invoice(inv, lines),
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
                current = _clean_tracking(row[0])
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
            current = _clean_tracking(row[0])
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


# ---------- Pick lists ----------
# A pick list is written as a quotation; column mapping follows the
# picklist-quotation converter (Quotations_tbl / QuotationsDetails_tbl).

# Matches the quotations BackOffice itself writes.
QUOTATION_VALID_DAYS = 10
UPC_CHUNK = 500  # SQL Server allows ~2100 parameters per statement


def _trunc(value, length):
    if value is None:
        return None
    text = str(value)
    return text[:length]


def list_sales_reps(db_id):
    conn = _connect(db_id)
    try:
        with conn.cursor(as_dict=True) as cur:
            cur.execute(
                """SELECT EmployeeID, FirstName, LastName FROM Employees_tbl
                   WHERE LTRIM(RTRIM(ISNULL(FirstName, ''))) <> ''
                      OR LTRIM(RTRIM(ISNULL(LastName, ''))) <> ''
                   ORDER BY FirstName, LastName"""
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {"id": r["EmployeeID"],
         "name": " ".join(p.strip() for p in (r["FirstName"], r["LastName"]) if p and p.strip())}
        for r in rows
    ]


def search_customers(db_id, q=""):
    q = (q or "").strip()
    sql = """SELECT TOP 50 CustomerID, BusinessName, AccountNo, ShipCity, ShipState
             FROM Customers_tbl WHERE (Discontinued IS NULL OR Discontinued = 0)"""
    params = ()
    if q:
        like = f"%{q}%"
        sql += " AND (BusinessName LIKE %s OR AccountNo LIKE %s OR CAST(CustomerID AS nvarchar(12)) = %s)"
        params = (like, like, q)
    sql += " ORDER BY BusinessName"
    conn = _connect(db_id)
    try:
        with conn.cursor(as_dict=True) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {"id": r["CustomerID"], "name": (r["BusinessName"] or "").strip(), "account_no": r["AccountNo"],
         "city": r["ShipCity"], "state": r["ShipState"]}
        for r in rows
    ]


def find_items_by_upc(db_id, upcs):
    """barcode → Items_tbl row (+ UnitDesc) for every barcode BackOffice knows.
    When several products share a barcode, an active one wins, then the oldest."""
    upcs = sorted({u for u in upcs if u})
    found = {}
    if not upcs:
        return found
    conn = _connect(db_id)
    try:
        with conn.cursor(as_dict=True) as cur:
            for i in range(0, len(upcs), UPC_CHUNK):
                chunk = upcs[i:i + UPC_CHUNK]
                cur.execute(
                    f"""SELECT i.*, u.UnitDesc FROM Items_tbl i
                        LEFT JOIN Units_tbl u ON i.UnitID = u.UnitID
                        WHERE i.ProductUPC IN ({",".join(["%s"] * len(chunk))})
                        ORDER BY ISNULL(i.Discontinued, 0), i.ProductID""",
                    tuple(chunk),
                )
                for row in cur.fetchall():
                    found.setdefault((row["ProductUPC"] or "").strip(), row)
    finally:
        conn.close()
    return found


def quotation_detail_row(quotation_id, item, qty):
    """The QuotationsDetails_tbl values for one pick-list line, in
    QUOTATION_DETAIL_COLUMNS order — defaults as BackOffice writes them:
    the item's own ExpDate and tax id, no ReasonID or ItemWeight."""
    unit_price = float(item.get("UnitPrice") or 0)
    unit_cost = float(item.get("UnitCost") or 0)
    extended_price = round(qty * unit_price, 2)
    return (
        quotation_id, item.get("CateID"), item.get("SubCateID"), _trunc(item.get("UnitDesc") or "", 50), 1,
        item.get("ProductID"), _trunc(item.get("ProductSKU"), 20), _trunc(item.get("ProductUPC"), 20),
        _trunc(item.get("ProductDescription"), 50), _trunc(item.get("ItemSize") or "", 10),
        item.get("ExpDate"), None, "",
        unit_price, unit_price, 0, unit_cost,
        0, 0, qty, None,
        extended_price, 0, round(qty * unit_cost, 2),
        0, 0, extended_price,
        0, "",
        0, item.get("ItemTaxID") or 0, 0, 0,
    )


QUOTATION_DETAIL_COLUMNS = (
    "QuotationID, CateID, SubCateID, UnitDesc, UnitQty, "
    "ProductID, ProductSKU, ProductUPC, ProductDescription, ItemSize, "
    "ExpDate, ReasonID, LineMessage, "
    "UnitPrice, OriginalPrice, RememberPrice, UnitCost, "
    "Discount, ds_Percent, Qty, ItemWeight, "
    "ExtendedPrice, ExtendedDisc, ExtendedCost, "
    "PromotionID, PromotionLine, ActExtendedPrice, "
    "SPPromoted, SPPromotionDescription, "
    "Taxable, ItemTaxID, Catch, Flag"
)


def create_quotation(db_id, *, now, number_prefix, customer_id, sales_rep_id, status, title, notes, memo, lines):
    """Write the pick list as one quotation — header, a line per barcode and
    the total — in a single transaction; the number is taken under a lock so
    two pick lists never share one. `lines` are (Items_tbl row, qty) pairs.
    Returns (QuotationID, QuotationNumber, the customer's BusinessName)."""
    conn = _connect(db_id)
    try:
        with conn.cursor(as_dict=True) as cur:
            cur.execute(
                """SELECT BusinessName, AccountNo, ShipTo, ShipAddress1, ShipAddress2, ShipContact,
                          ShipCity, ShipState, ShipZipCode, ShipPhone_Number
                   FROM Customers_tbl WHERE CustomerID = %s""",
                (customer_id,),
            )
            customer = cur.fetchone()
            if not customer:
                raise BackofficeError(f"BackOffice customer {customer_id} not found")
            start = len(number_prefix) + 1
            cur.execute(
                """SELECT ISNULL(MAX(CASE WHEN LEN(QuotationNumber) > %s
                                           AND SUBSTRING(QuotationNumber, %s, 20) NOT LIKE %s
                                      THEN CAST(SUBSTRING(QuotationNumber, %s, 20) AS BIGINT) END), 0) + 1
                          AS next_counter
                   FROM Quotations_tbl WITH (UPDLOCK, HOLDLOCK)
                   WHERE QuotationNumber LIKE %s""",
                (len(number_prefix), start, "%[^0-9]%", start, number_prefix + "%"),
            )
            number = f"{number_prefix}{cur.fetchone()['next_counter']}"
            expires = now + timedelta(days=QUOTATION_VALID_DAYS)
            cur.execute(
                """INSERT INTO Quotations_tbl
                   (QuotationNumber, QuotationDate, QuotationTitle, CustomerID, Status,
                    PoNumber, AutoOrderNo, ExpirationDate,
                    BusinessName, AccountNo, Shipto, ShipAddress1, ShipAddress2,
                    ShipContact, ShipCity, ShipState, ShipZipCode, ShipPhoneNo,
                    ShipperID, SalesRepID, TermID,
                    Header, Footer, Notes, Memo, flaged)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                   SELECT CAST(SCOPE_IDENTITY() AS INT) AS QuotationID""",
                (
                    _trunc(number, 20), now, _trunc(title, 50), customer_id, status,
                    "", None, expires,
                    _trunc(customer["BusinessName"] or "", 50), _trunc(customer["AccountNo"] or "", 13),
                    _trunc(customer["ShipTo"] or "", 50), _trunc(customer["ShipAddress1"] or "", 50),
                    _trunc(customer["ShipAddress2"] or "", 50), _trunc(customer["ShipContact"] or "", 50),
                    _trunc(customer["ShipCity"] or "", 20), _trunc(customer["ShipState"] or "", 3),
                    _trunc(customer["ShipZipCode"] or "", 10), _trunc(customer["ShipPhone_Number"] or "", 13),
                    0, sales_rep_id, 1,
                    "", "", _trunc(notes, 255), memo, 0,
                ),
            )
            quotation_id = cur.fetchone()["QuotationID"]
            placeholders = ", ".join(["%s"] * 33)
            for item, qty in lines:
                cur.execute(
                    f"INSERT INTO QuotationsDetails_tbl ({QUOTATION_DETAIL_COLUMNS}) VALUES ({placeholders})",
                    quotation_detail_row(quotation_id, item, qty),
                )
            cur.execute(
                """UPDATE Quotations_tbl SET QuotationTotal =
                       (SELECT ISNULL(SUM(ExtendedPrice), 0) FROM QuotationsDetails_tbl WHERE QuotationID = %s)
                   WHERE QuotationID = %s""",
                (quotation_id, quotation_id),
            )
        conn.commit()
        return quotation_id, number, (customer["BusinessName"] or "").strip()
    except Exception as e:
        conn.rollback()
        if isinstance(e, BackofficeError):
            raise
        raise BackofficeError(f"Could not create the quotation: {e}")
    finally:
        conn.close()


def delete_quotation(db_id, quotation_id, quotation_number):
    """Remove a pick list's quotation and its lines. Only the row whose id
    AND number both match is touched; False when it is already gone."""
    conn = _connect(db_id)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT QuotationID FROM Quotations_tbl WHERE QuotationID = %s AND QuotationNumber = %s",
                (quotation_id, quotation_number),
            )
            if not cur.fetchone():
                conn.commit()
                return False
            cur.execute("DELETE FROM QuotationsDetails_tbl WHERE QuotationID = %s", (quotation_id,))
            cur.execute("DELETE FROM Quotations_tbl WHERE QuotationID = %s", (quotation_id,))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        raise BackofficeError(f"Could not delete quotation {quotation_number}: {e}")
    finally:
        conn.close()

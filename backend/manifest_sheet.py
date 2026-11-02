"""Pickup summary sheet: a manifest this app writes itself, for carriers with
no manifest API (Spee-Dee). The driver checks the package count against what
is tendered and signs it.

A minimal text-only PDF (Letter, built-in Helvetica, WinAnsi text) written by
hand, so no PDF library is needed to create one.
"""
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")

PAGE_W, PAGE_H = 612, 792
MARGIN = 40
ROW_H = 15
ROWS_FIRST_PAGE = 36
ROWS_PER_PAGE = 44
FOOTER_H = 110  # totals + signature lines, kept on the last page

# (header, x, max characters)
COLUMNS = (
    ("#", MARGIN, 4),
    ("Order", MARGIN + 24, 12),
    ("Box", MARGIN + 92, 7),
    ("Tracking barcode", MARGIN + 130, 26),
    ("Recipient", MARGIN + 282, 20),
    ("City, State", MARGIN + 378, 16),
    ("Weight lb", MARGIN + 450, 9),
    ("COD", MARGIN + 497, 9),
)


def central_date(dt):
    return dt.astimezone(CENTRAL).strftime("%m/%d/%Y")


def sheet_row(row, order_id):
    """One table row from a shipments row and its carrier order id."""
    dest = row.get("destination") or {}
    city = ", ".join(x for x in ((dest.get("city") or "").strip(), (dest.get("state") or "").strip()) if x)
    weight = row.get("total_weight_lb")
    # COD is per order: the driver collects it once, at the first box.
    cod = ((row.get("options") or {}).get("cod") or {}).get("amount")
    return {
        "order": order_id or "",
        "box": f"{row.get('box_number') or 1}/{row.get('box_total') or 1}",
        "tracking": row.get("tracking_number") or "",
        "recipient": dest.get("company") or dest.get("contact") or "",
        "city": city,
        "weight": float(weight) if weight is not None else None,
        "cod": float(cod) if cod and (row.get("box_number") or 1) == 1 else None,
    }


def _escape(text):
    raw = str(text).encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _clip(text, width):
    text = str(text or "")
    return text if len(text) <= width else text[: max(width - 1, 1)] + "…"


class _Page:
    def __init__(self):
        self.ops = []

    def text(self, x, y, value, size=9, bold=False):
        font = b"F2" if bold else b"F1"
        self.ops.append(b"BT /" + font + b" %d Tf %.1f %.1f Td (" % (size, x, y)
                        + _escape(value) + b") Tj ET")

    def line(self, x1, y1, x2, y2, width=0.5):
        self.ops.append(b"%.2f w %.1f %.1f m %.1f %.1f l S" % (width, x1, y1, x2, y2))

    def stream(self):
        return b"\n".join(self.ops)


def _origin_lines(origin):
    o = {k.removeprefix("origin_"): (v or "").strip() for k, v in (origin or {}).items()}
    street = ", ".join(x for x in (o.get("address1"), o.get("address2")) if x)
    city = " ".join(x for x in (f"{o.get('city')}," if o.get("city") else "", o.get("state"), o.get("zip")) if x)
    return [x for x in (o.get("company") or o.get("contact"), street, city, o.get("phone")) if x]


def _layout(rows):
    """Rows split into pages; the footer needs room on the last page."""
    pages, i = [], 0
    while True:
        capacity = ROWS_FIRST_PAGE if not pages else ROWS_PER_PAGE
        chunk = rows[i:i + capacity]
        i += len(chunk)
        pages.append(chunk)
        if i >= len(rows):
            footer_rows = FOOTER_H // ROW_H
            if len(chunk) > capacity - footer_rows:
                pages.append([])  # footer alone on a fresh page
            return pages


def _table_header(page, y):
    for title, x, _ in COLUMNS:
        page.text(x, y, title, bold=True)
    page.line(MARGIN, y - 4, PAGE_W - MARGIN, y - 4, 0.8)
    return y - ROW_H - 2


def summary_sheet_pdf(title, ship_date, origin, rows):
    """The sheet as PDF bytes. `rows` come from sheet_row()."""
    total_weight = round(sum(r["weight"] or 0 for r in rows), 2)
    orders = len({r["order"] for r in rows if r["order"]})
    cod_amounts = [r["cod"] for r in rows if r.get("cod")]
    chunks = _layout(rows)
    pages = []
    number = 0
    for p_index, chunk in enumerate(chunks):
        page = _Page()
        y = PAGE_H - MARGIN - 10
        page.text(MARGIN, y, title, size=16, bold=True)
        page.text(PAGE_W - MARGIN - 120, y, f"Ship date {ship_date}", size=10, bold=True)
        y -= 22
        if p_index == 0:
            for line in _origin_lines(origin):
                page.text(MARGIN, y, line, size=10)
                y -= 13
            y -= 6
            page.text(MARGIN, y, f"{len(rows)} package(s) in {orders} order(s)", size=11, bold=True)
            y -= 22
        if chunk or p_index == 0:
            y = _table_header(page, y)
        for r in chunk:
            number += 1
            values = (number, r["order"], r["box"], r["tracking"], r["recipient"], r["city"],
                      "" if r["weight"] is None else f"{r['weight']:g}",
                      f"{r['cod']:.2f}" if r.get("cod") else "")
            for (_, x, width), value in zip(COLUMNS, values):
                page.text(x, y, _clip(value, width))
            y -= ROW_H
        if p_index == len(chunks) - 1:
            y -= 8
            page.line(MARGIN, y + 6, PAGE_W - MARGIN, y + 6, 0.8)
            page.text(MARGIN, y - 8, f"Total packages: {len(rows)}", size=11, bold=True)
            page.text(MARGIN + 160, y - 8, f"Total weight: {total_weight:g} lb", size=11, bold=True)
            if cod_amounts:
                page.text(MARGIN + 310, y - 8, f"COD to collect: ${sum(cod_amounts):,.2f} "
                          f"({len(cod_amounts)} order(s))", size=11, bold=True)
            y -= 52
            page.line(MARGIN, y, MARGIN + 260, y)
            page.line(MARGIN + 320, y, PAGE_W - MARGIN, y)
            page.text(MARGIN, y - 12, "Driver signature", size=8)
            page.text(MARGIN + 320, y - 12, "Date / time picked up", size=8)
        page.text(PAGE_W - MARGIN - 60, MARGIN - 16, f"Page {p_index + 1} of {len(chunks)}", size=8)
        pages.append(page.stream())
    return _assemble(pages)


def _assemble(streams):
    """Objects: 1 catalog, 2 pages, 3-4 fonts, then (page, content) pairs."""
    objects = [None, None,
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
    kids = []
    for stream in streams:
        page_num = len(objects) + 1
        kids.append(page_num)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>"
            % (PAGE_W, PAGE_H, page_num + 1))
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
                  + b"] /Count %d >>" % len(kids))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)

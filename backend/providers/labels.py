"""Provider-agnostic label document helpers.

These operate purely on `(bytes, format)` tuples, so any provider's labels —
PDF, PNG, or ZPL — merge and print through the same code.
"""
import io


def sniff_label_format(data, declared="pdf"):
    """The declared document format can be 'url' or wrong — trust the bytes."""
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"\x89PNG"):
        return "png"
    head = data[:256].lstrip()
    # ZPL streams open with ^XA, but some couriers prefix a graphic download (~DG).
    if head[:3] == b"^XA" or (head[:3] == b"~DG" and b"^XA" in data[:65536]):
        return "zpl"
    return declared if declared in ("pdf", "png", "zpl") else "pdf"


def count_label_pages(docs):
    """Printable pages across label documents — a 3-box shipment may arrive as
    one 3-page PDF or three 1-page documents."""
    total = 0
    for data, fmt in docs:
        if fmt == "pdf":
            try:
                from pypdf import PdfReader
                total += len(PdfReader(io.BytesIO(data)).pages)
            except Exception:
                total += 1
        elif fmt == "zpl":
            total += max(data.count(b"^XA"), 1)
        else:
            total += 1
    return total


def _image_to_pdf(data):
    """Wrap a raster label (PNG/JPG) in a single-page PDF at its native size,
    honoring the image's embedded DPI so a 4x6 label stays 4x6."""
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    dpi = img.info.get("dpi")
    resolution = float(dpi[0]) if dpi and dpi[0] else 203.0  # thermal labels are 203 DPI
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=resolution)
    return buf.getvalue()


def _merge_to_pdf(docs):
    """One multi-page PDF, one label per page — PDF pages copied as-is, raster
    labels converted first. Handles all-PDF, all-image, and mixed sets."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for data, fmt in docs:
        page_pdf = data if fmt == "pdf" else _image_to_pdf(data)
        for page in PdfReader(io.BytesIO(page_pdf)).pages:
            writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def merge_label_documents(docs):
    """Combine per-box labels into one printable file. PDF/PNG labels merge into
    a single multi-page PDF (one label per page); ZPL concatenates. Returns
    (bytes, format) or (None, None)."""
    if not docs:
        return None, None
    if len(docs) == 1:
        return docs[0]
    formats = {fmt for _, fmt in docs}
    if formats == {"zpl"}:
        return b"\n".join(data for data, _ in docs), "zpl"
    if formats <= {"pdf", "png"}:
        try:
            return _merge_to_pdf(docs), "pdf"
        except Exception:
            return docs[0]  # never drop the whole job if conversion fails
    return docs[0]


# ---------------------------------------------------------------- ZPL conversion

LABEL_WIDTH_IN = 4
LABEL_HEIGHT_IN = 6
_INK_THRESHOLD = 160  # gray level below which a pixel prints black


def _render_pdf_pages(data, dpi):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        return [page.render(scale=dpi / 72.0).to_pil() for page in pdf]
    finally:
        pdf.close()


def _fit_to_label(img, width_px, height_px):
    """Scale a rendered label page onto the printer's label area, keeping
    aspect ratio. Pages that are not label-shaped (a 4x6 label printed on a
    letter sheet) get their white margins trimmed first so the label itself,
    not the empty page, fills the media."""
    from PIL import Image, ImageOps

    gray = img.convert("L")
    label_aspect = width_px / height_px
    if abs(gray.width / gray.height - label_aspect) > 0.1:
        bbox = ImageOps.invert(gray).point(lambda p: 255 if p > 255 - _INK_THRESHOLD else 0).getbbox()
        if bbox:
            gray = gray.crop(bbox)
    scale = min(width_px / gray.width, height_px / gray.height)
    new_size = (max(1, round(gray.width * scale)), max(1, round(gray.height * scale)))
    if new_size != gray.size:
        gray = gray.resize(new_size, Image.LANCZOS)
    canvas = Image.new("L", (width_px, height_px), 255)
    canvas.paste(gray, ((width_px - gray.width) // 2, (height_px - gray.height) // 2))
    return canvas


def _image_to_zpl(img, width_px, height_px):
    page = _fit_to_label(img, width_px, height_px)
    # ^GF bits are 1 = black; PIL's "1" mode is 1 = white, so threshold inverted.
    bitmap = page.point(lambda p: 255 if p < _INK_THRESHOLD else 0).convert("1")
    bytes_per_row = (width_px + 7) // 8
    raw = bitmap.tobytes()
    total = bytes_per_row * height_px
    return (
        f"^XA^PW{width_px}^LL{height_px}^LH0,0^FO0,0"
        f"^GFA,{total},{total},{bytes_per_row},{raw.hex().upper()}^FS^XZ"
    )


def to_zpl(data, fmt, dpi=203):
    """Render any label document as ZPL for a Zebra printer: ZPL passes
    through, PDF pages and PNG images are rasterized at the printer's DPI and
    embedded as ^GFA bitmaps, one ^XA…^XZ label per page."""
    fmt = sniff_label_format(data, fmt)
    if fmt == "zpl":
        return data
    from PIL import Image

    if fmt == "pdf":
        pages = _render_pdf_pages(data, dpi)
    else:
        pages = [Image.open(io.BytesIO(data))]
    width_px, height_px = LABEL_WIDTH_IN * dpi, LABEL_HEIGHT_IN * dpi
    return "\n".join(_image_to_zpl(p, width_px, height_px) for p in pages).encode("ascii")

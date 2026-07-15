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
    if data[:3] == b"^XA" or data[:16].lstrip()[:3] == b"^XA":
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

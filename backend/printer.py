import socket

import db


class PrinterError(Exception):
    pass


def network_print(data, host=None, port=None):
    """Send raw bytes (ZPL or printer-supported format) to a network label
    printer on its raw port (default 9100)."""
    host = host or db.get_setting("printer_host")
    port = int(port or db.get_setting("printer_port") or 9100)
    if not host:
        raise PrinterError("No printer host configured — set it in Settings → Printing")
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.sendall(data)
    except OSError as e:
        raise PrinterError(f"Could not print to {host}:{port} — {e}")


def printer_dpi():
    try:
        return int(db.get_setting("printer_dpi") or 203)
    except ValueError:
        return 203


def print_label(data, fmt):
    """Network-print a label document of any format: it is converted to ZPL
    first, so PDF/PNG labels print on a Zebra just like native ZPL ones."""
    from providers import labels

    try:
        zpl = labels.to_zpl(data, fmt, dpi=printer_dpi())
    except Exception as e:
        raise PrinterError(f"Could not convert {fmt.upper()} label to ZPL — {e}")
    network_print(zpl)


def print_shipment_label(shipment_row):
    if not shipment_row["label_path"]:
        raise PrinterError("No label stored for this shipment")
    with open(shipment_row["label_path"], "rb") as f:
        data = f.read()
    print_label(data, shipment_row["label_format"] or "pdf")


TEST_ZPL = b"^XA^CF0,40^FO50,60^FDEasyShip printer test^FS^FO50,120^FDIf you can read this,^FS^FO50,170^FDnetwork printing works.^FS^XZ"

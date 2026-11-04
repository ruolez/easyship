import socket

import db

PRINT_MODES = ("browser", "network", "browserprint")
PRINTER_DPIS = (203, 300)
DEFAULT_PORT = 9100
DEFAULT_DPI = 203


class PrinterError(Exception):
    pass


def _port(value):
    """A TCP port as an int, DEFAULT_PORT when blank, None when it is not one
    (network_print refuses it with a message)."""
    text = str(value if value is not None else "").strip()
    if not text:
        return DEFAULT_PORT
    try:
        port = int(text)
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def _dpi(value):
    try:
        dpi = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_DPI
    return dpi if dpi in PRINTER_DPIS else DEFAULT_DPI


def station_config(raw):
    """The printing setup of the station that sent the request ({mode, host,
    port, dpi}, kept in its browser). A field the request leaves out — a tab
    opened before printing moved to each station — falls back to the shared
    setup that used to live on the server."""
    raw = raw if isinstance(raw, dict) else {}

    def value(field, setting):
        return raw[field] if field in raw else db.get_setting(setting)

    mode = value("mode", "print_mode") or "browser"
    return {
        "mode": mode if mode in PRINT_MODES else "browser",
        "host": str(value("host", "printer_host") or "").strip(),
        "port": _port(value("port", "printer_port")),
        "dpi": _dpi(value("dpi", "printer_dpi")),
    }


def network_print(data, host, port):
    """Send raw bytes (ZPL or printer-supported format) to a network label
    printer on its raw port."""
    if not host:
        raise PrinterError("No printer IP set for this computer — set it in Settings → Printing")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise PrinterError("Printer port must be a number from 1 to 65535 — check Settings → Printing")
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.sendall(data)
    except OSError as e:
        raise PrinterError(f"Could not print to {host}:{port} — {e}")


def print_label(data, fmt, cfg):
    """Network-print a label document of any format on the station's printer:
    it is converted to ZPL first, so PDF/PNG labels print on a Zebra just like
    native ZPL ones."""
    from providers import labels

    try:
        zpl = labels.to_zpl(data, fmt, dpi=cfg["dpi"])
    except Exception as e:
        raise PrinterError(f"Could not convert {fmt.upper()} label to ZPL — {e}")
    network_print(zpl, cfg["host"], cfg["port"])


def print_shipment_label(shipment_row, cfg):
    if not shipment_row["label_path"]:
        raise PrinterError("No label stored for this shipment")
    with open(shipment_row["label_path"], "rb") as f:
        data = f.read()
    print_label(data, shipment_row["label_format"] or "pdf", cfg)


TEST_ZPL = b"^XA^CF0,40^FO50,60^FDEasyShip printer test^FS^FO50,120^FDIf you can read this,^FS^FO50,170^FDnetwork printing works.^FS^XZ"

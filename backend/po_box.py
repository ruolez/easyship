"""PO Box destinations: only USPS — and the services that hand the last mile to
USPS — can deliver, so the rate list is narrowed before the packer picks."""
import re

_PO_BOX = re.compile(r"\bp\.?\s*o\.?\s*box\b|\bpost\s*office\s*box\b|\bpob\s*#?\s*\d", re.I)

# Matched against the lower-cased "courier_name umbrella_name" of a rate, so
# the same list covers every provider's naming (Easyship "USPS - Priority
# Mail", ShipStation "USPS Priority Mail" under "Stamps.com", an Endicia
# instance alias, Shippo/EasyPost "USPS").
PO_BOX_SERVICE_KEYWORDS = (
    "usps", "stamps", "endicia", "postal",
    "surepost", "ground saver", "mail innovations",   # UPS, USPS final mile
    "ground economy", "smartpost",                    # FedEx, USPS final mile
    "ecommerce",                                      # DHL eCommerce
)


def is_po_box(destination):
    lines = (destination.get("address1") or "", destination.get("address2") or "")
    return any(_PO_BOX.search(line) for line in lines)


def delivers_to_po_box(courier_name, umbrella_name):
    text = f"{courier_name or ''} {umbrella_name or ''}".lower()
    return any(k in text for k in PO_BOX_SERVICE_KEYWORDS)


def split_rates(rates):
    """(rates that deliver to a PO Box, sorted distinct carriers of the rest)."""
    kept, hidden = [], set()
    for r in rates:
        if delivers_to_po_box(r.get("courier_name"), r.get("umbrella_name")):
            kept.append(r)
        else:
            hidden.add(r.get("umbrella_name") or r.get("courier_name") or "")
    return kept, sorted(hidden)

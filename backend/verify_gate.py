"""Verification gate: a label is bought only for an order verified in
EasyShip or checked in the old Shipper app, unless a user with the Verify
override permission buys anyway.

An EasyShip verification counts only while the order still matches it: the
order's current lines (Shopify live, the BackOffice invoice) are compared
with what was verified, and anything needing more units makes the order
unverified again. When the current lines cannot be read, the verification
stands.
"""
import backoffice
import db
import shipper
import shopify_client
import verify
import verify_store


def _prefixes(source):
    return verify.parse_prefixes(db.get_setting(verify.SKIP_PREFIX_KEYS[source]))


def _shopify_current(shipment):
    gid = shipment.get("shopify_order_id")
    if not gid:
        resolved = shopify_client.resolve_order(shipment["shopify_store_id"], shipment["shopify_order_name"])
        gid = resolved and resolved["id"]
    nodes = shopify_client.get_picklist_orders(shipment["shopify_store_id"], [gid]) if gid else []
    return verify.build_lines(verify.shopify_items(nodes[0]), _prefixes("shopify")) if nodes else None


def _backoffice_current(shipment):
    rows = backoffice.invoice_verify_lines(shipment["backoffice_db_id"], shipment["backoffice_invoice_id"])
    return verify.build_lines(verify.invoice_items(rows), _prefixes("backoffice"))


def _shipper_status(source, number):
    try:
        res = shipper.check_shopify(number) if source == "shopify" else shipper.check_quotation(number)
    except (shipper.ShipperError, backoffice.BackofficeError):
        return "unavailable"
    return res["status"]


def _passed(where, verification=None):
    return {"verified": True, "where": where, "verification_id": verification and verification["id"],
            "unverified": [], "message": ""}


def _blocked(message, unverified=(), verification=None):
    return {"verified": False, "where": None, "verification_id": verification and verification["id"],
            "unverified": list(unverified), "message": message}


def check(shipment):
    """Is this shipment's order verified? shipment carries the shipments-row
    fields source, shopify_store_id/_order_id/_order_name or
    backoffice_db_id/_invoice_id/_invoice_number."""
    source = shipment.get("source")
    if source == "shopify":
        if not shipment.get("shopify_store_id") or not shipment.get("shopify_order_name"):
            return _passed(None)
        number = shipment["shopify_order_name"]
        key = verify.shopify_order_key(shipment["shopify_store_id"], number)
        read_current = _shopify_current
        errors = (shopify_client.ShopifyError,)
    elif source == "backoffice":
        try:
            number = backoffice.quotation_number_for_invoice(shipment["backoffice_db_id"],
                                                             shipment["backoffice_invoice_number"])
        except backoffice.BackofficeError as e:
            return _blocked(f"Could not check verification — {e}")
        if not number:
            return _blocked(f"No quotation found for invoice {shipment['backoffice_invoice_number']} — "
                            "the order cannot have been verified")
        key = verify.backoffice_order_key(number)
        read_current = _backoffice_current
        errors = (backoffice.BackofficeError,)
    else:
        return _passed(None)

    verification = verify_store.live_by_key(key)
    if verification and verification["status"] == "verified":
        try:
            current = read_current(shipment)
        except errors:
            current = None
        if current is None:
            return _passed("easyship", verification)
        missing = verify.unverified_items(verify_store.live_lines(verification["id"]), current)
        if not missing:
            return _passed("easyship", verification)
        return _blocked("The order changed after it was verified — it has unverified items", missing, verification)

    if _shipper_status(source, number) == "verified":
        return _passed("shipper", verification)
    if verification:
        missing = [{"title": l["title"], "barcode": l["barcode"], "qty_required": l["qty_required"],
                    "qty_verified": l["qty_verified"]}
                   for l in verify_store.live_lines(verification["id"]) if verify.remaining(l) > 0]
        return _blocked("Verification of this order is not finished", missing, verification)
    return _blocked("This order has not been verified")

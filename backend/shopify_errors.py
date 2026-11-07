class ShopifyError(Exception):
    pass


class ShopifyUnavailable(ShopifyError):
    """Shopify could not be reached or was overloaded — a retry later can succeed."""

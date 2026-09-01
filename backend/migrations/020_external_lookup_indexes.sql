CREATE INDEX IF NOT EXISTS idx_shipments_shopify_order_name_norm
  ON shipments (LOWER(LTRIM(shopify_order_name, '#')));

CREATE INDEX IF NOT EXISTS idx_shipments_backoffice_invoice_norm
  ON shipments (LOWER(backoffice_invoice_number));

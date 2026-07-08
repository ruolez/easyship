CREATE TABLE users (
  id SERIAL PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE shopify_stores (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  shop_domain TEXT NOT NULL,
  access_token TEXT NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE shipments (
  id SERIAL PRIMARY KEY,
  source TEXT NOT NULL CHECK (source IN ('shopify', 'backoffice', 'manual')),
  shopify_store_id INT REFERENCES shopify_stores(id),
  shopify_order_id TEXT,
  shopify_order_name TEXT,
  backoffice_invoice_id INT,
  backoffice_invoice_number TEXT,
  destination JSONB NOT NULL,
  parcels JSONB NOT NULL,
  items JSONB,
  easyship_shipment_id TEXT,
  courier_name TEXT,
  courier_service_id TEXT,
  rate JSONB,
  shipping_cost NUMERIC(10,2),
  tracking_number TEXT,
  label_path TEXT,
  label_format TEXT DEFAULT 'pdf',
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'rated', 'label_created', 'fulfilled', 'voided', 'error')),
  error_message TEXT,
  writeback_shopify_at TIMESTAMPTZ,
  writeback_backoffice_at TIMESTAMPTZ,
  created_by INT NOT NULL REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_shipments_created_at ON shipments (created_at DESC);
CREATE INDEX idx_shipments_backoffice_invoice ON shipments (backoffice_invoice_id);
CREATE INDEX idx_shipments_shopify_order ON shipments (shopify_order_id);

CREATE TABLE audit_log (
  id SERIAL PRIMARY KEY,
  user_id INT REFERENCES users(id),
  action TEXT NOT NULL,
  detail JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_log_created_at ON audit_log (created_at DESC);

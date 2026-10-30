-- Order verification: the packer scans every product of an order before it
-- ships. One verification per order (order_key = "shopify:<store>:<NAME>" or
-- "backoffice:<quotation number>"); a reset keeps the old one for history.
CREATE TABLE IF NOT EXISTS verifications (
  id SERIAL PRIMARY KEY,
  order_key TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('shopify', 'backoffice')),
  order_name TEXT NOT NULL,
  shopify_store_id INT,
  shopify_order_id TEXT,
  source_db TEXT,
  quotation_id INT,
  customer TEXT,
  status TEXT NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress', 'verified', 'reset')),
  forced BOOLEAN NOT NULL DEFAULT FALSE,
  started_by INT REFERENCES users(id),
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  verified_by INT REFERENCES users(id),
  verified_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Writebacks after verifying: Shopify tag "Checked" / DB_Admin Checker.
  -- NULL = not applicable, 'pending' | 'done' | 'failed'.
  shopify_tag_state TEXT,
  shopify_tag_error TEXT,
  dbadmin_state TEXT,
  dbadmin_error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS verifications_live_uniq ON verifications (order_key) WHERE status <> 'reset';
CREATE INDEX IF NOT EXISTS verifications_started_idx ON verifications (started_at DESC);

CREATE TABLE IF NOT EXISTS verification_lines (
  id SERIAL PRIMARY KEY,
  verification_id INT NOT NULL REFERENCES verifications(id) ON DELETE CASCADE,
  position INT NOT NULL,
  line_key TEXT NOT NULL,
  barcode TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  sku TEXT NOT NULL DEFAULT '',
  qty_required INT NOT NULL,
  qty_verified INT NOT NULL DEFAULT 0,
  scanned BOOLEAN NOT NULL DEFAULT FALSE,
  forced BOOLEAN NOT NULL DEFAULT FALSE,
  skip_reason TEXT,
  UNIQUE (verification_id, line_key)
);

-- Everything that happened, append-only. qty_before/qty_after make an event
-- undoable; client_id makes a retried request count once. Wrong scans are
-- the "Wrong barcodes" report until someone marks them fixed.
CREATE TABLE IF NOT EXISTS verification_events (
  id BIGSERIAL PRIMARY KEY,
  verification_id INT NOT NULL REFERENCES verifications(id) ON DELETE CASCADE,
  line_id INT REFERENCES verification_lines(id) ON DELETE SET NULL,
  user_id INT REFERENCES users(id),
  kind TEXT NOT NULL,
  result TEXT,
  barcode TEXT,
  units INT NOT NULL DEFAULT 0,
  qty_before INT,
  qty_after INT,
  detail JSONB,
  client_id TEXT UNIQUE,
  undone_at TIMESTAMPTZ,
  fixed_at TIMESTAMPTZ,
  fixed_by INT REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS verification_events_verification_idx ON verification_events (verification_id, id);
CREATE INDEX IF NOT EXISTS verification_events_wrong_idx ON verification_events (created_at DESC)
  WHERE kind = 'scan' AND result = 'wrong' AND fixed_at IS NULL;

-- One scan of a case barcode counts `quantity` units of the unit barcode.
CREATE TABLE IF NOT EXISTS case_barcodes (
  id SERIAL PRIMARY KEY,
  barcode TEXT NOT NULL,
  unit_barcode TEXT NOT NULL,
  quantity INT NOT NULL CHECK (quantity > 0),
  created_by INT REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS case_barcodes_barcode_uniq ON case_barcodes (barcode);

-- Box 1 of a shipment group: who bought past the verification gate.
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS verify_gate JSONB;

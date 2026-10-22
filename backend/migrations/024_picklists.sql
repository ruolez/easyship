-- Pick lists: batches of eligible Shopify orders whose products, grouped by
-- barcode, are written to a BackOffice quotation for warehouse picking.

-- Sections a regular user may open beyond the defaults, e.g. ["picklists"].
-- NULL = none; admins always have every section.
ALTER TABLE users ADD COLUMN IF NOT EXISTS allowed_sections JSONB;

-- store_id / backoffice_db_id carry no foreign keys on purpose: the existing
-- store and database deletes only check shipments, and must keep working.
CREATE TABLE picklists (
  id SERIAL PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'creating' CHECK (status IN ('creating', 'active', 'cancelled')),
  backoffice_db_id INT,
  quotation_id INT,
  quotation_number TEXT,
  customer_id INT,
  customer_name TEXT,
  sales_rep_id INT,
  sales_rep_name TEXT,
  order_count INT NOT NULL DEFAULT 0,
  unit_count INT NOT NULL DEFAULT 0,
  line_count INT NOT NULL DEFAULT 0,
  created_by INT REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  cancelled_by INT REFERENCES users(id),
  cancelled_at TIMESTAMPTZ
);

CREATE TABLE picklist_orders (
  id SERIAL PRIMARY KEY,
  picklist_id INT NOT NULL REFERENCES picklists(id) ON DELETE CASCADE,
  store_id INT NOT NULL,
  order_gid TEXT NOT NULL,
  order_name TEXT,
  customer_name TEXT,
  ship_state TEXT,
  units INT NOT NULL DEFAULT 0,
  active BOOLEAN NOT NULL DEFAULT TRUE
);

-- An order can sit on only one live pick list, even when two users generate at once.
CREATE UNIQUE INDEX picklist_orders_active_uniq ON picklist_orders (store_id, order_gid) WHERE active;
CREATE INDEX picklist_orders_picklist_idx ON picklist_orders (picklist_id);

CREATE TABLE picklist_lines (
  id SERIAL PRIMARY KEY,
  picklist_id INT NOT NULL REFERENCES picklists(id) ON DELETE CASCADE,
  barcode TEXT NOT NULL,
  product_id INT,
  sku TEXT,
  description TEXT,
  qty INT NOT NULL
);
CREATE INDEX picklist_lines_picklist_idx ON picklist_lines (picklist_id);

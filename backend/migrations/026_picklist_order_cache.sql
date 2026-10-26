-- The Pick lists page opens on the orders read last time; each load then asks
-- Shopify only for the orders changed since, and the whole window is read
-- again now and then. node is the raw GraphQL order, judged again on every
-- read. No foreign keys, like the other picklist tables.
CREATE TABLE picklist_order_cache (
  store_id INT NOT NULL,
  order_gid TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  node JSONB NOT NULL,
  PRIMARY KEY (store_id, order_gid)
);

-- changed_since: the next read of changes asks for orders updated since then.
-- synced_at: the last read of either kind; full_at: the last whole read.
CREATE TABLE picklist_store_sync (
  store_id INT PRIMARY KEY,
  changed_since TIMESTAMPTZ NOT NULL,
  synced_at TIMESTAMPTZ NOT NULL,
  full_at TIMESTAMPTZ NOT NULL
);

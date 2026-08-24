-- Named shipping-provider instances: several accounts of one platform can
-- coexist (e.g. two ShipStation accounts). `key` is the stable identity used
-- everywhere a provider name was used before (settings prefix, shipments.provider,
-- users.allowed_providers, the nav selector); `label` is the editable alias.
-- The four pre-existing platforms become instances whose key IS the platform
-- name, so every existing setting, shipment and permission keeps working.
CREATE TABLE provider_instances (
  id SERIAL PRIMARY KEY,
  platform TEXT NOT NULL,
  key TEXT UNIQUE NOT NULL,
  label TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO provider_instances (platform, key, label) VALUES
  ('easyship', 'easyship', 'Easyship'),
  ('shippo', 'shippo', 'GoShippo'),
  ('easypost', 'easypost', 'EasyPost'),
  ('shipstation', 'shipstation', 'ShipStation');

-- Easyship's two un-namespaced keys move under the primary instance prefix so
-- every instance reads `{key}_default_item_category` / `{key}_excluded_service_ids`.
INSERT INTO settings (key, value, updated_at)
  SELECT 'easyship_default_item_category', value, updated_at FROM settings
  WHERE key = 'default_item_category'
  ON CONFLICT (key) DO NOTHING;
DELETE FROM settings WHERE key = 'default_item_category';
INSERT INTO settings (key, value, updated_at)
  SELECT 'easyship_excluded_service_ids', value, updated_at FROM settings
  WHERE key = 'excluded_courier_service_ids'
  ON CONFLICT (key) DO NOTHING;
DELETE FROM settings WHERE key = 'excluded_courier_service_ids';

-- Alias snapshot so parcels keep a readable provider name after an instance
-- is deleted.
ALTER TABLE shipments ADD COLUMN provider_label TEXT;
UPDATE shipments s SET provider_label = pi.label
  FROM provider_instances pi WHERE pi.key = s.provider;

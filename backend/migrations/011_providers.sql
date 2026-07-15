-- Multi-provider support: which platform created each shipment, and the
-- per-box draft shipment ids of every provider that quoted (so a ship-time
-- rate pick can buy from the chosen provider and cancel the rest).
ALTER TABLE shipments ADD COLUMN provider TEXT;
ALTER TABLE shipments ADD COLUMN provider_drafts JSONB;
UPDATE shipments SET provider = 'easyship' WHERE provider IS NULL;

-- Easyship was the only platform before this migration; keep it enabled.
INSERT INTO settings (key, value) VALUES ('easyship_enabled', 'true')
  ON CONFLICT (key) DO NOTHING;

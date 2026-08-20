-- Register ShipStation (API v2) as a selectable provider. Disabled by default —
-- an admin turns it on in Settings after entering an API key.
INSERT INTO settings (key, value) VALUES ('shipstation_enabled', 'false')
  ON CONFLICT (key) DO NOTHING;

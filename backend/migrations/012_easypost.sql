-- Register EasyPost as a selectable provider. Disabled by default — an admin
-- turns it on in Settings after entering an API key. (Seeded for discoverability;
-- an unset flag already means disabled for any provider but Easyship.)
INSERT INTO settings (key, value) VALUES ('easypost_enabled', 'false')
  ON CONFLICT (key) DO NOTHING;

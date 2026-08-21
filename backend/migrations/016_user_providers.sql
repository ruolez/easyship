-- Per-user shipping-provider allow-list. NULL = unrestricted (all enabled
-- providers); a JSON array restricts a regular user to that subset. Admins
-- are never restricted.
ALTER TABLE users ADD COLUMN allowed_providers JSONB;

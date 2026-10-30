-- A user who does not ship (e.g. only verifies orders): no shipping account,
-- no shipping pages or APIs. Admins always ship.
ALTER TABLE users ADD COLUMN IF NOT EXISTS can_ship BOOLEAN NOT NULL DEFAULT TRUE;

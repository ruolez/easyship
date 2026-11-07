-- A store connects either with a pasted Admin API token (auth_method 'token')
-- or through a Dev Dashboard app's client ID + secret (auth_method 'app'),
-- whose tokens EasyShip gets itself: client credentials grant for stores in
-- the app's organization, authorization code install for any other store.
ALTER TABLE shopify_stores
  ALTER COLUMN access_token SET DEFAULT '',
  ADD COLUMN auth_method TEXT NOT NULL DEFAULT 'token' CHECK (auth_method IN ('token', 'app')),
  ADD COLUMN client_id TEXT NOT NULL DEFAULT '',
  ADD COLUMN client_secret TEXT NOT NULL DEFAULT '',
  ADD COLUMN token_grant TEXT CHECK (token_grant IN ('client_credentials', 'authorization_code')),
  ADD COLUMN token_expires_at TIMESTAMPTZ,
  ADD COLUMN refresh_token TEXT,
  ADD COLUMN refresh_expires_at TIMESTAMPTZ,
  ADD COLUMN granted_scopes TEXT,
  ADD COLUMN connected_at TIMESTAMPTZ,
  ADD COLUMN auth_error TEXT;

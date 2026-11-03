-- Endicia direct (Stamps.com SERA) rates create nothing on Stamps.com's side,
-- so each box's label request lives here between rating and buying.
-- buy_key is the purchase's Idempotency-Key, saved with the frozen request
-- (buy_body) BEFORE it is sent: SERA replays a key's response for 24 h, so a
-- lost purchase is re-sent with it instead of being bought twice.
CREATE TABLE IF NOT EXISTS stamps_drafts (
    draft_id       TEXT PRIMARY KEY,
    provider       TEXT NOT NULL,
    body           JSONB NOT NULL,
    service_id     TEXT,
    buy_key        TEXT,
    buy_body       JSONB,
    buy_started_at TIMESTAMPTZ,
    label_id       TEXT,
    label          JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_stamps_drafts_label ON stamps_drafts (label_id);

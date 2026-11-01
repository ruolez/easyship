-- Spee-Dee quotes create nothing on Spee-Dee's side (creating the order IS the
-- purchase), so the order body lives here between rating and buying.
-- order_id is recorded the moment the order exists; create_started_at marks a
-- create whose response may have been lost, so it is searched for, not re-sent.
CREATE TABLE IF NOT EXISTS speedee_drafts (
    draft_id          TEXT PRIMARY KEY,
    provider          TEXT NOT NULL,
    body              JSONB NOT NULL,
    quote             JSONB,
    order_id          TEXT,
    create_started_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_speedee_drafts_order ON speedee_drafts (order_id);

-- UDS quotes create nothing on UDS's side (creating the order IS the
-- purchase), so the order body lives here between rating and buying.
-- order_ref is the unique OrderRef sent with it (UDS refuses a second order
-- with the same one), order_id is recorded the moment the order exists, and
-- packages holds each box's barcode + label from the create response.
-- create_started_at marks a create whose response may have been lost.
CREATE TABLE IF NOT EXISTS uds_drafts (
    draft_id          TEXT PRIMARY KEY,
    provider          TEXT NOT NULL,
    body              JSONB NOT NULL,
    quote             JSONB,
    order_ref         TEXT,
    order_id          TEXT,
    packages          JSONB,
    create_started_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_uds_drafts_order ON uds_drafts (order_id);

-- Carrier tracking events, from any platform that reports them (UDS first).
-- event_id is the carrier's id for the event, so a re-read never duplicates.
CREATE TABLE IF NOT EXISTS carrier_tracking_events (
    id               SERIAL PRIMARY KEY,
    provider         TEXT NOT NULL,
    tracking_number  TEXT NOT NULL,
    event_id         BIGINT NOT NULL,
    status_code      TEXT,
    status           TEXT,
    description      TEXT,
    detail           TEXT,
    exception_detail TEXT,
    event_at         TIMESTAMPTZ,
    location         TEXT,
    signed_by        TEXT,
    left_at          TEXT,
    vpod_url         TEXT,
    signature_png    TEXT,
    photos           JSONB,
    raw              JSONB,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, tracking_number, event_id)
);
CREATE INDEX IF NOT EXISTS idx_tracking_events_number ON carrier_tracking_events (tracking_number);

-- Latest carrier tracking status per parcel (NULL until a carrier reports).
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS tracking_status TEXT;
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS tracking_status_at TIMESTAMPTZ;
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_shipments_tracking_number ON shipments (tracking_number);

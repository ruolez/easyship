-- Per-shipment shipping options chosen at rate time (e.g. signature
-- requirement), passed to whichever provider creates the draft.
ALTER TABLE shipments ADD COLUMN options JSONB NOT NULL DEFAULT '{}'::jsonb;

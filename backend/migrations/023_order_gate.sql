-- Order-gate snapshot kept on box 1 of a Shopify draft: whether the order was
-- on hold or not fully paid when it was rated. The buy endpoint re-checks
-- Shopify live and reads this only when Shopify cannot be reached. Never
-- shown in Parcels or reports.
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS order_gate JSONB;

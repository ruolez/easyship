-- Profit-gate snapshot kept on box 1 of a draft: the order economics fetched
-- at rate time, the rates the server offered, and whether the gate was
-- cleared. Read only by the buy endpoint — never shown in Parcels or reports.
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS profit_check JSONB;

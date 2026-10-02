-- The Parcels list filters by these with equality / IN; none had an index
-- (the only provider index is the partial one for manifests).
CREATE INDEX IF NOT EXISTS idx_shipments_status ON shipments (status);
CREATE INDEX IF NOT EXISTS idx_shipments_provider ON shipments (provider);
CREATE INDEX IF NOT EXISTS idx_shipments_created_by ON shipments (created_by);

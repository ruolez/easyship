ALTER TABLE shipments ADD COLUMN group_id TEXT;
ALTER TABLE shipments ADD COLUMN box_number INT NOT NULL DEFAULT 1;
ALTER TABLE shipments ADD COLUMN box_total INT NOT NULL DEFAULT 1;
CREATE INDEX idx_shipments_group_id ON shipments (group_id);

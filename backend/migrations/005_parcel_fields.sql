ALTER TABLE shipments ADD COLUMN courier_umbrella_name TEXT;
ALTER TABLE shipments ADD COLUMN total_weight_lb NUMERIC(8,2);
ALTER TABLE shipments ADD COLUMN label_created_at TIMESTAMPTZ;

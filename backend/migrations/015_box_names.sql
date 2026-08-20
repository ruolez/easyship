-- Box names are now just the dimensions; rewrite existing names so old boxes
-- match the ones created from here on.
UPDATE boxes SET name = trim_scale(length)::text || '×' || trim_scale(width)::text || '×' || trim_scale(height)::text;

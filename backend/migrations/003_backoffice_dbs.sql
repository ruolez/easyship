CREATE TABLE backoffice_dbs (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  host TEXT NOT NULL,
  port TEXT NOT NULL DEFAULT '1433',
  db_name TEXT NOT NULL,
  username TEXT NOT NULL,
  password TEXT NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE shipments ADD COLUMN backoffice_db_id INT REFERENCES backoffice_dbs(id);

-- Per order source: leave the customer's company name blank when looking up
-- the ship-to address from this store / BackOffice connection.
ALTER TABLE shopify_stores ADD COLUMN no_company BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE backoffice_dbs ADD COLUMN no_company BOOLEAN NOT NULL DEFAULT FALSE;

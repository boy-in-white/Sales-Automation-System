-- Run manually as the PostgreSQL owner AFTER Ledgerly has created its views.
-- Set a real password separately, e.g. \password repliq_reader in psql.
-- No public access to PostgreSQL is required.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'repliq_reader') THEN
        CREATE ROLE repliq_reader LOGIN;
    END IF;
END $$;
GRANT CONNECT ON DATABASE olx_business TO repliq_reader;
GRANT USAGE ON SCHEMA analytics TO repliq_reader;
GRANT SELECT ON analytics.v_inventory_stock TO repliq_reader;
GRANT USAGE ON SCHEMA business TO repliq_reader;
GRANT SELECT (id,canonical_name,category,brand,active) ON business.products TO repliq_reader;
GRANT SELECT (product_id,raw_alias) ON business.product_aliases TO repliq_reader;
ALTER ROLE repliq_reader SET default_transaction_read_only = on;
ALTER ROLE repliq_reader SET statement_timeout = '2s';
-- No INSERT/UPDATE/DELETE or access to Ledgerly's technical/customer tables.

-- Phase 1: Master Product + customer-SKU identity foundation.
-- Idempotent -- safe to run more than once against an existing
-- drizzl_inventory database. Does not touch, alter, or drop any
-- existing table. See TECHNICAL_README.md for the full design writeup.
--
-- Apply with:
--   /opt/homebrew/opt/postgresql@16/bin/psql drizzl_inventory -f migrations/001_master_product_identity.sql

CREATE TABLE IF NOT EXISTS master_products (
    product_id   SERIAL PRIMARY KEY,
    barcode      TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL,
    unit_size    TEXT,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS customer_product_skus (
    id                    SERIAL PRIMARY KEY,
    customer_id           INTEGER NOT NULL REFERENCES customers(id),
    product_id            INTEGER NOT NULL REFERENCES master_products(product_id),
    external_sku          TEXT NOT NULL,
    external_description  TEXT,
    active                BOOLEAN NOT NULL DEFAULT TRUE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (customer_id, external_sku)
);

CREATE INDEX IF NOT EXISTS idx_customer_product_skus_product_id ON customer_product_skus(product_id);

-- Seed the seven canonical Drizzl products. Idempotent on barcode --
-- re-running this file does not create duplicates or new product_ids
-- for products that already exist.
INSERT INTO master_products (barcode, product_name, unit_size) VALUES
    ('9000000000001', 'Drizzl Passionfruit Probiotic Soda', '250 ml'),
    ('9000000000002', 'Drizzl Yuzu & Elderflower Probiotic Soda', '250 ml'),
    ('9000000000003', 'Drizzl Mixed Berry Probiotic Soda', '250 ml'),
    ('9000000000004', 'Drizzl Lemon & Mint Probiotic Soda', '250 ml'),
    ('9000000000005', 'Drizzl Orange Probiotic Soda', '250 ml'),
    ('9000000000006', 'Drizzl Probiotic Sparkling Water - Passionfruit', '250 ml'),
    ('9000000000007', 'Drizzl Probiotic Sparkling Water - Lemon & Mint', '250 ml')
ON CONFLICT (barcode) DO NOTHING;

-- Seed Demo Commerce's six known SKU mappings. The seventh product (Sparkling
-- Water - Lemon & Mint) intentionally gets no row here -- no known
-- Demo Commerce SKU yet; it exists in master_products independently.
-- Demo Commerce's customer_id is looked up by name, never hard-coded. If a
-- database somehow doesn't have Demo Commerce seeded yet, this simply inserts
-- zero mapping rows rather than erroring -- the seven master products
-- above are still created either way.
INSERT INTO customer_product_skus (customer_id, product_id, external_sku)
SELECT c.id, mp.product_id, v.external_sku
FROM (VALUES
    ('9000000000001', 'DEMO-SKU-001'),
    ('9000000000002', 'DEMO-SKU-002'),
    ('9000000000003', 'DEMO-SKU-003'),
    ('9000000000004', 'DEMO-SKU-004'),
    ('9000000000005', 'DEMO-SKU-005'),
    ('9000000000006', 'DEMO-SKU-006')
) AS v(barcode, external_sku)
JOIN master_products mp ON mp.barcode = v.barcode
CROSS JOIN (SELECT id FROM customers WHERE name = 'Demo Commerce Logistics Private Limited') c
ON CONFLICT (customer_id, external_sku) DO NOTHING;

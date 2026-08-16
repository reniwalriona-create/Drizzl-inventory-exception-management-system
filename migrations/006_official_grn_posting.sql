-- Phase 8: Official GRN Posting + Canonical Inventory Sale Movements +
-- Full PO Commitment Release.
--
-- Makes grn_id the real PostgreSQL primary key of grn_receipts (mirrors
-- migrations/002's po_id swap for purchase_orders -- done now because
-- grn_receipts/grn_line_items/inventory_movements/inventory_flags are
-- all still empty, the same safest-possible-moment reasoning). Adds
-- canonical product_id identity to grn_line_items and inventory_movements,
-- and drops the legacy sku_code->products(sku_code) FKs that would
-- otherwise force either a fake legacy products row per Master Product
-- barcode, or a NULL sku_code for every canonical row (both rejected --
-- see PROJECT_HANDOFF.md and grn_posting.py).
--
-- Idempotent -- safe to run more than once. Wrapped in one transaction so
-- a failure partway through leaves the database exactly as it was.
--
-- Apply with:
--   /opt/homebrew/opt/postgresql@16/bin/psql drizzl_inventory -f migrations/006_official_grn_posting.sql

BEGIN;

-- Step 1: grn_id as a plain column first (not yet the primary key).
ALTER TABLE grn_receipts ADD COLUMN IF NOT EXISTS grn_id BIGSERIAL;

-- Step 2: swap the primary key from grn_number to grn_id. Guarded by
-- checking the *actual live* primary-key column first, so re-running
-- this file is a no-op once the swap has already happened. Only two
-- child tables reference grn_receipts.grn_number today (grn_line_items,
-- discrepancy_notes) -- both dropped/recreated exactly as migration 002
-- did for purchase_orders' five children.
DO $$
DECLARE
    current_pk text;
BEGIN
    SELECT a.attname INTO current_pk
    FROM pg_index i
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
    WHERE i.indrelid = 'grn_receipts'::regclass AND i.indisprimary;

    IF current_pk = 'grn_number' THEN
        ALTER TABLE grn_line_items DROP CONSTRAINT grn_line_items_grn_number_fkey;
        ALTER TABLE discrepancy_notes DROP CONSTRAINT discrepancy_notes_grn_number_fkey;

        ALTER TABLE grn_receipts DROP CONSTRAINT grn_receipts_pkey;
        ALTER TABLE grn_receipts ADD CONSTRAINT grn_receipts_pkey PRIMARY KEY (grn_id);

        ALTER TABLE grn_receipts ALTER COLUMN grn_number SET NOT NULL;
        ALTER TABLE grn_receipts ADD CONSTRAINT grn_receipts_grn_number_key UNIQUE (grn_number);

        ALTER TABLE grn_line_items ADD CONSTRAINT grn_line_items_grn_number_fkey FOREIGN KEY (grn_number) REFERENCES grn_receipts(grn_number);
        ALTER TABLE discrepancy_notes ADD CONSTRAINT discrepancy_notes_grn_number_fkey FOREIGN KEY (grn_number) REFERENCES grn_receipts(grn_number);
    END IF;
END $$;

-- Step 3: the future business-identity rule. Temporarily redundant with
-- the global UNIQUE(grn_number) above while grn_line_items/
-- discrepancy_notes still key off grn_number alone -- both are kept
-- until those child tables migrate to grn_id. Cross-customer GRN number
-- collisions remain unsupported until then (same transitional limitation
-- as purchase_orders.po_number after migration 002).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'grn_receipts_customer_grn_number_key'
    ) THEN
        ALTER TABLE grn_receipts ADD CONSTRAINT grn_receipts_customer_grn_number_key UNIQUE (customer_id, grn_number);
    END IF;
END $$;

-- Step 4: canonical PO linkage + extra header metadata the legacy PDF
-- schema never needed. po_id is the official PO this GRN was matched
-- against at Phase 6/7 verification time -- never independently
-- re-resolved during posting (grn_posting.py copies staged_grns.
-- official_po_id here exactly).
ALTER TABLE grn_receipts ADD COLUMN IF NOT EXISTS po_id BIGINT REFERENCES purchase_orders(po_id);
ALTER TABLE grn_receipts ADD COLUMN IF NOT EXISTS supplier_code TEXT;
ALTER TABLE grn_receipts ADD COLUMN IF NOT EXISTS dn_number TEXT;
CREATE INDEX IF NOT EXISTS idx_grn_receipts_po_id ON grn_receipts(po_id);

-- Step 5: grn_line_items canonical identity, mirroring po_line_items'
-- Phase 5 shape. product_id nullable at the DB level (legacy PDF lines
-- keep NULL); every line grn_posting.py creates MUST have it set.
-- sku_code/sku_desc keep their legacy meaning for old PDF lines; for a
-- canonical line they mirror external_sku/external_sku_description --
-- see committed_quantity()/grn discrepancy reports, which still join on
-- sku_code as document identity, never product_id.
ALTER TABLE grn_line_items ADD COLUMN IF NOT EXISTS product_id INTEGER;
ALTER TABLE grn_line_items ADD COLUMN IF NOT EXISTS external_sku TEXT;
ALTER TABLE grn_line_items ADD COLUMN IF NOT EXISTS external_sku_description TEXT;
-- Preserved source rejection facts, never the source of truth for the
-- PO-vs-GRN commitment discrepancy (see reconcile.py/grn_csv_staging.py).
ALTER TABLE grn_line_items ADD COLUMN IF NOT EXISTS source_dn_quantity NUMERIC;
ALTER TABLE grn_line_items ADD COLUMN IF NOT EXISTS source_dn_value NUMERIC;
-- Drop the legacy FK -- a canonical line's sku_code mirrors external_sku,
-- which will almost never exist as a row in the legacy `products` table,
-- and creating a fake one to satisfy the FK is explicitly forbidden.
ALTER TABLE grn_line_items DROP CONSTRAINT IF EXISTS grn_line_items_sku_code_fkey;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'grn_line_items_product_id_fkey'
    ) THEN
        ALTER TABLE grn_line_items ADD CONSTRAINT grn_line_items_product_id_fkey FOREIGN KEY (product_id) REFERENCES master_products(product_id);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_grn_line_items_product_id ON grn_line_items(product_id);

-- Step 6: inventory_movements canonical identity. product_id is the true
-- internal inventory identity for a canonical movement -- stock_by_
-- location()/current_balance_by_product() group by it directly, never
-- by the sku_code compatibility string (see reconcile.py). Drop the
-- legacy FK for the same fake-row reason as grn_line_items above -- a
-- canonical movement's sku_code is master_products.barcode, which
-- (correctly) will never be a row in legacy `products`.
ALTER TABLE inventory_movements ADD COLUMN IF NOT EXISTS product_id INTEGER;
-- One SALE movement per official GRN line (received_qty > 0) -- UNIQUE
-- so a bug can't double-create one; plain UNIQUE allows unlimited NULLs
-- for every legacy/manual movement, which never sets this.
ALTER TABLE inventory_movements ADD COLUMN IF NOT EXISTS source_grn_line_item_id INTEGER UNIQUE REFERENCES grn_line_items(id);
ALTER TABLE inventory_movements DROP CONSTRAINT IF EXISTS inventory_movements_sku_code_fkey;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'inventory_movements_product_id_fkey'
    ) THEN
        ALTER TABLE inventory_movements ADD CONSTRAINT inventory_movements_product_id_fkey FOREIGN KEY (product_id) REFERENCES master_products(product_id);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_inventory_movements_product_id ON inventory_movements(product_id);

-- Step 7: inventory_flags canonical identity -- lets a future operator
-- see which Master Product went negative without joining back through
-- movement_id, which is deliberately unFK'd (see PROJECT_HANDOFF.md's
-- Phase 4 writeup on why that FK was removed).
ALTER TABLE inventory_flags ADD COLUMN IF NOT EXISTS product_id INTEGER;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'inventory_flags_product_id_fkey'
    ) THEN
        ALTER TABLE inventory_flags ADD CONSTRAINT inventory_flags_product_id_fkey FOREIGN KEY (product_id) REFERENCES master_products(product_id);
    END IF;
END $$;

-- Step 8: staging -> official linkage, same pattern as Phase 5's
-- staged_purchase_orders.posted_po_id/staged_po_lines.posted_line_item_id.
ALTER TABLE staged_grns ADD COLUMN IF NOT EXISTS posted_grn_id BIGINT UNIQUE REFERENCES grn_receipts(grn_id);
ALTER TABLE staged_grns ADD COLUMN IF NOT EXISTS posted_at TIMESTAMPTZ;
ALTER TABLE staged_grn_lines ADD COLUMN IF NOT EXISTS posted_grn_line_item_id INTEGER UNIQUE REFERENCES grn_line_items(id);
CREATE INDEX IF NOT EXISTS idx_staged_grns_posted_grn_id ON staged_grns(posted_grn_id);

COMMIT;

-- Phase 2: Purchase Order Identity Foundation.
-- Makes po_id the real PostgreSQL primary key of purchase_orders, while
-- keeping po_number as temporary backwards-compatible scaffolding -- the
-- five existing child tables (po_line_items, appointments, grn_receipts,
-- discrepancy_notes, debit_notes) still reference po_number, not po_id,
-- until a later phase migrates them. See TECHNICAL_README.md.
--
-- Done now specifically because purchase_orders (and all five child
-- tables) currently have zero rows -- this is the safest possible moment
-- to make this change, before any real PO data exists.
--
-- Idempotent -- safe to run more than once. Wrapped in one transaction so
-- a failure partway through leaves the database exactly as it was, never
-- half-migrated.
--
-- Apply with:
--   /opt/homebrew/opt/postgresql@16/bin/psql drizzl_inventory -f migrations/002_po_identity_foundation.sql

BEGIN;

-- Step 1: add po_id as a plain column first (not yet the primary key).
-- BIGSERIAL backfills existing rows with sequential values automatically
-- as part of this statement, if any ever exist when this runs.
ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS po_id BIGSERIAL;

-- Step 2: swap the primary key from po_number to po_id. Guarded by
-- checking the *actual live* primary-key column first, so re-running
-- this file is a no-op once the swap has already happened.
DO $$
DECLARE
    current_pk text;
BEGIN
    SELECT a.attname INTO current_pk
    FROM pg_index i
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
    WHERE i.indrelid = 'purchase_orders'::regclass AND i.indisprimary;

    IF current_pk = 'po_number' THEN
        -- Drop the five existing FKs so the old PK can be dropped;
        -- recreated below against the same column once it's UNIQUE
        -- instead of PRIMARY KEY (a Postgres FK can target either).
        ALTER TABLE po_line_items DROP CONSTRAINT po_line_items_po_number_fkey;
        ALTER TABLE appointments DROP CONSTRAINT appointments_po_number_fkey;
        ALTER TABLE grn_receipts DROP CONSTRAINT grn_receipts_po_number_fkey;
        ALTER TABLE discrepancy_notes DROP CONSTRAINT discrepancy_notes_po_number_fkey;
        ALTER TABLE debit_notes DROP CONSTRAINT debit_notes_po_number_fkey;

        ALTER TABLE purchase_orders DROP CONSTRAINT purchase_orders_pkey;
        ALTER TABLE purchase_orders ADD CONSTRAINT purchase_orders_pkey PRIMARY KEY (po_id);

        ALTER TABLE purchase_orders ALTER COLUMN po_number SET NOT NULL;
        ALTER TABLE purchase_orders ADD CONSTRAINT purchase_orders_po_number_key UNIQUE (po_number);

        -- Recreated with the same names and the same (default) NO ACTION /
        -- NO ACTION behavior as before -- nothing about child-table
        -- behavior changes, only what po_number's uniqueness is backed by.
        ALTER TABLE po_line_items ADD CONSTRAINT po_line_items_po_number_fkey FOREIGN KEY (po_number) REFERENCES purchase_orders(po_number);
        ALTER TABLE appointments ADD CONSTRAINT appointments_po_number_fkey FOREIGN KEY (po_number) REFERENCES purchase_orders(po_number);
        ALTER TABLE grn_receipts ADD CONSTRAINT grn_receipts_po_number_fkey FOREIGN KEY (po_number) REFERENCES purchase_orders(po_number);
        ALTER TABLE discrepancy_notes ADD CONSTRAINT discrepancy_notes_po_number_fkey FOREIGN KEY (po_number) REFERENCES purchase_orders(po_number);
        ALTER TABLE debit_notes ADD CONSTRAINT debit_notes_po_number_fkey FOREIGN KEY (po_number) REFERENCES purchase_orders(po_number);
    END IF;
END $$;

-- Step 3: customer_id NOT NULL. Confirmed code-safe by inspection --
-- upsert_po() and every _ensure_po_stub() call site (via upsert_grn,
-- upsert_discrepancy_note, upsert_debit_note, import_appointments_csv)
-- always resolves customer_id through _ensure_customer() using the real
-- DEFAULT_CUSTOMER constant; no current code path (web upload or CLI)
-- can create a customerless PO. Idempotent by nature.
ALTER TABLE purchase_orders ALTER COLUMN customer_id SET NOT NULL;

-- Step 4: the future business-identity rule. Temporarily redundant with
-- the global UNIQUE(po_number) above while child tables still key off
-- po_number alone -- both are kept until child tables migrate to po_id
-- and the global po_number uniqueness can be relaxed.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'purchase_orders_customer_po_number_key'
    ) THEN
        ALTER TABLE purchase_orders ADD CONSTRAINT purchase_orders_customer_po_number_key UNIQUE (customer_id, po_number);
    END IF;
END $$;

COMMIT;

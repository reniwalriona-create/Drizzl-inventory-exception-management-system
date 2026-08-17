-- Phase 11: database integrity hardening -- CHECK constraints on
-- quantity-type columns. Every one of these is always a non-negative
-- physical/document count in real usage (direction/sign is expressed
-- through movement_type or the ordered-vs-received comparison, never
-- through a negative stored quantity) -- verified against live data
-- before writing this migration: zero negative values exist in any of
-- these columns in the real drizzl_inventory database.
--
-- Deliberately NOT constraining computed/derived values that ARE
-- allowed to be negative by design: inventory_movements itself never
-- stores a running balance (stock_by_location()/current_balance()
-- compute it fresh, and negative balances are intentionally allowed and
-- flagged, never blocked -- see inventory_flags). This migration only
-- constrains raw, always-non-negative document/receipt quantities.
--
-- Idempotent -- safe to run more than once (each ADD CONSTRAINT is
-- guarded by a pg_constraint existence check). Wrapped in one
-- transaction so a failure partway through leaves the database exactly
-- as it was.
--
-- Apply with:
--   /opt/homebrew/opt/postgresql@16/bin/psql drizzl_inventory -f migrations/009_integrity_check_constraints.sql

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'po_line_items_qty_nonneg') THEN
        ALTER TABLE po_line_items ADD CONSTRAINT po_line_items_qty_nonneg CHECK (qty >= 0);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'grn_line_items_received_qty_nonneg') THEN
        ALTER TABLE grn_line_items ADD CONSTRAINT grn_line_items_received_qty_nonneg CHECK (received_qty >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'grn_line_items_expected_qty_nonneg') THEN
        ALTER TABLE grn_line_items ADD CONSTRAINT grn_line_items_expected_qty_nonneg CHECK (expected_qty >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'grn_line_items_source_dn_quantity_nonneg') THEN
        ALTER TABLE grn_line_items ADD CONSTRAINT grn_line_items_source_dn_quantity_nonneg CHECK (source_dn_quantity >= 0);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'staged_po_lines_ordered_qty_nonneg') THEN
        ALTER TABLE staged_po_lines ADD CONSTRAINT staged_po_lines_ordered_qty_nonneg CHECK (ordered_qty >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'staged_po_lines_received_qty_nonneg') THEN
        ALTER TABLE staged_po_lines ADD CONSTRAINT staged_po_lines_received_qty_nonneg CHECK (received_qty >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'staged_po_lines_balanced_qty_nonneg') THEN
        ALTER TABLE staged_po_lines ADD CONSTRAINT staged_po_lines_balanced_qty_nonneg CHECK (balanced_qty >= 0);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'staged_grn_lines_received_qty_nonneg') THEN
        ALTER TABLE staged_grn_lines ADD CONSTRAINT staged_grn_lines_received_qty_nonneg CHECK (received_qty >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'staged_grn_lines_dn_quantity_nonneg') THEN
        ALTER TABLE staged_grn_lines ADD CONSTRAINT staged_grn_lines_dn_quantity_nonneg CHECK (dn_quantity >= 0);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'inventory_movements_quantity_nonneg') THEN
        ALTER TABLE inventory_movements ADD CONSTRAINT inventory_movements_quantity_nonneg CHECK (quantity >= 0);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'debit_note_items_qty_nonneg') THEN
        ALTER TABLE debit_note_items ADD CONSTRAINT debit_note_items_qty_nonneg CHECK (qty >= 0);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'appointments_booked_qty_nonneg') THEN
        ALTER TABLE appointments ADD CONSTRAINT appointments_booked_qty_nonneg CHECK (booked_qty >= 0);
    END IF;

    -- requested_qty is the magnitude of what a movement asked to move --
    -- always non-negative. available_before/resulting_balance are
    -- deliberately NOT constrained here: a negative resulting_balance is
    -- exactly the condition this row exists to flag, never something to
    -- block at the schema level.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'inventory_flags_requested_qty_nonneg') THEN
        ALTER TABLE inventory_flags ADD CONSTRAINT inventory_flags_requested_qty_nonneg CHECK (requested_qty >= 0);
    END IF;
END $$;

COMMIT;

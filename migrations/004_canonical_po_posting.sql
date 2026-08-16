-- Phase 5: Canonical PO posting -- turns a READY staged PO (Phase 3/4) into
-- an official purchase_orders/po_line_items record.
--
-- Purely additive: no column is dropped, renamed, or made NOT NULL. The
-- legacy PDF-PO ingestion path (ingest.py's upsert_po()) is completely
-- untouched by these changes and keeps inserting po_line_items rows with
-- product_id/external_sku/external_sku_description/external_tax_amount
-- left NULL, exactly as it always has.
--
-- sku_code identity note (see PROJECT_HANDOFF.md): reconcile.py's
-- committed_quantity() and friends deliberately keep using
-- po_line_items.item_code as the join key against inventory_movements/
-- grn_line_items -- that stays true even after this migration. product_id
-- here is purely additive canonical identity for display/reporting; it is
-- NOT used as a join key anywhere yet, because inventory_movements and
-- grn_receipts have not themselves been migrated to product_id (a later
-- phase). Every po_posting.py-created line mirrors external_sku into
-- item_code precisely so the existing item_code-keyed commitment/
-- discrepancy code keeps matching without modification.
--
-- Idempotent -- safe to run more than once (every statement is
-- ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS).
--
-- Apply with:
--   /opt/homebrew/opt/postgresql@16/bin/psql drizzl_inventory -f migrations/004_canonical_po_posting.sql

BEGIN;

-- purchase_orders: explicit destination fields (Phase 3 introduced these
-- on staged_purchase_orders; the official table never had them -- posting
-- needs somewhere to put them) plus external PO metadata worth keeping at
-- the official level even though the full raw row stays in po_import_rows.
ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS destination_facility_id TEXT;
ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS destination_facility_name TEXT;
ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS destination_city TEXT;
ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS external_po_created_at TIMESTAMP;
ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS external_po_modified_at TIMESTAMP;
ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS external_status TEXT;
ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS supplier_code TEXT;

-- po_line_items: canonical product identity + customer-document identity,
-- side by side. Nullable at the database level -- the legacy PDF path
-- still inserts lines with none of these set. po_posting.py enforces that
-- every line IT creates has a non-null, currently-valid product_id.
ALTER TABLE po_line_items ADD COLUMN IF NOT EXISTS product_id INTEGER REFERENCES master_products(product_id);
ALTER TABLE po_line_items ADD COLUMN IF NOT EXISTS external_sku TEXT;
ALTER TABLE po_line_items ADD COLUMN IF NOT EXISTS external_sku_description TEXT;
-- The CSV's single aggregate tax figure -- doesn't split into
-- cgst/sgst/igst/cess like the legacy PDF fields do, so it gets its own
-- column rather than a guessed split.
ALTER TABLE po_line_items ADD COLUMN IF NOT EXISTS external_tax_amount NUMERIC;
CREATE INDEX IF NOT EXISTS idx_po_line_items_product_id ON po_line_items(product_id);

-- Durable staging -> official linkage. UNIQUE so a staged record can only
-- ever point at one official PO/line, and so a bug can't double-link two
-- staged POs onto the same official row.
ALTER TABLE staged_purchase_orders ADD COLUMN IF NOT EXISTS posted_po_id BIGINT UNIQUE REFERENCES purchase_orders(po_id);
ALTER TABLE staged_purchase_orders ADD COLUMN IF NOT EXISTS posted_at TIMESTAMPTZ;
ALTER TABLE staged_po_lines ADD COLUMN IF NOT EXISTS posted_line_item_id INTEGER UNIQUE REFERENCES po_line_items(id);

COMMIT;

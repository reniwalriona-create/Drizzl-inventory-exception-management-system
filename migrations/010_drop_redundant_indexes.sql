-- Phase 11: drop genuinely redundant/stale indexes found during the
-- fresh-vs-migration-built schema equivalence check.
--
-- idx_grn_line_items_grn_number: a leftover from before migration 008
-- (Phase 10) moved grn_line_items off the grn_number text FK onto a
-- real grn_id FK. grn_number is now a display/compatibility mirror
-- column only -- no live application code filters or joins
-- grn_line_items by it anymore (verified by repository-wide search).
--
-- schema_postgres.sql never declares this index for fresh installs
-- (it was renamed to idx_grn_line_items_grn_id back in migration 008),
-- so this migration exists purely to bring an already-migrated database
-- in line with what a fresh install would look like.
--
-- (staged_purchase_orders.posted_po_id and staged_po_lines.
-- posted_line_item_id's redundant plain indexes -- duplicates of the
-- unique index each column's own UNIQUE modifier already creates --
-- were never actually created via any migration; only schema_
-- postgres.sql declared them for fresh installs, and that's fixed
-- directly in schema_postgres.sql. Nothing to drop here for those.)
--
-- Idempotent -- safe to run more than once.
--
-- Apply with:
--   /opt/homebrew/opt/postgresql@16/bin/psql drizzl_inventory -f migrations/010_drop_redundant_indexes.sql

BEGIN;

DROP INDEX IF EXISTS idx_grn_line_items_grn_number;

COMMIT;

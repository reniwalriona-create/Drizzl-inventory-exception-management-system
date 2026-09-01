-- Phase 9: Remove the legacy Discrepancy Note PDF workflow.
--
-- Discrepancy is no longer a separately-uploaded document. The system
-- now has authoritative ordered/received data of its own (official
-- purchase_orders/po_line_items vs. grn_receipts/grn_line_items), so
-- reconcile.py's official_discrepancies() computes PO-vs-GRN shortfall
-- fresh from posted records -- see TECHNICAL_README.md's Phase 9 writeup.
--
-- Safe to drop: discrepancy_notes and discrepancy_note_items were both
-- empty at the time this was written (real business data hadn't started
-- flowing in yet), and nothing outside the removed PDF upload/void
-- routes and reconcile.py's grn_discrepancies()/
-- debit_note_vs_discrepancy_note() (both removed in this same phase)
-- depended on them.
--
-- Idempotent -- safe to run more than once. Wrapped in one transaction so
-- a failure partway through leaves the database exactly as it was.
--
-- Apply with:
--   /opt/homebrew/opt/postgresql@16/bin/psql drizzl_inventory -f migrations/007_remove_legacy_discrepancy_workflow.sql

BEGIN;

DROP TABLE IF EXISTS discrepancy_note_items;
DROP TABLE IF EXISTS discrepancy_notes;

COMMIT;

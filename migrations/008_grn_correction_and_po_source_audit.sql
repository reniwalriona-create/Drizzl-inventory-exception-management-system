-- Phase 10: safe correction workflows for posted GRNs and PO source
-- warehouses.
--
-- 1. grn_receipts.supersedes_grn_id -- durable linkage between an
--    official GRN and the (voided) official GRN it corrected. Nullable,
--    self-referencing FK. "Superseded by" is deliberately NOT a second
--    column -- it's derived with a reverse query
--    (WHERE supersedes_grn_id = X), avoiding a redundant bidirectional
--    field that could drift out of sync with its own inverse.
--
-- 2. Real, mid-implementation finding: a correction must let a
--    corrected replacement GRN reuse its predecessor's grn_number (the
--    two are the same physical delivery, and Scootsy's own GRN number
--    doesn't change) -- but grn_receipts.grn_number was a blanket
--    UNIQUE column (both alone, and via UNIQUE(customer_id,
--    grn_number)), and grn_line_items.grn_number was a REAL foreign
--    key into it, which Postgres requires a full-table unique
--    constraint to support -- a partial index can't be an FK target.
--    Both had to change, mirroring the EXACT pattern already used twice
--    in this database (migrations/002's po_id swap, migrations/006's
--    grn_id swap): grn_line_items gets a real grn_id FK (backfilled
--    from the still-1:1-at-migration-time grn_number match, then made
--    NOT NULL), the old grn_number FK is dropped, and
--    grn_receipts.grn_number's uniqueness narrows from "always unique"
--    to "unique among ACTIVE (non-voided) rows" via a partial unique
--    index -- a voided, superseded GRN and its active replacement can
--    now legitimately share a grn_number; two simultaneously-active
--    GRNs still cannot. See PROJECT_HANDOFF.md's Phase 10 writeup and
--    grn_posting.py/reconcile.py for every read path that had to move
--    from grn_number-text matching to grn_id.
--
-- 3. po_source_corrections -- audit trail for changing an
--    already-assigned purchase_orders.source_location_id. A reason is
--    required at the application layer (ingest.py's
--    correct_po_source_location()); this table just makes every such
--    change a durable, queryable record instead of a silent overwrite.
--
-- Idempotent -- safe to run more than once. Wrapped in one transaction so
-- a failure partway through leaves the database exactly as it was.
--
-- Apply with:
--   /opt/homebrew/opt/postgresql@16/bin/psql drizzl_inventory -f migrations/008_grn_correction_and_po_source_audit.sql

BEGIN;

-- --- 1. Correction linkage ---------------------------------------------
ALTER TABLE grn_receipts ADD COLUMN IF NOT EXISTS supersedes_grn_id BIGINT;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'grn_receipts_supersedes_grn_id_fkey'
    ) THEN
        ALTER TABLE grn_receipts ADD CONSTRAINT grn_receipts_supersedes_grn_id_fkey
            FOREIGN KEY (supersedes_grn_id) REFERENCES grn_receipts(grn_id);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_grn_receipts_supersedes_grn_id ON grn_receipts(supersedes_grn_id);

-- --- 2. grn_line_items: migrate off the grn_number text FK onto grn_id --
ALTER TABLE grn_line_items ADD COLUMN IF NOT EXISTS grn_id BIGINT;
UPDATE grn_line_items gli SET grn_id = gr.grn_id
    FROM grn_receipts gr
    WHERE gr.grn_number = gli.grn_number AND gli.grn_id IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM grn_line_items WHERE grn_id IS NULL) THEN
        RAISE EXCEPTION 'grn_line_items has row(s) with no matching grn_receipts.grn_number -- refusing to proceed, this should be impossible under the old FK.';
    END IF;
END $$;

ALTER TABLE grn_line_items ALTER COLUMN grn_id SET NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'grn_line_items_grn_id_fkey') THEN
        ALTER TABLE grn_line_items ADD CONSTRAINT grn_line_items_grn_id_fkey
            FOREIGN KEY (grn_id) REFERENCES grn_receipts(grn_id);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_grn_line_items_grn_id ON grn_line_items(grn_id);

ALTER TABLE grn_line_items DROP CONSTRAINT IF EXISTS grn_line_items_grn_number_fkey;

-- --- 3. grn_receipts.grn_number: unique among ACTIVE rows only ---------
ALTER TABLE grn_receipts DROP CONSTRAINT IF EXISTS grn_receipts_grn_number_key;
ALTER TABLE grn_receipts DROP CONSTRAINT IF EXISTS grn_receipts_customer_grn_number_key;
CREATE UNIQUE INDEX IF NOT EXISTS grn_receipts_active_grn_number_key ON grn_receipts(grn_number) WHERE voided = 0;

-- --- 4. PO source-warehouse correction audit ----------------------------
CREATE TABLE IF NOT EXISTS po_source_corrections (
    id                       SERIAL PRIMARY KEY,
    po_id                    BIGINT NOT NULL REFERENCES purchase_orders(po_id),
    old_source_location_id   INTEGER REFERENCES locations(id),
    new_source_location_id   INTEGER NOT NULL REFERENCES locations(id),
    reason                   TEXT NOT NULL,
    created_at               TEXT DEFAULT CURRENT_TIMESTAMP::text
);
CREATE INDEX IF NOT EXISTS idx_po_source_corrections_po_id ON po_source_corrections(po_id);

COMMIT;

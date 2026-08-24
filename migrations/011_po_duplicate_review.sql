-- Durable operator decisions for staged POs whose number already exists.
BEGIN;

ALTER TABLE staged_purchase_orders
    ADD COLUMN IF NOT EXISTS duplicate_disposition TEXT,
    ADD COLUMN IF NOT EXISTS duplicate_review_reason TEXT,
    ADD COLUMN IF NOT EXISTS duplicate_official_po_id BIGINT REFERENCES purchase_orders(po_id),
    ADD COLUMN IF NOT EXISTS duplicate_reviewed_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'staged_po_duplicate_disposition_check'
    ) THEN
        ALTER TABLE staged_purchase_orders
            ADD CONSTRAINT staged_po_duplicate_disposition_check
            CHECK (duplicate_disposition IN ('keep_existing', 'treat_as_duplicate'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_staged_pos_duplicate_official_po_id
    ON staged_purchase_orders(duplicate_official_po_id);

COMMIT;

-- Preserve the financial value and completion date supplied by each
-- discrepancy CSV row. These fields are reporting-only and never move stock.
BEGIN;

ALTER TABLE staged_discrepancy_lines
    ADD COLUMN IF NOT EXISTS rejected_amount NUMERIC,
    ADD COLUMN IF NOT EXISTS completed_date DATE;

-- Backfill already-staged rows from their preserved source JSON. Only cast
-- values whose shape is known-safe; anything unusual remains NULL for review.
UPDATE staged_discrepancy_lines
SET rejected_amount = REPLACE(raw_data->>'TotalRejectedAmount', ',', '')::NUMERIC
WHERE rejected_amount IS NULL
  AND REPLACE(COALESCE(raw_data->>'TotalRejectedAmount', ''), ',', '')
      ~ '^([0-9]+([.][0-9]+)?|[.][0-9]+)$';

UPDATE staged_discrepancy_lines
SET completed_date = (raw_data->>'CompletedDate')::DATE
WHERE completed_date IS NULL
  AND COALESCE(raw_data->>'CompletedDate', '') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'staged_discrepancy_lines_rejected_amount_nonneg'
    ) THEN
        ALTER TABLE staged_discrepancy_lines
            ADD CONSTRAINT staged_discrepancy_lines_rejected_amount_nonneg
            CHECK (rejected_amount >= 0);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_staged_discrepancy_completed_date
    ON staged_discrepancy_lines(completed_date);

COMMIT;

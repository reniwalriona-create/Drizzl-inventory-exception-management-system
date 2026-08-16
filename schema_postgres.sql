-- Drizzl inventory tracking database, v2.
--
-- Core idea: `inventory_movements` is the single source of truth for how
-- much stock exists and where. Every real event -- a GRN receipt, a
-- damaged-goods write-off, a flea-market withdrawal, an inter-city
-- transfer -- becomes one row there, whether or not a PO/GRN/invoice
-- exists behind it. PO/GRN/discrepancy/debit-note tables still capture
-- the formal paperwork when it exists, and feed the ledger automatically;
-- they are no longer required for stock to move.

-- Businesses that send Drizzl POs and buy from them (Scootsy today, more
-- retail partners expected). Note: in the PO/GRN PDFs themselves, "Vendor
-- Name" means Drizzl/Demo Beverage Company (the seller) -- this table is
-- the other side of that relationship, the buyer, hence "customers".
CREATE TABLE customers (
    id         SERIAL PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    notes      TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP::text
);

-- SKU catalog. Rows get created automatically the first time a SKU is
-- seen in any parsed/imported document; pack_size exists because at
-- least one real document priced a "pack of 6" differently from a single
-- can, and quantities must not get silently summed across units.
CREATE TABLE products (
    sku_code   TEXT PRIMARY KEY,
    sku_desc   TEXT,
    brand      TEXT,
    pack_size  TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP::text
);

-- Physical/logical places stock can sit: Drizzl's own facilities,
-- consignment partners, one-off market events. Scootsy's own warehouses
-- are NOT modeled here -- once a GRN is confirmed there, that stock is
-- sold and off Drizzl's books, so there's no running balance to track.
CREATE TABLE locations (
    id         SERIAL PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    type       TEXT NOT NULL,  -- 'own_facility' | 'consignment_partner' | 'market_event'
    created_at TEXT DEFAULT CURRENT_TIMESTAMP::text
);

CREATE TABLE users (
    id             SERIAL PRIMARY KEY,
    username       TEXT UNIQUE NOT NULL,
    password_hash  TEXT NOT NULL,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP::text
);

-- The ledger. current stock at a location = sum of quantity_in minus
-- quantity_out for that location, derived from movement_type + the
-- from/to columns below (see reconcile.py for the actual query).
CREATE TABLE inventory_movements (
    id               SERIAL PRIMARY KEY,
    movement_date    TEXT NOT NULL,
    sku_code         TEXT REFERENCES products(sku_code),
    movement_type    TEXT NOT NULL,  -- 'production' | 'opening_balance' | 'transfer' | 'sale' | 'loss'
    quantity         REAL NOT NULL,  -- always positive; direction comes from movement_type
    location_from_id INTEGER REFERENCES locations(id),  -- null for production/opening_balance
    location_to_id   INTEGER REFERENCES locations(id),  -- null for sale/loss
    reason           TEXT,   -- e.g. 'damaged', 'expired', free text for manual entries
    reference_type   TEXT,   -- 'po' | 'grn' | 'discrepancy_note' | 'debit_note' | 'manual'
    reference_id     TEXT,   -- the relevant document number, or null for manual entries
    notes            TEXT,
    -- set only when a human explicitly chose "Continue Anyway" on the
    -- negative-inventory warning for a manual transfer/sale/loss -- see
    -- app.py's new_movement() and reconcile.py's negative_balances().
    negative_override_reason TEXT,
    -- Same idea, one tier down: set only when a human overrode the
    -- *commitment* warning (physical stock stays non-negative, but the
    -- movement eats into stock already promised to an open PO). Kept as
    -- a separate column from negative_override_reason on purpose -- the
    -- two warnings are conceptually different severities and must not
    -- be collapsed together. See reconcile.py's committed_at_location().
    commitment_override_reason TEXT,
    recorded_by      INTEGER REFERENCES users(id),  -- null for system-generated rows
    -- Void (not delete) is how a wrong entry gets corrected -- the row
    -- stays forever (audit trail, standard practice for a ledger), it's
    -- just excluded from every calculation (see reconcile.py's voided=0
    -- filters). Reversible: reconcile.voided_entries() lists every
    -- voided row for review, ingest.py's unvoid_movement() restores one.
    voided           INTEGER NOT NULL DEFAULT 0,
    void_reason      TEXT,
    voided_at        TEXT,
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP::text
);
CREATE INDEX idx_movements_sku ON inventory_movements(sku_code);
CREATE INDEX idx_movements_date ON inventory_movements(movement_date);

-- po_number is kept as the join key across tables rather than a synthetic
-- id, since every document we've seen prints it as the natural reference.
-- Known limitation: this assumes PO numbers stay unique across customers.
-- True today (only Scootsy). Revisit with a composite (customer_id,
-- po_number) key if/when a second customer's numbering scheme is known
-- to collide with Scootsy's.
-- Phase 2 (2026-08-15): po_id is the real internal identity now, not
-- po_number. po_number stays NOT NULL + UNIQUE as temporary backwards-
-- compatible scaffolding -- po_line_items/appointments/grn_receipts/
-- discrepancy_notes/debit_notes all still reference po_number, not po_id,
-- until a later phase migrates them (a Postgres FK can target a UNIQUE
-- column just as well as a PRIMARY KEY, which is what makes this work
-- without changing any child table below). UNIQUE(customer_id, po_number)
-- is the future business-identity rule, temporarily redundant with the
-- table-wide UNIQUE(po_number) until child tables migrate off po_number
-- and that global uniqueness can be relaxed to allow cross-customer PO
-- number collisions. See PROJECT_HANDOFF.md and
-- migrations/002_po_identity_foundation.sql.
CREATE TABLE purchase_orders (
    po_id                    BIGSERIAL PRIMARY KEY,
    po_number                TEXT NOT NULL UNIQUE,
    customer_id              INTEGER NOT NULL REFERENCES customers(id),
    po_date                  TEXT,
    po_release_date          TEXT,
    payment_terms            TEXT,
    expected_delivery_date   TEXT,
    po_expiry_date           TEXT,
    vendor_name               TEXT,  -- as printed on the doc: Drizzl itself
    vendor_gstin               TEXT,
    facility_name               TEXT,
    grand_total                  REAL,
    source_file                   TEXT,
    -- Phase 5: the CUSTOMER's receiving facility, mirrored from the staged
    -- PO's destination_* fields at posting time -- NOT a Drizzl location,
    -- never confused with source_location_id below. facility_name above is
    -- kept in sync (= destination_facility_name) for older UI/reports.
    destination_facility_id      TEXT,
    destination_facility_name     TEXT,
    destination_city               TEXT,
    -- Phase 5: external PO metadata from the CSV staging snapshot, copied
    -- through at posting time. The full raw CSV row remains in
    -- po_import_rows regardless -- these are just the fields worth
    -- surfacing at the official level too.
    external_po_created_at          TIMESTAMP,
    external_po_modified_at          TIMESTAMP,
    external_status                   TEXT,
    supplier_code                      TEXT,
    -- The Drizzl location expected to fulfill this PO -- e.g. "Mumbai".
    -- Completely separate from facility_name above, which is Scootsy's
    -- own receiving warehouse ("DEMO FACILITY A") -- never inferred from one
    -- another (a Scootsy facility code doesn't reliably map to a Drizzl
    -- location). Null means "not allocated yet"; assigned via
    -- ingest.py's assign_po_source_location(), never guessed. Drives
    -- both the Committed-inventory calculation (reconcile.py's
    -- committed_quantity()) and which location a resulting GRN's sale
    -- movement comes from (ingest.py's upsert_grn()).
    source_location_id             INTEGER REFERENCES locations(id),
    -- Void, not delete -- see inventory_movements.voided above.
    voided                         INTEGER NOT NULL DEFAULT 0,
    void_reason                    TEXT,
    voided_at                      TEXT,
    created_at                     TEXT DEFAULT CURRENT_TIMESTAMP::text,
    CONSTRAINT purchase_orders_customer_po_number_key UNIQUE (customer_id, po_number)
);

CREATE TABLE po_line_items (
    id               SERIAL PRIMARY KEY,
    po_number        TEXT NOT NULL REFERENCES purchase_orders(po_number),
    sno              TEXT,
    item_code        TEXT,
    item_desc        TEXT,
    hsn_code         TEXT,
    qty              REAL,
    mrp              REAL,
    unit_base_cost   REAL,
    taxable_value    REAL,
    cgst_rate        REAL,
    cgst_amt         REAL,
    sgst_rate        REAL,
    sgst_amt         REAL,
    igst_rate        REAL,
    igst_amt         REAL,
    cess_rate        REAL,
    cess_amt         REAL,
    add_cess         REAL,
    total            REAL,
    -- Phase 5: canonical product identity alongside the legacy/document
    -- identity. NULL for every line the legacy PDF path (upsert_po())
    -- creates -- only po_posting.py populates these, and only from an
    -- already-reviewed staged_po_lines snapshot, never re-resolved.
    -- Deliberately NOT used as a join key anywhere yet (see
    -- reconcile.committed_quantity()): item_code/item_desc are mirrored
    -- from external_sku/external_sku_description on every canonical line
    -- specifically so the existing item_code-keyed commitment/GRN-matching
    -- code keeps working unmodified until inventory_movements/grn_receipts
    -- get their own product_id migration in a later phase.
    -- No inline REFERENCES here -- master_products is defined later in
    -- this file; the FK is added below once it exists (same forward-
    -- reference pattern migrations/002 handles for po_number).
    product_id                INTEGER,
    external_sku              TEXT,
    external_sku_description  TEXT,
    -- The CSV's single aggregate tax figure -- doesn't split into
    -- cgst/sgst/igst/cess like the legacy PDF fields, so it isn't forced
    -- into them. NULL for legacy PDF-sourced lines.
    external_tax_amount       NUMERIC
);
CREATE INDEX idx_po_line_items_po_number ON po_line_items(po_number);
CREATE INDEX idx_po_line_items_product_id ON po_line_items(product_id);

-- Warehouse delivery slot bookings (from the appointment CSV export).
CREATE TABLE appointments (
    id             SERIAL PRIMARY KEY,
    appointment_id TEXT UNIQUE,
    po_number      TEXT REFERENCES purchase_orders(po_number),
    facility_name  TEXT,
    slot_date      TEXT,
    slot_time      TEXT,
    booked_qty     REAL,
    state          TEXT,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP::text
);
CREATE INDEX idx_appointments_po_number ON appointments(po_number);

-- One row per goods-receipt document. Confirms delivery accepted AND
-- that Drizzl will be paid for it -- this is the point stock is
-- considered SOLD and stops being tracked as Drizzl's own inventory.
-- Receiving a GRN automatically creates a 'sale' row in
-- inventory_movements (see ingest.py); nothing here blocks a GRN from
-- existing without a matching PO row -- po_number is optional.
CREATE TABLE grn_receipts (
    grn_number     TEXT PRIMARY KEY,
    po_number      TEXT REFERENCES purchase_orders(po_number),
    customer_id    INTEGER REFERENCES customers(id),
    inbound_no     TEXT,
    grn_date       TEXT,
    create_date    TEXT,
    invoice_no     TEXT,
    invoice_date   TEXT,
    challan_no     TEXT,
    challan_date   TEXT,
    vendor_name    TEXT,
    facility_name  TEXT,
    source         TEXT,  -- always 'pdf' -- GRNs arrive as individual PDFs
    source_file    TEXT,
    -- Fallback/override for which Drizzl location this GRN's sale
    -- movement(s) should come from. Only consulted if this GRN has no
    -- po_number, or its PO exists but has no source_location_id of its
    -- own -- when a PO source is set, that takes precedence (see
    -- ingest.py's _resolve_grn_source_location()). Never guessed/
    -- defaulted; assigned via assign_grn_source_location().
    source_location_id INTEGER REFERENCES locations(id),
    -- Void, not delete -- see inventory_movements.voided above. Voiding a
    -- GRN also voids the sale movement(s) it created (ingest.py's
    -- void_grn()), so "the numbers" reflect the void everywhere at once.
    voided         INTEGER NOT NULL DEFAULT 0,
    void_reason    TEXT,
    voided_at      TEXT,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP::text
);
CREATE INDEX idx_grn_receipts_po_number ON grn_receipts(po_number);

CREATE TABLE grn_line_items (
    id                SERIAL PRIMARY KEY,
    grn_number        TEXT NOT NULL REFERENCES grn_receipts(grn_number),
    sku_code          TEXT REFERENCES products(sku_code),
    sku_desc          TEXT,
    lot_no            TEXT,
    lot_mrp           REAL,
    lot_expiry_date   TEXT,
    -- expected_qty = what the delivery challan/PDF said should arrive on
    -- this line ("Exp Qty" on the GRN PDF).
    expected_qty      REAL,
    -- received_qty = what was actually counted in at the warehouse. This
    -- is always the true "sold" quantity -- see ingest.py's upsert_grn.
    received_qty      REAL,
    unit_price        REAL,
    taxable_value     REAL,
    cgst_rate         REAL,
    cgst_amt          REAL,
    sgst_rate         REAL,
    sgst_amt          REAL,
    igst_rate         REAL,
    igst_amt          REAL,
    cess_rate         REAL,
    cess_amt          REAL,
    add_cess          REAL,
    total             REAL
);
CREATE INDEX idx_grn_line_items_grn_number ON grn_line_items(grn_number);

-- Discrepancy Note: itemized detail of what went wrong with a GRN line
-- (reason + who's at fault), issued alongside/after a GRN. Purely
-- informational for now -- it does NOT auto-create a 'loss' row in
-- inventory_movements (see ingest.py's upsert_discrepancy_note and
-- PROJECT_HANDOFF.md section 4 for why).
CREATE TABLE discrepancy_notes (
    dn_number      TEXT PRIMARY KEY,
    dn_date        TEXT,
    po_number      TEXT REFERENCES purchase_orders(po_number),
    grn_number     TEXT REFERENCES grn_receipts(grn_number),
    invoice_number TEXT,
    inbound_no     TEXT,
    customer_id    INTEGER REFERENCES customers(id),
    grn_qty        REAL,
    grn_amt        REAL,
    total_dn_qty   REAL,
    dn_amt         REAL,
    invoice_amt    REAL,
    source_file    TEXT,
    -- Void, not delete -- see inventory_movements.voided above.
    voided         INTEGER NOT NULL DEFAULT 0,
    void_reason    TEXT,
    voided_at      TEXT,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP::text
);

CREATE TABLE discrepancy_note_items (
    id            SERIAL PRIMARY KEY,
    dn_number     TEXT NOT NULL REFERENCES discrepancy_notes(dn_number),
    sno           TEXT,
    sku_code      TEXT REFERENCES products(sku_code),
    hsn_code      TEXT,
    sku_desc      TEXT,
    reason        TEXT,   -- e.g. 'Damaged'
    remarks       TEXT,   -- e.g. 'DP WORLD-DAMAGE' -- who's at fault
    exp_qty       REAL,   -- expected per the PO
    dn_qty        REAL,   -- quantity flagged in this discrepancy
    lot_mrp       REAL,
    unit_price    REAL,
    taxable_value REAL,
    cgst_rate     REAL,
    cgst_amt      REAL,
    sgst_rate     REAL,
    sgst_amt      REAL,
    igst_rate     REAL,
    igst_amt      REAL,
    cess_rate     REAL,
    cess_amt      REAL,
    total         REAL
);
CREATE INDEX idx_discrepancy_note_items_dn_number ON discrepancy_note_items(dn_number);

-- CPD Debit Note: the financial side of a discrepancy -- how much is
-- being deducted from Drizzl's payout, referencing the same GRN/PO.
CREATE TABLE debit_notes (
    note_number       TEXT PRIMARY KEY,
    reference_number  TEXT,   -- sometimes equals a grn_number
    po_number         TEXT REFERENCES purchase_orders(po_number),
    invoice_number    TEXT,
    discrepancy_type  TEXT,   -- e.g. 'QDN'
    note_date         TEXT,
    customer_id       INTEGER REFERENCES customers(id),
    sub_total         REAL,
    tax_amount        REAL,
    total_amount      REAL,
    credits_remaining REAL,
    source_file       TEXT,
    created_at        TEXT DEFAULT CURRENT_TIMESTAMP::text
);

-- A document still gets stored even if something about it looks off (a
-- missing field, totals that don't add up) -- it's logged here instead
-- of silently trusted or silently dropped, so a human can go check it.
CREATE TABLE ingestion_flags (
    id             SERIAL PRIMARY KEY,
    document_type  TEXT NOT NULL,  -- 'po' | 'grn' | 'discrepancy_note' | 'debit_note'
    document_id    TEXT NOT NULL,  -- the relevant document number
    issue          TEXT NOT NULL,
    source_file    TEXT,
    resolved       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP::text
);
CREATE INDEX idx_ingestion_flags_unresolved ON ingestion_flags(resolved);

-- Two different severities of inventory incident, both logged here,
-- distinguished by `source`:
--   'manual_override'    a human explicitly overrode the *physical*
--                         negative-inventory warning on a manual
--                         transfer/sale/loss (on-hand would go < 0).
--   'grn'                 a real GRN's sale movement pushed its source
--                         location's on-hand below zero (GRNs are never
--                         blocked -- see ingest.py's upsert_grn()).
--   'commitment_override'  a human explicitly overrode the *commitment*
--                         warning -- physical on-hand stays >= 0, but
--                         the movement eats into stock already reserved
--                         for an open PO (see reconcile.py's
--                         committed_at_location()). One tier less
--                         severe than the other two; kept as a distinct
--                         `source` value rather than collapsed into
--                         'manual_override' so the two are never
--                         confused when reviewing this table.
-- In every case this almost always means an earlier production/
-- transfer/opening_balance entry is missing, not that the movement
-- itself was wrong -- kept open until a human investigates and resolves
-- it. movement_id is informational only (no FK) -- like ingestion_flags'
-- document_id, deliberately not enforced, since a flag must never block
-- or be blocked by what happens to the movement it's about (e.g. a GRN
-- re-upload deletes and recreates its movements; an enforced FK here
-- would break that).
CREATE TABLE inventory_flags (
    id                SERIAL PRIMARY KEY,
    movement_id       INTEGER,
    sku_code          TEXT,
    location_name     TEXT,
    source            TEXT NOT NULL,  -- 'manual_override' | 'grn' | 'commitment_override'
    reference_id      TEXT,           -- grn_number for source='grn', else null
    available_before  REAL,
    requested_qty     REAL,
    resulting_balance REAL,
    reason            TEXT,           -- the human's override reason (source='manual_override' only)
    resolved          INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT DEFAULT CURRENT_TIMESTAMP::text
);
CREATE INDEX idx_inventory_flags_unresolved ON inventory_flags(resolved);

-- One row per action taken on the web app -- document uploads, manual
-- movements, flag resolutions. Exists so there's one place to see
-- everything that happened, in order, without having to cross-reference
-- separate document/movement tables.
CREATE TABLE activity_log (
    id             SERIAL PRIMARY KEY,
    action_type    TEXT NOT NULL,  -- 'po_upload' | 'grn_upload' | 'discrepancy_note_upload' | 'movement' | 'flag_resolved'
    description    TEXT NOT NULL,  -- human-readable summary, built at the time of the action
    reference_type TEXT,           -- 'po' | 'grn' | 'discrepancy_note' | 'movement' | 'ingestion_flag'
    reference_id   TEXT,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP::text
);
CREATE INDEX idx_activity_log_created_at ON activity_log(created_at);

CREATE TABLE debit_note_items (
    id          SERIAL PRIMARY KEY,
    note_number TEXT NOT NULL REFERENCES debit_notes(note_number),
    description TEXT,
    -- Debit Note line items are description-only, no SKU code printed on
    -- the document -- left null when it can't be confidently matched to
    -- a known product rather than guessed from the description text.
    sku_code    TEXT REFERENCES products(sku_code),
    qty         REAL,
    rate        REAL,
    amount      REAL
);

-- Phase 1 of the Master Product / customer-SKU identity system (added
-- 2026-08-15). Deliberately NOT wired into any table above yet -- the
-- legacy `products` table (keyed by sku_code) keeps working exactly as
-- before for every existing PO/GRN/movement path. See PROJECT_HANDOFF.md.

-- Drizzl's own canonical product catalog. barcode is the real-world
-- business identifier; product_id is only the internal relational key.
CREATE TABLE master_products (
    product_id   SERIAL PRIMARY KEY,
    barcode      TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL,
    unit_size    TEXT,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Deferred FK -- po_line_items.product_id (Phase 5) is declared earlier in
-- this file, before master_products exists yet.
ALTER TABLE po_line_items ADD CONSTRAINT po_line_items_product_id_fkey FOREIGN KEY (product_id) REFERENCES master_products(product_id);

-- Bridges a customer's own SKU code to Drizzl's master product. Not
-- assumed globally unique -- two different customers may reuse the same
-- external_sku for two different products, so uniqueness is scoped to
-- (customer_id, external_sku). A customer may have more than one
-- external_sku pointing at the same product_id over time (e.g. a
-- replacement code), so (customer_id, product_id) is deliberately NOT
-- made unique.
CREATE TABLE customer_product_skus (
    id                    SERIAL PRIMARY KEY,
    customer_id           INTEGER NOT NULL REFERENCES customers(id),
    product_id            INTEGER NOT NULL REFERENCES master_products(product_id),
    external_sku          TEXT NOT NULL,
    external_description  TEXT,
    active                BOOLEAN NOT NULL DEFAULT TRUE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (customer_id, external_sku)
);
CREATE INDEX idx_customer_product_skus_product_id ON customer_product_skus(product_id);

-- Phase 3 (2026-08-15): PO CSV staging infrastructure. Staged data never
-- reaches the official ledger (purchase_orders/po_line_items/
-- inventory_movements etc.) -- see po_csv_staging.py and
-- migrations/003_po_csv_staging.sql. Review/posting is a later phase.

-- One row per uploaded CSV file. file_sha256 + customer_id gives exact-
-- file idempotency: re-uploading the identical bytes for the same
-- customer reuses this batch rather than creating a duplicate.
CREATE TABLE po_import_batches (
    batch_id        BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    source_filename TEXT NOT NULL,
    file_sha256     TEXT NOT NULL,
    source_entity   TEXT,
    status          TEXT NOT NULL DEFAULT 'staged',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (customer_id, file_sha256)
);
CREATE INDEX idx_po_import_batches_customer_id ON po_import_batches(customer_id);
CREATE INDEX idx_po_import_batches_created_at ON po_import_batches(created_at);

-- Every readable CSV row, before any normalization -- raw_data preserves
-- exactly what the source file said, so nothing is ever lost even if a
-- field is malformed or a SKU is unknown.
CREATE TABLE po_import_rows (
    row_id             BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    batch_id           BIGINT NOT NULL REFERENCES po_import_batches(batch_id),
    source_row_number  INTEGER NOT NULL,
    raw_data           JSONB NOT NULL,
    validation_status  TEXT NOT NULL DEFAULT 'valid',
    validation_errors  JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (batch_id, source_row_number)
);
CREATE INDEX idx_po_import_rows_batch_id ON po_import_rows(batch_id);

-- One row per (customer, PoNumber) within a batch -- the CSV repeats
-- PO-level fields on every product line, so many po_import_rows collapse
-- into one staged PO here. source_location_id is deliberately NULL on
-- import (see po_csv_staging.py) -- never inferred from the customer's
-- destination facility, a completely different concept from a Drizzl
-- location (see destination_facility_* below).
CREATE TABLE staged_purchase_orders (
    staged_po_id               BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    batch_id                   BIGINT NOT NULL REFERENCES po_import_batches(batch_id),
    customer_id                INTEGER NOT NULL REFERENCES customers(id),

    external_po_number         TEXT NOT NULL,

    entity_raw                 TEXT,
    -- The CUSTOMER's receiving facility -- e.g. "DEMO FACILITY A", Mumbai. NOT a
    -- Drizzl location. Never used to infer source_location_id.
    destination_facility_id    TEXT,
    destination_facility_name  TEXT,
    destination_city           TEXT,

    po_created_at              TIMESTAMP,
    po_modified_at             TIMESTAMP,
    external_status            TEXT,
    supplier_code               TEXT,
    vendor_name                 TEXT,

    po_amount                   NUMERIC,
    expected_delivery_date      DATE,
    po_expiry_date               DATE,
    otb_reference_number          TEXT,
    internal_external_po           TEXT,
    po_ageing                       INTEGER,
    brand_name                       TEXT,
    reference_po_number               TEXT,

    -- The Drizzl warehouse that will fulfill this order. Always NULL on
    -- initial staging -- assigned by a human in Phase 4, never guessed
    -- from destination_facility_id/destination_facility_name/
    -- destination_city or any other field.
    source_location_id          INTEGER REFERENCES locations(id),

    validation_status            TEXT NOT NULL DEFAULT 'valid',
    validation_errors            JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Phase 5: durable link to the official PO this staged record was
    -- posted into, and when. NULL means never posted. UNIQUE so a staged
    -- record can only ever point at one official PO, and no two staged
    -- records can claim the same one. Never cleared/overwritten once set
    -- -- see po_posting.py's idempotency handling.
    posted_po_id                  BIGINT UNIQUE REFERENCES purchase_orders(po_id),
    posted_at                      TIMESTAMPTZ,

    created_at                    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (batch_id, customer_id, external_po_number)
);
CREATE INDEX idx_staged_pos_batch_id ON staged_purchase_orders(batch_id);
CREATE INDEX idx_staged_pos_customer_external_po ON staged_purchase_orders(customer_id, external_po_number);
CREATE INDEX idx_staged_pos_validation_status ON staged_purchase_orders(validation_status);
CREATE INDEX idx_staged_pos_source_location_id ON staged_purchase_orders(source_location_id);
CREATE INDEX idx_staged_pos_posted_po_id ON staged_purchase_orders(posted_po_id);

-- One row per raw CSV product line -- deliberately no UNIQUE(staged_po_id,
-- external_sku), since a future export may legitimately repeat a SKU on
-- the same PO. product_id is resolved through catalog.resolve_customer_sku()
-- at staging time -- NULL means the customer's SKU didn't map to a known
-- Drizzl master product (never guessed, never auto-created).
CREATE TABLE staged_po_lines (
    staged_line_id           BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    staged_po_id              BIGINT NOT NULL REFERENCES staged_purchase_orders(staged_po_id),
    raw_row_id                 BIGINT NOT NULL UNIQUE REFERENCES po_import_rows(row_id),
    source_row_number           INTEGER NOT NULL,

    external_sku                 TEXT,
    external_sku_description      TEXT,

    -- Resolved via catalog.resolve_customer_sku(), never legacy
    -- products.sku_code -- the customer's SKU must never become our true
    -- product identity. NULL + validation_status='blocked' if unmapped.
    product_id                     INTEGER REFERENCES master_products(product_id),

    category_id                     TEXT,

    ordered_qty                       NUMERIC,
    received_qty                       NUMERIC,
    balanced_qty                         NUMERIC,

    tax                                   NUMERIC,
    line_value_without_tax                  NUMERIC,
    line_value_with_tax                       NUMERIC,
    mrp                                         NUMERIC,
    unit_based_cost                               NUMERIC,

    validation_status                              TEXT NOT NULL DEFAULT 'valid',
    validation_errors                                JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Phase 5: durable link to the official po_line_items row this staged
    -- line was posted into. NULL means never posted. UNIQUE for the same
    -- reason as staged_purchase_orders.posted_po_id above.
    posted_line_item_id                                 INTEGER UNIQUE REFERENCES po_line_items(id),

    created_at                                        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_staged_po_lines_posted_line_item_id ON staged_po_lines(posted_line_item_id);
CREATE INDEX idx_staged_po_lines_staged_po_id ON staged_po_lines(staged_po_id);
CREATE INDEX idx_staged_po_lines_product_id ON staged_po_lines(product_id);
CREATE INDEX idx_staged_po_lines_external_sku ON staged_po_lines(external_sku);
CREATE INDEX idx_staged_po_lines_validation_status ON staged_po_lines(validation_status);

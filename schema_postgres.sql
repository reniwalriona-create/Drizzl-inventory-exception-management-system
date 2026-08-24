-- Drizzl inventory tracking database, v2.
--
-- Core idea: `inventory_movements` is the single source of truth for how
-- much stock exists and where. Every real event -- a GRN receipt, a
-- damaged-goods write-off, a flea-market withdrawal, an inter-city
-- transfer -- becomes one row there, whether or not a PO/GRN/invoice
-- exists behind it. PO/GRN/debit-note tables still capture the formal
-- paperwork when it exists, and feed the ledger automatically; they are
-- no longer required for stock to move. PO-vs-GRN discrepancy is
-- computed fresh from official posted records, never uploaded as its
-- own document (see reconcile.py's official_discrepancies(), Phase 9).

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
    -- No REFERENCES here -- Phase 8 dropped the legacy FK to
    -- products(sku_code). A canonical movement's sku_code is
    -- master_products.barcode (derived, never trusted from the caller --
    -- see ingest.py's record_movement()), which would never exist as a
    -- legacy products row; a legacy/manual movement's sku_code stays the
    -- old free-text SKU. product_id (below) is the TRUE internal
    -- inventory identity for a canonical movement -- reconcile.py's
    -- stock_by_location()/current_balance_by_product() group by it
    -- directly, never by this compatibility string.
    sku_code         TEXT,
    movement_type    TEXT NOT NULL,  -- 'production' | 'opening_balance' | 'transfer' | 'sale' | 'loss'
    quantity         REAL NOT NULL CHECK (quantity >= 0),  -- always positive; direction comes from movement_type
    location_from_id INTEGER REFERENCES locations(id),  -- null for production/opening_balance
    location_to_id   INTEGER REFERENCES locations(id),  -- null for sale/loss
    reason           TEXT,   -- e.g. 'damaged', 'expired', free text for manual entries
    reference_type   TEXT,   -- 'po' | 'grn' | 'debit_note' | 'manual'
    reference_id     TEXT,   -- the relevant document number, or null for manual entries
    notes            TEXT,
    -- Phase 8: canonical product identity. NULL for every legacy/manual
    -- movement. No inline REFERENCES -- master_products is defined later
    -- in this file; the FK is added below once it exists.
    product_id       INTEGER,
    -- Exactly one SALE movement per official GRN line (received_qty > 0)
    -- -- UNIQUE so a bug can't double-create one; plain UNIQUE allows
    -- unlimited NULLs, which every legacy/manual movement leaves it as.
    -- No inline REFERENCES -- grn_line_items is defined later in this
    -- file; the FK is added there once it exists.
    source_grn_line_item_id INTEGER UNIQUE,
    -- Direct owner for one per-product GRN discrepancy movement. Unlike
    -- reference_id text, this remains unambiguous across GRN corrections.
    source_grn_id BIGINT,
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
-- debit_notes all still reference po_number, not po_id, until a later
-- phase migrates them (a Postgres FK can target a UNIQUE
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
    qty              REAL CHECK (qty >= 0),
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
    booked_qty     REAL CHECK (booked_qty >= 0),
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
-- Phase 8: grn_id is the real internal PK (mirrors purchase_orders.po_id
-- from Phase 2) -- grn_number kept as unique compatibility scaffolding
-- for grn_line_items, which still references it directly.
-- UNIQUE(customer_id, grn_number) is the future business-
-- identity rule, temporarily redundant with the table-wide UNIQUE below
-- until those child tables migrate to grn_id.
CREATE TABLE grn_receipts (
    grn_id         BIGSERIAL PRIMARY KEY,
    -- Phase 10: NOT unique on its own -- see the partial unique index
    -- below (grn_receipts_active_grn_number_key). A corrected
    -- replacement legitimately shares its predecessor's grn_number
    -- (same physical delivery, same Scootsy-issued number) while the
    -- predecessor is voided; two simultaneously ACTIVE GRNs still can't
    -- share one. grn_line_items no longer FKs to this column (it FKs to
    -- grn_id instead, below) specifically because a plain/partial
    -- unique index can't be an FK target and grn_number can no longer
    -- promise table-wide uniqueness.
    grn_number     TEXT NOT NULL,
    po_number      TEXT REFERENCES purchase_orders(po_number),
    -- Phase 8: the official PO this GRN was matched against at Phase 6/7
    -- verification time -- copied exactly from staged_grns.official_po_id
    -- when posted, never independently re-resolved. See grn_posting.py.
    po_id          BIGINT REFERENCES purchase_orders(po_id),
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
    supplier_code  TEXT,
    dn_number      TEXT,
    source         TEXT,  -- 'pdf' (legacy) | 'csv' (Phase 8 canonical)
    source_file    TEXT,
    -- Fallback/override for which Drizzl location this GRN's sale
    -- movement(s) should come from. Only consulted if this GRN has no
    -- po_number, or its PO exists but has no source_location_id of its
    -- own -- when a PO source is set, that takes precedence (see
    -- ingest.py's _resolve_grn_source_location()). Never guessed/
    -- defaulted; assigned via assign_grn_source_location(). For a
    -- canonical GRN this mirrors the matched official PO's
    -- source_location_id at posting time -- the actual sale movement's
    -- location is always read fresh from the PO, never from here.
    source_location_id INTEGER REFERENCES locations(id),
    -- Void, not delete -- see inventory_movements.voided above. Voiding a
    -- GRN also voids the sale movement(s) it created (ingest.py's
    -- void_grn()), so "the numbers" reflect the void everywhere at once.
    voided         INTEGER NOT NULL DEFAULT 0,
    void_reason    TEXT,
    voided_at      TEXT,
    -- Phase 10: durable correction linkage -- set on the NEW official GRN
    -- when it explicitly replaces an older one (ingest.py's void_grn() on
    -- the old one runs in the same transaction as the INSERT that sets
    -- this). "Superseded by" is never stored as its own column -- derive
    -- it with `WHERE supersedes_grn_id = <this grn_id>` (see
    -- grn_posting.find_correction_target()/reconcile.lookup_document()) --
    -- a reverse query can't drift out of sync with itself the way a
    -- second hand-maintained column could.
    supersedes_grn_id BIGINT REFERENCES grn_receipts(grn_id),
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP::text
);
-- Unique among ACTIVE rows only (see the grn_number column comment
-- above) -- a voided/superseded GRN and its active replacement can
-- share a grn_number; a second simultaneously-active GRN with the same
-- number cannot. Global, not per-customer -- same "temporary Phase 8
-- compatibility scaffolding" scope as before Phase 10, just narrowed to
-- active rows.
CREATE UNIQUE INDEX grn_receipts_active_grn_number_key ON grn_receipts(grn_number) WHERE voided = 0;
CREATE INDEX idx_grn_receipts_supersedes_grn_id ON grn_receipts(supersedes_grn_id);
CREATE INDEX idx_grn_receipts_po_number ON grn_receipts(po_number);
CREATE INDEX idx_grn_receipts_po_id ON grn_receipts(po_id);

CREATE TABLE grn_line_items (
    id                SERIAL PRIMARY KEY,
    -- Phase 10: the real FK is grn_id (below), not this column --
    -- grn_number can no longer promise table-wide uniqueness (a
    -- corrected replacement shares its predecessor's grn_number), so it
    -- can't be an FK target any more. grn_number stays as a NOT NULL
    -- plain text mirror purely for display/compatibility (matching
    -- inventory_movements.reference_id's existing unenforced-text
    -- pattern) -- every real lookup must join/filter on grn_id.
    grn_number        TEXT NOT NULL,
    -- No inline REFERENCES here -- grn_receipts.grn_id already exists
    -- above in this file (grn_line_items is declared after it), so this
    -- one doesn't need the deferred-FK pattern product_id below does.
    grn_id            BIGINT NOT NULL REFERENCES grn_receipts(grn_id),
    sku_code          TEXT,
    sku_desc          TEXT,
    lot_no            TEXT,
    lot_mrp           REAL,
    lot_expiry_date   TEXT,
    -- expected_qty = what the delivery challan/PDF said should arrive on
    -- this line ("Exp Qty" on the GRN PDF). Always NULL for a canonical
    -- (Phase 8) line -- the CSV workflow has no line-level expected qty,
    -- and the PO/GRN comparison is the authoritative ordered-vs-received
    -- computation instead (see grn_csv_staging.get_grn_po_comparison()).
    expected_qty      REAL CHECK (expected_qty >= 0),
    -- received_qty = what was actually counted in at the warehouse. This
    -- is always the true "sold" quantity -- see ingest.py's upsert_grn.
    received_qty      REAL CHECK (received_qty >= 0),
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
    total             REAL,
    -- Phase 8: canonical product identity alongside legacy/document
    -- identity -- exactly the same distinction as po_line_items.
    -- product_id is NULL for every legacy PDF-sourced line; a canonical
    -- (Phase 8 CSV) line always has it set. sku_code/sku_desc mirror
    -- external_sku/external_sku_description on a canonical line
    -- specifically so the existing sku_code-keyed commitment code
    -- (reconcile.py's committed_quantity()) keeps matching without
    -- modification. No inline REFERENCES on
    -- product_id here -- master_products is defined later in this file;
    -- the FK is added below once it exists.
    product_id                INTEGER,
    external_sku              TEXT,
    external_sku_description  TEXT,
    -- Preserved source rejection facts -- never the source of truth for
    -- the PO-vs-GRN commitment discrepancy (that's ordered - received,
    -- computed fresh; see PROJECT_HANDOFF.md).
    source_dn_quantity        NUMERIC CHECK (source_dn_quantity >= 0),
    source_dn_value           NUMERIC
);
CREATE INDEX idx_grn_line_items_grn_id ON grn_line_items(grn_id);

-- Deferred FK -- inventory_movements.source_grn_line_item_id (Phase 8) is
-- declared earlier in this file, before grn_line_items exists yet.
ALTER TABLE inventory_movements ADD CONSTRAINT inventory_movements_source_grn_line_item_id_fkey FOREIGN KEY (source_grn_line_item_id) REFERENCES grn_line_items(id);
ALTER TABLE inventory_movements ADD CONSTRAINT inventory_movements_source_grn_id_fkey FOREIGN KEY (source_grn_id) REFERENCES grn_receipts(grn_id);

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
    document_type  TEXT NOT NULL,  -- 'po' | 'grn' | 'debit_note'
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
    -- available_before/resulting_balance are deliberately NOT
    -- constrained non-negative -- a negative resulting_balance is
    -- exactly the condition this row exists to flag, never something to
    -- block at the schema level (Phase 11).
    available_before  REAL,
    requested_qty     REAL CHECK (requested_qty >= 0),
    resulting_balance REAL,
    reason            TEXT,           -- the human's override reason (source='manual_override' only)
    resolved          INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT DEFAULT CURRENT_TIMESTAMP::text,
    -- Phase 8: lets a future operator see which canonical Master Product
    -- went negative without joining back through movement_id (which is
    -- deliberately unFK'd, see above). NULL for a legacy/manual flag. No
    -- inline REFERENCES -- master_products is defined later in this file.
    product_id        INTEGER
);
CREATE INDEX idx_inventory_flags_unresolved ON inventory_flags(resolved);

-- One row per action taken on the web app -- document uploads, manual
-- movements, flag resolutions. Exists so there's one place to see
-- everything that happened, in order, without having to cross-reference
-- separate document/movement tables.
CREATE TABLE activity_log (
    id             SERIAL PRIMARY KEY,
    action_type    TEXT NOT NULL,  -- 'po_upload' | 'grn_upload' | 'movement' | 'flag_resolved'
    description    TEXT NOT NULL,  -- human-readable summary, built at the time of the action
    reference_type TEXT,           -- 'po' | 'grn' | 'movement' | 'ingestion_flag'
    reference_id   TEXT,
    actor_username TEXT,           -- username at the time of the action
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP::text
);
CREATE INDEX idx_activity_log_created_at ON activity_log(created_at);

-- Phase 10: audit trail for correcting an already-assigned PO source
-- warehouse (ingest.py's correct_po_source_location()). Every change is
-- a durable, queryable row, never a silent overwrite of
-- purchase_orders.source_location_id -- see PROJECT_HANDOFF.md. A
-- reason is required at the application layer; NOT NULL here is the
-- backstop.
CREATE TABLE po_source_corrections (
    id                      SERIAL PRIMARY KEY,
    po_id                   BIGINT NOT NULL REFERENCES purchase_orders(po_id),
    old_source_location_id  INTEGER REFERENCES locations(id),
    new_source_location_id  INTEGER NOT NULL REFERENCES locations(id),
    reason                  TEXT NOT NULL,
    created_at              TEXT DEFAULT CURRENT_TIMESTAMP::text
);
CREATE INDEX idx_po_source_corrections_po_id ON po_source_corrections(po_id);

CREATE TABLE debit_note_items (
    id          SERIAL PRIMARY KEY,
    note_number TEXT NOT NULL REFERENCES debit_notes(note_number),
    description TEXT,
    -- Debit Note line items are description-only, no SKU code printed on
    -- the document -- left null when it can't be confidently matched to
    -- a known product rather than guessed from the description text.
    sku_code    TEXT REFERENCES products(sku_code),
    qty         REAL CHECK (qty >= 0),
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

-- Deferred FKs -- Phase 8's product_id columns are all declared earlier in
-- this file, before master_products exists yet.
ALTER TABLE inventory_movements ADD CONSTRAINT inventory_movements_product_id_fkey FOREIGN KEY (product_id) REFERENCES master_products(product_id);
ALTER TABLE grn_line_items ADD CONSTRAINT grn_line_items_product_id_fkey FOREIGN KEY (product_id) REFERENCES master_products(product_id);
ALTER TABLE inventory_flags ADD CONSTRAINT inventory_flags_product_id_fkey FOREIGN KEY (product_id) REFERENCES master_products(product_id);
CREATE INDEX idx_inventory_movements_product_id ON inventory_movements(product_id);
CREATE INDEX idx_grn_line_items_product_id ON grn_line_items(product_id);

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

    -- A same-customer PO number may be uploaded again. Exact copies are
    -- derived automatically; materially changed copies require an audited
    -- operator decision. Neither disposition overwrites the official PO.
    duplicate_disposition          TEXT CHECK (duplicate_disposition IN ('keep_existing', 'treat_as_duplicate')),
    duplicate_review_reason         TEXT,
    duplicate_official_po_id         BIGINT REFERENCES purchase_orders(po_id),
    duplicate_reviewed_at             TIMESTAMPTZ,

    created_at                    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (batch_id, customer_id, external_po_number)
);
CREATE INDEX idx_staged_pos_batch_id ON staged_purchase_orders(batch_id);
CREATE INDEX idx_staged_pos_customer_external_po ON staged_purchase_orders(customer_id, external_po_number);
CREATE INDEX idx_staged_pos_validation_status ON staged_purchase_orders(validation_status);
CREATE INDEX idx_staged_pos_source_location_id ON staged_purchase_orders(source_location_id);
CREATE INDEX idx_staged_pos_duplicate_official_po_id ON staged_purchase_orders(duplicate_official_po_id);
-- No separate index on posted_po_id -- the column's own UNIQUE modifier
-- above already creates staged_purchase_orders_posted_po_id_key, a
-- unique btree index on exactly this column, which already covers
-- every lookup a plain index would (Phase 11: this used to be a
-- genuinely redundant duplicate).

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

    ordered_qty                       NUMERIC CHECK (ordered_qty >= 0),
    received_qty                       NUMERIC CHECK (received_qty >= 0),
    balanced_qty                         NUMERIC CHECK (balanced_qty >= 0),

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
CREATE INDEX idx_staged_po_lines_staged_po_id ON staged_po_lines(staged_po_id);
CREATE INDEX idx_staged_po_lines_product_id ON staged_po_lines(product_id);
CREATE INDEX idx_staged_po_lines_external_sku ON staged_po_lines(external_sku);
CREATE INDEX idx_staged_po_lines_validation_status ON staged_po_lines(validation_status);

-- Phase 6 (2026-08-16): GRN CSV staging -- see migrations/005_grn_csv_staging.sql
-- for the full rationale (also in this file's header comments there).
-- Zero official-ledger effect: grn_receipts/grn_line_items/
-- inventory_movements are never touched by anything that populates these
-- tables. See grn_csv_staging.py and PROJECT_HANDOFF.md.

-- One row per uploaded GRN CSV file. Unlike the PO CSV, this export does
-- NOT identify the customer/buyer anywhere in its own data (VendorName/
-- SupplierCode identify the SUPPLIER, i.e. Drizzl) -- customer_id must
-- always be supplied explicitly by the caller, never inferred.
CREATE TABLE grn_import_batches (
    batch_id        BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    source_filename TEXT NOT NULL,
    file_sha256     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'staged',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (customer_id, file_sha256)
);
CREATE INDEX idx_grn_import_batches_customer_id ON grn_import_batches(customer_id);
CREATE INDEX idx_grn_import_batches_created_at ON grn_import_batches(created_at);

-- Every readable CSV row, before any normalization.
CREATE TABLE grn_import_rows (
    row_id             BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    batch_id           BIGINT NOT NULL REFERENCES grn_import_batches(batch_id),
    source_row_number  INTEGER NOT NULL,
    raw_data           JSONB NOT NULL,
    validation_status  TEXT NOT NULL DEFAULT 'valid',
    validation_errors  JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (batch_id, source_row_number)
);
CREATE INDEX idx_grn_import_rows_batch_id ON grn_import_rows(batch_id);

-- One row per (customer, GrnNumber) within a batch. NOT globally unique
-- on (customer_id, external_grn_number) -- a corrected/re-exported file
-- may legitimately contain the same GRN number again in a later batch;
-- that is a duplicate/conflict to detect (po_verification_status), not
-- something ingestion itself refuses to store.
CREATE TABLE staged_grns (
    staged_grn_id           BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    batch_id                BIGINT NOT NULL REFERENCES grn_import_batches(batch_id),
    customer_id             INTEGER NOT NULL REFERENCES customers(id),

    external_grn_number     TEXT NOT NULL,
    external_po_number      TEXT,
    -- The official PO this GRN was matched against -- customer-scoped
    -- exact lookup only, never a stub, never fuzzy/prefix matched.
    official_po_id          BIGINT REFERENCES purchase_orders(po_id),

    facility_name           TEXT,
    supplier_code           TEXT,
    vendor_name              TEXT,

    invoice_number            TEXT,
    invoice_date               DATE,
    external_created_at         TIMESTAMP,

    -- Blank is allowed and common. Multiple DISTINCT nonblank DN numbers
    -- within one GRN is a validation_status='blocked' condition -- never
    -- inferred/invented from DNQuantity being nonzero.
    dn_number                    TEXT,

    validation_status             TEXT NOT NULL DEFAULT 'valid',
    validation_errors             JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Independent of validation_status/validation_errors -- recomputed
    -- any time via validate_staged_grn()/revalidate_grn_batch() without
    -- touching the intrinsic parsing/normalization findings.
    po_verification_status         TEXT NOT NULL DEFAULT 'pending',
    po_verification_errors          JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Phase 8: durable link to the official GRN this staged record was
    -- posted into, and when. NULL means never posted. UNIQUE so a staged
    -- record can only ever point at one official GRN. Once set, this
    -- staged record becomes an immutable audit snapshot -- revalidation
    -- skips it (see grn_csv_staging.py).
    posted_grn_id                     BIGINT UNIQUE REFERENCES grn_receipts(grn_id),
    posted_at                          TIMESTAMPTZ,

    created_at                       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (batch_id, customer_id, external_grn_number)
);
CREATE INDEX idx_staged_grns_batch_id ON staged_grns(batch_id);
CREATE INDEX idx_staged_grns_customer_external_grn ON staged_grns(customer_id, external_grn_number);
CREATE INDEX idx_staged_grns_customer_external_po ON staged_grns(customer_id, external_po_number);
CREATE INDEX idx_staged_grns_official_po_id ON staged_grns(official_po_id);
CREATE INDEX idx_staged_grns_validation_status ON staged_grns(validation_status);
CREATE INDEX idx_staged_grns_po_verification_status ON staged_grns(po_verification_status);
CREATE INDEX idx_staged_grns_posted_grn_id ON staged_grns(posted_grn_id);

-- One row per normalized PHYSICAL receipt/lot line -- NOT necessarily one
-- raw CSV row (see staged_grn_line_source_rows and grn_csv_staging.py's
-- normalization algorithm). Deliberately no UNIQUE(staged_grn_id,
-- external_sku): the same SKU can legitimately appear as more than one
-- real lot within one GRN.
CREATE TABLE staged_grn_lines (
    staged_grn_line_id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    staged_grn_id               BIGINT NOT NULL REFERENCES staged_grns(staged_grn_id),

    external_sku                TEXT,
    external_sku_description    TEXT,
    -- Resolved via catalog.resolve_customer_sku(), never legacy
    -- products.sku_code. NULL + validation_status='blocked' if unmapped.
    product_id                  INTEGER REFERENCES master_products(product_id),

    brand_name                  TEXT,
    category                    TEXT,

    -- The quantity that will eventually reduce inventory -- never summed
    -- across raw rows that are really the same physical line represented
    -- twice (see the normalization algorithm).
    received_qty                NUMERIC CHECK (received_qty >= 0),

    -- Source rejection facts, preserved but NOT the source of truth for
    -- the PO-vs-GRN commitment discrepancy (that's ordered_qty -
    -- received_qty, computed fresh in get_grn_po_comparison()).
    dn_quantity                 NUMERIC CHECK (dn_quantity >= 0),
    dn_value                    NUMERIC,

    grn_line_value_without_tax  NUMERIC,
    grn_line_value_with_tax     NUMERIC,

    lot_mrp                     NUMERIC,
    lot_expiry_date             DATE,

    cgst_rate        NUMERIC,
    cgst_amount      NUMERIC,
    sgst_rate        NUMERIC,
    sgst_amount      NUMERIC,
    igst_rate        NUMERIC,
    igst_amount      NUMERIC,
    cess_rate        NUMERIC,
    cess_amount      NUMERIC,
    additional_cess  NUMERIC,
    total_tax        NUMERIC,
    total_amount     NUMERIC,

    validation_status           TEXT NOT NULL DEFAULT 'valid',
    validation_errors           JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Phase 8: durable link to the official grn_line_items row this
    -- staged line was posted into. NULL means never posted.
    posted_grn_line_item_id     INTEGER UNIQUE REFERENCES grn_line_items(id),

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_staged_grn_lines_staged_grn_id ON staged_grn_lines(staged_grn_id);
CREATE INDEX idx_staged_grn_lines_product_id ON staged_grn_lines(product_id);
CREATE INDEX idx_staged_grn_lines_external_sku ON staged_grn_lines(external_sku);
CREATE INDEX idx_staged_grn_lines_validation_status ON staged_grn_lines(validation_status);

-- Raw row <-> normalized line lineage. One normalized line can derive
-- from more than one raw row (the duplicated-DN-representation case);
-- UNIQUE(raw_row_id) ensures a given raw row is never claimed by more
-- than one normalized line.
CREATE TABLE staged_grn_line_source_rows (
    staged_grn_line_id BIGINT NOT NULL REFERENCES staged_grn_lines(staged_grn_line_id),
    raw_row_id          BIGINT NOT NULL REFERENCES grn_import_rows(row_id),
    PRIMARY KEY (staged_grn_line_id, raw_row_id),
    UNIQUE (raw_row_id)
);

-- Discrepancy/PR CSVs classify GRN shortfall loss movements that already
-- exist. They never create a second stock deduction.
CREATE TABLE discrepancy_import_batches (
    batch_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    source_filename TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (customer_id, file_sha256)
);

CREATE TABLE staged_discrepancy_lines (
    staged_line_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES discrepancy_import_batches(batch_id),
    source_row_number INTEGER NOT NULL,
    raw_data JSONB NOT NULL,
    pr_number TEXT,
    po_number TEXT,
    grn_number TEXT,
    external_sku TEXT,
    product_id INTEGER REFERENCES master_products(product_id),
    accepted_qty NUMERIC,
    rejected_qty NUMERIC,
    rejected_reason TEXT,
    official_grn_id BIGINT REFERENCES grn_receipts(grn_id),
    discrepancy_movement_id INTEGER REFERENCES inventory_movements(id),
    review_status TEXT NOT NULL,
    review_message TEXT,
    classified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (batch_id, source_row_number)
);
CREATE INDEX idx_staged_discrepancy_batch ON staged_discrepancy_lines(batch_id);
CREATE INDEX idx_staged_discrepancy_grn ON staged_discrepancy_lines(grn_number);

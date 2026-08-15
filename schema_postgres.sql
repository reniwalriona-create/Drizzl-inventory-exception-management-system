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
CREATE TABLE purchase_orders (
    po_number               TEXT PRIMARY KEY,
    customer_id              INTEGER REFERENCES customers(id),
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
    created_at                     TEXT DEFAULT CURRENT_TIMESTAMP::text
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
    total            REAL
);
CREATE INDEX idx_po_line_items_po_number ON po_line_items(po_number);

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

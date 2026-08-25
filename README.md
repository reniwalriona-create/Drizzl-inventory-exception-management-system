# Drizzl Inventory Management System

> An internal inventory ledger and B2B document-reconciliation system built for **Drizzl**, a probiotic soda brand — evolved over thirteen engineering phases from a single-customer parsing prototype into a canonical-identity, staging-then-posting inventory platform with a correction/audit trail.

**Stack:** Python · Flask · PostgreSQL · Werkzeug/Flask-Login · Flask-WTF
**Focus:** inventory-ledger design, B2B document reconciliation, canonical product identity, and the operational discipline (staging, atomic posting, void/supersede) that keeps a real business's numbers trustworthy.

## Current operator workflow (updated 2026-08-24)

1. Upload a PO CSV, review it, assign a Drizzl source warehouse, and post the ready POs. Exact duplicates are skipped; changed same-number POs require review; unknown customer SKUs remain blocked until manually mapped to an existing Master Product.
2. Upload a GRN CSV and post verified GRNs. The project enforces one GRN per PO, and the first posted GRN closes the entire PO commitment.
3. GRN posting removes the **full ordered PO quantity** from the source warehouse: received units are recorded as sale and any positive `ordered - received` difference is recorded as an unclassified loss. This ensures the PO is fully removed from Drizzl inventory.
4. Upload a discrepancy CSV only to classify the existing shortfall loss (damaged, expired, short delivery, etc.). It does **not** deduct stock again.
5. Manual movements use existing active Master Products only. Creating a product or mapping a new customer SKU remains a terminal/developer task.

The dashboard shortfall rate is calculated only from official POs with posted GRNs: `total positive (ordered - received) / total ordered`. Manual loss movements do not affect it.

Dashboard warning terminology:

- **Products currently below zero** is a live count of product/location balances that are negative now.
- **Unreviewed stock warnings** are saved incidents created when a movement pushed stock below zero. They remain until marked resolved, even if a later movement restores the balance.
- CSV validation and duplicate problems are handled on each import batch's review screen. The obsolete dashboard-level “Documents flagged for review” panel has been removed.

---

## What the system does

Two customer document formats flow through the system, both ending at the same place — a single inventory ledger:

```text
PO CSV  →  staged for review  →  Drizzl source warehouse assigned  →  canonical PO
                                                                            │
GRN CSV →  normalized + quarantined if it conflicts  →  official GRN  →  SALE movement
                                                                            │
                                                                   PO commitment closes
```

- **Purchase Orders** arrive as a CSV export. A PO never moves physical inventory by itself — it creates a **commitment**, reserving stock against a future delivery.
- **GRNs** represent what was actually received. Posting records received units as sale and records any positive ordered-minus-received shortfall as an unclassified loss, so the full PO quantity leaves the source warehouse exactly once.
- **Discrepancy CSVs** explain the cause of an already-recorded GRN shortfall. Posting one classifies that loss for reporting and does not change stock again.
- **Corrections never edit history.** A wrong PO source or a wrong GRN gets void/restore or void+replace treatment — the original row stays in the database, marked voided (and, for a replaced GRN, linked to its replacement via `supersedes_grn_id`), forever inspectable.

## Architecture concepts

**Raw → staged → official**, for both POs and GRNs. A CSV upload never touches the ledger directly — it lands in a staging table first (`staged_purchase_orders` / `staged_grns`), gets validated and normalized, and only a human's explicit "post" action turns it into an official `purchase_orders`/`grn_receipts` row with real inventory effect. Staging rows are never deleted, even after posting — they're the permanent audit trail of what the source file actually said before any normalization.

**Canonical Master Product identity.** `master_products` is Drizzl's own product catalog (7 real SKUs), addressed by `product_id`. `customer_product_skus` maps each customer's own external SKU string to a `product_id` — a customer's SKU is never treated as the product's true identity, because two customers' SKUs for the same physical product don't match, and nothing should have to guess.

**Internal IDs for canonical joins.** `po_id`, `grn_id`, and `product_id` are the real primary keys canonical code joins on — never a text field like `po_number`/`grn_number`/`sku_code`. Text identifiers are kept for exactly three things: matching a re-uploaded file back to its own record, on-screen display, and audit trail — never as a join key in canonical calculations. (The one deliberate exception: `grn_receipts.grn_number` *can* repeat across a correction chain — a superseded GRN and its replacement share the real-world GRN number — which is exactly why canonical code joins on `grn_id`, not the text, once that became possible.)

**The inventory ledger** (`inventory_movements`) is the single source of truth for stock — never a mutable `current_stock` column. Every physical event (production, transfer, sale, loss, opening balance) is one row; current stock at any location is `SUM(inflows) − SUM(outflows)`, computed fresh, every time.

**Quarantine instead of guessing.** A GRN whose source warehouse can't be resolved, whose PO doesn't match, or whose GRN number collides with an already-posted one is never silently accepted or silently rejected — it's flagged and shown to a human, with the specific reason, before it can affect inventory.

**Atomic, idempotent posting.** Posting a batch of staged records is all-or-nothing — if any one of them isn't ready, nothing in the batch is written. Re-posting an already-posted record is a safe no-op, not a duplicate.

## Important engineering decisions

- **Customer SKU ≠ product identity** — see "canonical Master Product identity" above. This was the single decision that shaped everything downstream of it.
- **`product_id`-based inventory, not SKU-string-based** — stock, commitments, manual movements, dashboard filters, damage reporting, and discrepancy calculations key on the Master Product. Customer SKUs and barcodes are references, never separate inventory pools.
- **Unknown customer SKUs block posting** — they never create a product or inventory row. Add the mapping manually, then use the PO batch's “Revalidate Master Product mappings” action.
- **Staging before ledger writes** — nothing from an uploaded file can touch physical inventory numbers until a human has reviewed it and explicitly posted it.
- **Void/supersede instead of delete** — a mistaken document, movement, or PO-source assignment is never removed; it's voided (with a required reason) and, for a GRN correction, explicitly linked to whatever replaced it. A superseded record can never be resurrected through a generic "restore" — only through another explicit correction.
- **Negative inventory is allowed, on purpose** — a GRN is a real document; if honoring it makes a balance go negative, that's surfaced as a flag for a human to investigate (usually a missing upstream movement), not silently blocked or hidden.

## Setup

**1. Python environment**

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

**2. PostgreSQL**

```bash
brew install postgresql@16
brew services start postgresql@16
/opt/homebrew/opt/postgresql@16/bin/createdb drizzl_inventory
```

**3. Environment variables**

Copy `.env.example` to `.env` and fill in values (or just export them in your shell). Local development doesn't strictly need this — `config.py` falls back to a local `drizzl_inventory` database and a clearly-labeled dev-only secret key whenever `APP_ENV` isn't `production`. Production **requires** `SECRET_KEY` and `DATABASE_URL` — the app refuses to start without them rather than falling back to anything insecure.

**4. Schema / migrations**

A fresh database: `./.venv/bin/python db.py` creates every table from `schema_postgres.sql` and seeds reference data (Scootsy as the one customer, Drizzl Demo Warehouse as the one location, the 7 Master Products + known SKU mappings) on first connection.

An existing database that predates a phase: apply every file in `migrations/` in numeric order —

```bash
for f in migrations/*.sql; do
  /opt/homebrew/opt/postgresql@16/bin/psql drizzl_inventory -f "$f"
done
```

Every migration is idempotent (safe to re-run). `schema_postgres.sql` and the full migration chain produce the same canonical schema — both are verified as part of `verify_all.py`.

**5. Initial user**

There's no self-registration. Create the first login with:

```bash
./.venv/bin/python create_user.py admin
```

It prompts for a password interactively (never as a command-line argument, so it never sits in shell history) and stores only a salted hash.

**6. Run the server**

```bash
./.venv/bin/python app.py            # dev server, port 5001 by default
```

For anything beyond local development, run behind gunicorn instead:

```bash
./.venv/bin/gunicorn -w 2 -b 0.0.0.0:5001 app:app
```

**7. Verify**

```bash
./.venv/bin/python verify_all.py
```

One command, one PASS/FAIL summary — runs every business-logic and security verification suite plus a read-only integrity audit of the live database. See "Verification" below for what each suite actually covers.

---

## Project structure

```text
app.py                  Flask routes: dashboard, upload, staging review, corrections, auth
config.py                Environment-driven configuration (secrets, database URL, debug)
db.py                    Postgres connection handling, schema bootstrap, seed data
create_user.py            CLI to create/reset a login (no self-registration UI)

ingest.py                 Ledger writes, void/restore, and dormant CLI-only legacy helpers
po_csv_staging.py         PO CSV → staged_purchase_orders (raw → normalized, never posts)
po_posting.py             staged_purchase_orders → official purchase_orders (atomic, idempotent)
grn_csv_staging.py        GRN CSV → staged_grns (normalization, duplicate/PO-verification checks)
grn_posting.py             staged_grns → official grn_receipts + SALE movements + commitment close;
                           also the GRN correction/replacement service
discrepancy_csv_staging.py discrepancy CSV → validates posted PO/GRN shortfalls and classifies
                           existing losses without another stock movement
catalog.py                Master Product / customer-SKU mapping lookups
reconcile.py               All read-side calculations: stock, commitment, discrepancy, lookup
validate.py                 Per-document-type sanity checks, logged as ingestion flags

po_parser.py / grn_parser.py / debit_note_parser.py    Dormant CLI/parser fixtures; not web routes
templates/ + static/        Flask UI (server-rendered, no JS framework)

migrations/                 001–014, applied in order, each idempotent
schema_postgres.sql         The current canonical schema (fresh-install target)

verify_*.py                  One suite per phase/subsystem (see below)
verify_all.py                 Runs all of them, one PASS/FAIL summary
verify_system_integrity.py     Read-only invariant audit of the live database
```

## Verification

Each mutating `verify_*.py` suite creates and drops its own disposable Postgres database, so automated tests never touch development data. `verify_system_integrity.py` is the one intentional exception: it is strictly read-only and audits the selected live database. `verify_canonical_manual_movements.py` proves that manual entries cannot bypass Master Products.

```bash
./.venv/bin/python verify_all.py          # everything, one summary
./.venv/bin/python verify_system_integrity.py   # read-only audit of the live database
```

`verify_po_parser.py` / `verify_grn_parser.py` / `verify_debit_note_parser.py` need real sample PDFs that live outside version control (`private_data/`, gitignored) — run them manually after touching a parser, with the samples present.

## Operations

**Backups.** Before any production migration, take a real backup:

```bash
pg_dump -Fc drizzl_inventory > drizzl_inventory_$(date +%Y%m%d).dump
```

Restore into a fresh database:

```bash
createdb drizzl_inventory_restored
pg_restore -d drizzl_inventory_restored drizzl_inventory_20260101.dump
```

There's no automated backup service in this repository — this is a manual, documented step, not application code.

**Health check.** `GET /health` (no auth required) returns `{"status": "ok"}` if the process is up and can reach the database, `{"status": "unavailable"}` (503) otherwise. It reveals nothing about schema or configuration — safe to point a load balancer or uptime monitor at.

**Logs.** The app logs to stderr (timestamp, level, message). At the prototype owner's request, unexpected error types/messages are also shown to signed-in operators with instructions to contact the developer; full tracebacks remain server-side only.

## Current limitations

This is a real, working internal tool, not a finished product. Known gaps, deliberately not addressed yet:

- single shared operator role — no per-permission access control
- no automated backup scheduling (documented manual process only, see above)
- not deployed anywhere — everything above describes running it locally or on a server you provision yourself
- Debit Notes and appointment-slot CSVs are CLI-only (`ingest.py`), not wired into the web UI
- two customers sharing a PO/GRN number is still unsupported (`purchase_orders.po_number`/legacy `grn_number` global-uniqueness scaffolding from early phases, revisit when a second customer's numbering scheme actually collides)

## What I learned building this

I started this project as a recent Information Science graduate, not a trained software engineer, building it iteratively with AI-assisted development while deliberately focusing on understanding the architecture and the business logic rather than treating generated code as a finished product. The most valuable parts weren't the CRUD scaffolding — they were the modeling decisions that came from real operational edge cases: separating physical inventory from commitments, using an event ledger instead of a mutable stock field, recognizing that a customer's SKU string isn't the system's true product identity before a second customer's documents ever arrived, and designing corrections so the system stays explainable (void + supersede, never silently overwrite) rather than just producing the right total. Phases 9–12 were as much about *removing* things safely — a whole document workflow, several ambiguous joins, an insecure default — as they were about adding new capability, which turned out to be its own kind of engineering discipline.

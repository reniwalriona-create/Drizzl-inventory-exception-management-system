"""
Loads parsed documents into inventory.db, and keeps the inventory_movements
ledger in sync with them automatically. GRNs arrive as individual PDFs, one
per delivery -- there is no bulk CSV import path.

Usage:
    python3 ingest.py po <po_pdf_path>
    python3 ingest.py grn <grn_pdf_path>
    python3 ingest.py appointments-csv <appointments_csv_path>
    python3 ingest.py debit-note <debit_note_pdf_path>
"""
import csv
import re
import sys
from pathlib import Path

from db import get_connection
from reconcile import current_balance, resolve_grn_source_location
from po_parser import parse_po_pdf
from grn_parser import parse_grn_pdf
from debit_note_parser import parse_debit_note_pdf
from validate import validate_po, validate_grn, validate_debit_note, record_flags

# Every document ingested so far is from this one customer. When a
# second customer's documents show up, pass a real customer_name through
# instead of relying on this default. (There used to be a DEFAULT_LOCATION
# here too, used to guess which Drizzl location a GRN's sale came from --
# removed once source locations became a real, explicit per-PO/GRN field
# instead of an assumption; see resolve_grn_source_location() in
# reconcile.py.)
DEFAULT_CUSTOMER = "Scootsy Logistics Private Limited"


def _normalize_sku(code):
    """Discrepancy Notes print SKU codes with a location prefix
    ("106-DEMO-SKU-006"); everywhere else it's the bare code ("DEMO-SKU-006").
    Strip the prefix so both refer to the same product row."""
    if not code:
        return code
    return re.sub(r'^\d+-', '', code.strip())


def _ensure_customer(conn, name):
    if not name:
        return None
    conn.execute("INSERT INTO customers (name) VALUES (?) ON CONFLICT (name) DO NOTHING", (name,))
    return conn.execute(
        "SELECT id FROM customers WHERE name = ?", (name,)
    ).fetchone()["id"]


def _ensure_location(conn, name, loc_type="own_facility"):
    if not name:
        return None
    conn.execute(
        "INSERT INTO locations (name, type) VALUES (?, ?) ON CONFLICT (name) DO NOTHING", (name, loc_type)
    )
    return conn.execute(
        "SELECT id FROM locations WHERE name = ?", (name,)
    ).fetchone()["id"]


def _ensure_product(conn, sku_code, sku_desc=None):
    sku_code = _normalize_sku(sku_code)
    if not sku_code:
        return None
    conn.execute(
        "INSERT INTO products (sku_code, sku_desc) VALUES (?, ?) ON CONFLICT (sku_code) DO NOTHING",
        (sku_code, sku_desc),
    )
    if sku_desc:
        conn.execute(
            "UPDATE products SET sku_desc = ? WHERE sku_code = ? AND sku_desc IS NULL",
            (sku_desc, sku_code),
        )
    return sku_code


def _ensure_po_stub(conn, po_number, customer_id=None):
    if not po_number:
        return
    conn.execute(
        "INSERT INTO purchase_orders (po_number, customer_id) VALUES (?, ?) ON CONFLICT (po_number) DO NOTHING",
        (po_number, customer_id),
    )


def clear_movements_for_reference(conn, reference_type, reference_id):
    conn.execute(
        "DELETE FROM inventory_movements WHERE reference_type = ? AND reference_id = ?",
        (reference_type, reference_id),
    )


def void_po(conn, po_number, reason):
    """A PO never created any ledger movement ("an order isn't a stock
    event"), so voiding one is purely a paperwork flag -- nothing else
    to touch. Unlike a hard delete, this never fails even if a GRN
    references this po_number: nothing is being removed, so there's no
    foreign-key ordering to worry about."""
    row = conn.execute("SELECT po_number FROM purchase_orders WHERE po_number = ?", (po_number,)).fetchone()
    if row is None:
        raise ValueError("PO not found.")
    conn.execute(
        "UPDATE purchase_orders SET voided = 1, void_reason = ?, voided_at = CURRENT_TIMESTAMP WHERE po_number = ?",
        (reason, po_number),
    )


def void_grn(conn, grn_number, reason):
    """Voiding a GRN also voids the sale movement(s) it auto-created --
    otherwise those units would keep counting as sold in every
    calculation even though the GRN that supposedly caused it is now
    void. Nothing is deleted -- the GRN, its line items, and its
    movements all stay in the database, just excluded from every
    calculation (see reconcile.py's voided=0 filters) until restored
    (unvoid_grn(), reverses this exactly).

    Phase 10: also resolves any still-open inventory_flags tied to the
    movement(s) just voided -- an unresolved negative-inventory flag
    must not keep pretending a now-dead movement is an active problem.
    The flag row itself is never deleted (see reconcile.
    unresolved_inventory_flags()); this is the same 'resolved=1, keep
    for audit' treatment a human clicking Resolve already applies, just
    triggered automatically by the movement dying underneath it. This is
    what makes replace_posted_grn() (grn_posting.py) safe to build on
    top of this function without any extra flag-handling of its own.

    Phase 10: targets ONLY the currently ACTIVE (voided = 0) row for
    this grn_number. Since a superseded predecessor can share the same
    grn_number with its active replacement (see grn_receipts.
    supersedes_grn_id / the partial unique index on grn_number), a bare
    grn_number match with no voided filter could otherwise silently
    rewrite an already-voided historical row's void_reason/voided_at --
    exactly the kind of "quietly overwrite old history" this whole
    void/restore architecture exists to prevent."""
    row = conn.execute(
        "SELECT grn_number FROM grn_receipts WHERE grn_number = ? AND voided = 0", (grn_number,)
    ).fetchone()
    if row is None:
        raise ValueError("GRN not found (or has no currently active record under this number).")
    conn.execute(
        "UPDATE grn_receipts SET voided = 1, void_reason = ?, voided_at = CURRENT_TIMESTAMP "
        "WHERE grn_number = ? AND voided = 0",
        (reason, grn_number),
    )
    voided_movement_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM inventory_movements WHERE reference_type = 'grn' AND reference_id = ? AND voided = 0",
            (grn_number,),
        ).fetchall()
    ]
    conn.execute(
        "UPDATE inventory_movements SET voided = 1, void_reason = ?, voided_at = CURRENT_TIMESTAMP "
        "WHERE reference_type = 'grn' AND reference_id = ? AND voided = 0",
        (reason, grn_number),
    )
    if voided_movement_ids:
        placeholders = ",".join(["?"] * len(voided_movement_ids))
        conn.execute(
            f"UPDATE inventory_flags SET resolved = 1 WHERE resolved = 0 AND movement_id IN ({placeholders})",
            tuple(voided_movement_ids),
        )


def void_movement(conn, movement_id, reason):
    """Only a manually-entered movement (reference_type='manual') can be
    voided this way. A GRN/PO-generated movement is kept in sync with its
    parent document by upsert_grn() etc. -- voiding
    it directly here would desync it from that document (the document
    would still claim the movement is "live" while the movement itself
    says otherwise). Void the document instead (void_grn() etc.), which
    voids its movements properly. Returns the voided row (as a dict) so
    the caller can log what happened without a second query."""
    row = conn.execute(
        "SELECT sku_code, movement_type, quantity, reference_type FROM inventory_movements WHERE id = ?",
        (movement_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Movement not found.")
    if row["reference_type"] != "manual":
        raise ValueError(
            f"This {row['movement_type']} movement came from a {row['reference_type']} document, not a manual "
            "entry -- void the document instead so its line items and the ledger stay in sync."
        )
    conn.execute(
        "UPDATE inventory_movements SET voided = 1, void_reason = ?, voided_at = CURRENT_TIMESTAMP WHERE id = ?",
        (reason, movement_id),
    )
    return dict(row)


def unvoid_po(conn, po_number):
    """Restores a mistakenly-voided PO -- see reconcile.voided_entries(),
    the review panel this is meant to be reached from."""
    conn.execute(
        "UPDATE purchase_orders SET voided = 0, void_reason = NULL, voided_at = NULL WHERE po_number = ?",
        (po_number,),
    )


def unvoid_grn(conn, grn_id):
    """Restores a mistakenly-voided GRN, and un-voids its sale
    movement(s) along with it -- the exact reverse of void_grn(). Safe
    to blanket-restore every movement referencing this GRN: a
    GRN-sourced movement can only ever have been voided as part of
    voiding this same GRN (void_movement() refuses to void a non-manual
    movement directly), so there's no risk of un-voiding something that
    was independently voided for a different reason.

    Phase 10: takes grn_id (the real, unambiguous identity), NOT
    grn_number -- a voided GRN's grn_number is no longer guaranteed
    unique on its own (a superseded predecessor and its active
    replacement, or a whole chain of corrections, can share one; see
    grn_receipts.supersedes_grn_id / the partial unique index on
    grn_number). Two required safety properties:
    - Refuses to restore a GRN whose PO already has a DIFFERENT active
      official GRN right now (checked directly against purchase_orders/
      grn_receipts current state, not by walking the supersedes_grn_id
      chain -- robust to any chain length: A superseded by B superseded
      by C, restoring A must still be refused even though nothing
      directly supersedes A anymore, since C is what's actually active).
      Restoring it would create two active receipt outcomes for the
      same PO, silently double-counting inventory. Only applies to
      canonical (po_id-linked) GRNs -- a legacy PDF GRN is never part of
      a supersede chain (replace_posted_grn() only ever touches
      canonical GRNs), so this check is a no-op for it.
    - After restoring, verifies the invariant this whole function exists
      to preserve -- active official GRN <-> its canonical
      (product_id-identified) received_qty>0 SALE movements are active,
      linked via source_grn_line_item_id -> grn_line_items.grn_id (never
      grn_number text, which a superseded ancestor/descendant can
      share). A mismatch means movement state has drifted out of sync
      with the GRN in some way this function doesn't understand; it
      raises rather than leaving the ledger silently inconsistent (this
      never triggers in the normal void_grn()/unvoid_grn() round trip,
      since both always move the whole movement set together)."""
    row = conn.execute("SELECT grn_id, grn_number, po_id FROM grn_receipts WHERE grn_id = ?", (grn_id,)).fetchone()
    if row is None:
        raise ValueError("GRN not found.")

    if row["po_id"] is not None:
        active = conn.execute(
            "SELECT grn_id, grn_number FROM grn_receipts WHERE po_id = ? AND voided = 0", (row["po_id"],)
        ).fetchone()
        if active is not None and active["grn_id"] != grn_id:
            raise ValueError(
                f"GRN {row['grn_number']} (grn_id {grn_id}) can't be restored -- its PO already has a "
                f"different active official GRN right now ({active['grn_number']}, grn_id {active['grn_id']}). "
                "Restoring this one would create two active receipt outcomes for the same PO. If a correction "
                "in this chain was itself wrong, correct/replace the currently active GRN instead."
            )

    conn.execute(
        "UPDATE grn_receipts SET voided = 0, void_reason = NULL, voided_at = NULL WHERE grn_id = ?",
        (grn_id,),
    )

    if row["po_id"] is not None:
        # Canonical -- disambiguate via source_grn_line_item_id ->
        # grn_line_items.grn_id, never plain reference_id text, which a
        # superseded ancestor/descendant sharing this grn_number could
        # also match.
        conn.execute(
            """
            UPDATE inventory_movements SET voided = 0, void_reason = NULL, voided_at = NULL
            WHERE reference_type = 'grn' AND source_grn_line_item_id IN (
                SELECT id FROM grn_line_items WHERE grn_id = ?
            )
            """,
            (grn_id,),
        )
    else:
        # Legacy PDF GRN -- never part of a supersede chain, so its
        # grn_number is guaranteed not shared with any other row; plain
        # reference_id text matching is safe exactly as it always was.
        conn.execute(
            "UPDATE inventory_movements SET voided = 0, void_reason = NULL, voided_at = NULL "
            "WHERE reference_type = 'grn' AND reference_id = ?",
            (row["grn_number"],),
        )

    unlinked = conn.execute(
        """
        SELECT gli.id FROM grn_line_items gli
        WHERE gli.grn_id = ? AND gli.product_id IS NOT NULL
          AND gli.received_qty IS NOT NULL AND gli.received_qty > 0
          AND NOT EXISTS (
              SELECT 1 FROM inventory_movements m
              WHERE m.source_grn_line_item_id = gli.id AND m.voided = 0
          )
        """,
        (grn_id,),
    ).fetchall()
    if unlinked:
        raise ValueError(
            f"Integrity error restoring GRN {row['grn_number']} (grn_id {grn_id}): line item(s) "
            f"{[r['id'] for r in unlinked]} have no active SALE movement after restore -- refusing to leave "
            "the ledger silently inconsistent."
        )


def unvoid_movement(conn, movement_id):
    row = conn.execute(
        "SELECT sku_code, movement_type, quantity FROM inventory_movements WHERE id = ?",
        (movement_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Movement not found.")
    conn.execute(
        "UPDATE inventory_movements SET voided = 0, void_reason = NULL, voided_at = NULL WHERE id = ?",
        (movement_id,),
    )
    return dict(row)


def assign_po_source_location(conn, po_number, location_name, location_type="own_facility"):
    """Sets which Drizzl location a PO is expected to be fulfilled from
    -- separate from facility_name (Scootsy's own receiving warehouse),
    never inferred automatically (see schema.sql's purchase_orders.
    source_location_id). This is what unlocks the Committed-inventory
    calculation for this PO's lines (reconcile.committed_quantity()) and
    lets any of its GRNs create their sale movement -- so after setting
    it, back-fill any GRN sales that were pending on this PO having no
    source location yet.

    Phase 10: FIRST-TIME assignment only. Once a source is already set,
    this refuses to silently change it to a different location -- see
    correct_po_source_location() for the explicit, audited, reason-
    required way to change an already-assigned source. (Re-submitting
    the SAME location is a harmless no-op, not an error, so a UI
    accidentally resubmitting an unchanged form doesn't break.)"""
    location_id = _ensure_location(conn, location_name, location_type)
    row = conn.execute(
        "SELECT po_number, source_location_id FROM purchase_orders WHERE po_number = ?", (po_number,)
    ).fetchone()
    if row is None:
        raise ValueError("PO not found.")
    if row["source_location_id"] is not None and row["source_location_id"] != location_id:
        raise ValueError(
            f"PO {po_number} already has a source warehouse assigned -- use the Correct Source Warehouse "
            "workflow to change it, so the change is audited with a reason."
        )
    conn.execute(
        "UPDATE purchase_orders SET source_location_id = ? WHERE po_number = ?",
        (location_id, po_number),
    )
    for grn in conn.execute("SELECT grn_number FROM grn_receipts WHERE po_number = ? AND voided = 0", (po_number,)).fetchall():
        _create_pending_grn_sales(conn, grn["grn_number"])


def correct_po_source_location(conn, po_number, new_location_name, reason, location_type="own_facility"):
    """Explicit, audited correction of an ALREADY-assigned PO source
    warehouse (Phase 10) -- the only way to change source_location_id
    once assign_po_source_location() has set it once. See
    PROJECT_HANDOFF.md.

    Blocked entirely if the PO already has an active (non-voided)
    official GRN, whether posted via the legacy PDF path (grn_receipts.
    po_number) or the canonical CSV path (grn_receipts.po_id) -- that
    GRN's SALE movement(s) were recorded from the OLD warehouse, and
    correcting the PO's source alone would not move that history. The
    GRN itself must be corrected/replaced (grn_posting.
    replace_posted_grn()) instead, which re-posts the sale from
    wherever the PO's source points to at that time. Never silently
    moves historical inventory from one warehouse to another.

    Every correction is logged to po_source_corrections (old source, new
    source, reason, timestamp) -- never a silent overwrite. A reason is
    required."""
    if not reason or not reason.strip():
        raise ValueError("Correcting the source warehouse needs a reason.")

    po = conn.execute(
        "SELECT po_id, po_number, source_location_id, voided FROM purchase_orders WHERE po_number = ?",
        (po_number,),
    ).fetchone()
    if po is None:
        raise ValueError("PO not found.")
    if po["voided"]:
        raise ValueError("This PO is voided -- restore it first if its source warehouse needs correcting.")

    active_grn = conn.execute(
        "SELECT grn_number FROM grn_receipts WHERE (po_id = ? OR po_number = ?) AND voided = 0",
        (po["po_id"], po_number),
    ).fetchone()
    if active_grn is not None:
        raise ValueError(
            f"PO {po_number} already has an active official GRN ({active_grn['grn_number']}) -- its sale "
            "movement(s) were recorded from the current source warehouse. Correct or replace that GRN "
            "instead of changing the PO's source directly."
        )

    new_location_id = _ensure_location(conn, new_location_name, location_type)
    old_location_id = po["source_location_id"]
    if old_location_id == new_location_id:
        raise ValueError("The new source warehouse is the same as the current one.")

    conn.execute(
        "INSERT INTO po_source_corrections (po_id, old_source_location_id, new_source_location_id, reason) "
        "VALUES (?, ?, ?, ?)",
        (po["po_id"], old_location_id, new_location_id, reason),
    )
    conn.execute(
        "UPDATE purchase_orders SET source_location_id = ? WHERE po_number = ?",
        (new_location_id, po_number),
    )


def assign_grn_source_location(conn, grn_number, location_name, location_type="own_facility"):
    """Sets a GRN's own source location directly -- the fallback used
    when the GRN has no po_number, or its PO hasn't been allocated a
    source location either (see reconcile.resolve_grn_source_location()).
    Also creates whichever of this GRN's sale movements were pending on
    a source location existing."""
    location_id = _ensure_location(conn, location_name, location_type)
    row = conn.execute(
        "SELECT grn_number FROM grn_receipts WHERE grn_number = ? AND voided = 0", (grn_number,)
    ).fetchone()
    if row is None:
        raise ValueError("GRN not found (or has no currently active record under this number).")
    conn.execute(
        "UPDATE grn_receipts SET source_location_id = ? WHERE grn_number = ? AND voided = 0",
        (location_id, grn_number),
    )
    _create_pending_grn_sales(conn, grn_number)


def _create_pending_grn_sales(conn, grn_number):
    """Creates the 'sale' movement for any of this GRN's line items that
    couldn't be created yet because no Drizzl source location was
    resolvable at the time (see upsert_grn()). Safe to call any time a
    source location might have just become available (assign_po_
    source_location(), assign_grn_source_location()) -- a line item that
    already has its sale movement is skipped, so this never double-
    creates one. Also resolves the "needs source allocation" ingestion
    flag for this GRN once every line is caught up."""
    source_location = resolve_grn_source_location(conn, grn_number)
    if not source_location:
        return

    # AND voided = 0 -- combined with the partial unique index on
    # grn_receipts(grn_number) WHERE voided = 0 (Phase 10), at most one
    # row can ever match this, even if a superseded predecessor shares
    # the same grn_number.
    grn = conn.execute(
        "SELECT grn_id, grn_date FROM grn_receipts WHERE grn_number = ? AND voided = 0", (grn_number,)
    ).fetchone()
    if not grn:
        return

    existing_skus = {
        r["sku_code"] for r in conn.execute(
            "SELECT DISTINCT sku_code FROM inventory_movements WHERE reference_type = 'grn' AND reference_id = ?",
            (grn_number,),
        ).fetchall()
    }

    # Phase 10: filtered by grn_id, not grn_number -- a superseded
    # predecessor can share this grn_number, and its line items must
    # never leak into this active GRN's pending-sale computation.
    pending_lines = conn.execute(
        "SELECT sku_code, received_qty FROM grn_line_items WHERE grn_id = ? AND received_qty IS NOT NULL AND received_qty != 0",
        (grn["grn_id"],),
    ).fetchall()

    for item in pending_lines:
        if item["sku_code"] in existing_skus:
            continue
        _record_grn_sale(conn, grn_number, grn["grn_date"], item["sku_code"], item["received_qty"], source_location)

    conn.execute(
        "UPDATE ingestion_flags SET resolved = 1 "
        "WHERE document_type = 'grn' AND document_id = ? AND issue LIKE 'Needs a Drizzl source location%'",
        (grn_number,),
    )


def _record_grn_sale(conn, grn_number, grn_date, sku_code, received_qty, source_location):
    """The one place a GRN's sale movement actually gets created --
    called both from upsert_grn() (fresh upload, source already
    resolvable) and _create_pending_grn_sales() (backfilled after a
    location gets assigned later). A GRN is a real document -- never
    blocked for going negative, unlike a manual movement (see app.py's
    new_movement()). If it would, the movement still gets recorded
    (Scootsy really did receive these units), but it's flagged: a
    negative result here almost always means an earlier production/
    transfer/opening_balance entry is missing, not that this GRN is
    wrong."""
    available_before = current_balance(conn, source_location, sku_code)
    resulting_balance = available_before - received_qty
    movement_id = record_movement(
        conn, movement_date=grn_date, sku_code=sku_code,
        movement_type="sale", quantity=received_qty,
        location_from=source_location, reason="sold_to_customer",
        reference_type="grn", reference_id=grn_number,
    )
    if resulting_balance < 0:
        record_inventory_flag(
            conn, sku_code=sku_code, location_name=source_location,
            source="grn", available_before=available_before,
            requested_qty=received_qty, resulting_balance=resulting_balance,
            movement_id=movement_id, reference_id=grn_number,
        )


def _canonical_sku_code(conn, product_id):
    """The one authoritative compatibility sku_code for a canonical
    movement/flag -- always derived fresh from master_products.barcode,
    never trusted from a caller-supplied value. This is what stops a
    future bug like product_id=Passionfruit + sku_code=<some customer's
    SKU> from ever creating an inconsistent inventory identity (Phase 8,
    per PROJECT_HANDOFF.md)."""
    row = conn.execute("SELECT barcode FROM master_products WHERE product_id = ?", (product_id,)).fetchone()
    if row is None:
        raise ValueError(f"Master Product {product_id} does not exist.")
    return row["barcode"]


def record_movement(conn, movement_date, sku_code, movement_type, quantity,
                     location_from=None, location_to=None, reason=None,
                     reference_type=None, reference_id=None, notes=None,
                     recorded_by=None, sku_desc=None,
                     location_from_type="own_facility", location_to_type="own_facility",
                     negative_override_reason=None, commitment_override_reason=None,
                     product_id=None, source_grn_line_item_id=None):
    """product_id=None (every pre-Phase-8 caller) is the legacy path,
    completely unchanged: sku_code is upserted into the legacy `products`
    table via _ensure_product(). product_id=<a real Master Product> is
    the Phase 8 canonical path: sku_code is IGNORED and re-derived fresh
    from master_products.barcode (see _canonical_sku_code()) -- the
    caller's sku_code argument is never trusted, so it's safe to pass
    None for it on a canonical call. _ensure_product()/legacy `products`
    are never touched on this path."""
    if product_id is not None:
        sku_code = _canonical_sku_code(conn, product_id)
    else:
        sku_code = _ensure_product(conn, sku_code, sku_desc)
    from_id = _ensure_location(conn, location_from, location_from_type) if location_from else None
    to_id = _ensure_location(conn, location_to, location_to_type) if location_to else None
    cur = conn.execute(
        """
        INSERT INTO inventory_movements
            (movement_date, sku_code, movement_type, quantity,
             location_from_id, location_to_id, reason, reference_type,
             reference_id, notes, recorded_by, negative_override_reason,
             commitment_override_reason, product_id, source_grn_line_item_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (movement_date, sku_code, movement_type, quantity, from_id, to_id,
         reason, reference_type, reference_id, notes, recorded_by,
         negative_override_reason, commitment_override_reason,
         product_id, source_grn_line_item_id),
    )
    return cur.fetchone()["id"]


def record_inventory_flag(conn, sku_code, location_name, source, available_before,
                           requested_qty, resulting_balance, movement_id=None,
                           reference_id=None, reason=None, product_id=None):
    """Logs a negative-inventory incident -- either a human's explicit
    override on a manual transfer/sale/loss, or a real GRN's sale
    movement pushing its source location below zero. See inventory_flags
    in schema.sql; surfaced on the dashboard by
    reconcile.unresolved_inventory_flags(). product_id=<a real Master
    Product> (Phase 8 canonical path) re-derives sku_code from
    master_products.barcode the same authoritative way record_movement()
    does -- never trusts a caller-supplied sku_code alongside a
    product_id."""
    if product_id is not None:
        sku_code = _canonical_sku_code(conn, product_id)
    conn.execute(
        """
        INSERT INTO inventory_flags
            (movement_id, sku_code, location_name, source, reference_id,
             available_before, requested_qty, resulting_balance, reason, product_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (movement_id, sku_code, location_name, source, reference_id,
         available_before, requested_qty, resulting_balance, reason, product_id),
    )


def upsert_po(conn, parsed, source_file=None, customer_name=DEFAULT_CUSTOMER):
    """Re-uploading a po_number that was voided automatically un-voids it
    -- re-uploading a corrected file IS the fix, so it shouldn't also
    require a separate manual Restore click. See void_po() in this file."""
    po_number = parsed["po_number"]
    if not po_number:
        raise ValueError("Parsed PO has no po_number, refusing to store it")
    record_flags(conn, "po", po_number, validate_po(parsed), source_file)
    customer_id = _ensure_customer(conn, customer_name)

    conn.execute(
        """
        INSERT INTO purchase_orders
            (po_number, customer_id, po_date, po_release_date, payment_terms,
             expected_delivery_date, po_expiry_date, vendor_name,
             vendor_gstin, facility_name, grand_total, source_file)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(po_number) DO UPDATE SET
            customer_id=excluded.customer_id,
            po_date=excluded.po_date,
            po_release_date=excluded.po_release_date,
            payment_terms=excluded.payment_terms,
            expected_delivery_date=excluded.expected_delivery_date,
            po_expiry_date=excluded.po_expiry_date,
            vendor_name=excluded.vendor_name,
            vendor_gstin=excluded.vendor_gstin,
            facility_name=excluded.facility_name,
            grand_total=excluded.grand_total,
            source_file=excluded.source_file,
            voided=0, void_reason=NULL, voided_at=NULL
        """,
        (
            po_number, customer_id, parsed.get("po_date"), parsed.get("po_release_date"),
            parsed.get("payment_terms"), parsed.get("expected_delivery_date"),
            parsed.get("po_expiry_date"), parsed.get("vendor_name"),
            parsed.get("vendor_gstin"), parsed.get("facility_name"),
            parsed.get("grand_total"), source_file,
        ),
    )

    conn.execute("DELETE FROM po_line_items WHERE po_number = ?", (po_number,))
    for item in parsed.get("line_items", []):
        _ensure_product(conn, item.get("item_code"), item.get("item_desc"))
        conn.execute(
            """
            INSERT INTO po_line_items
                (po_number, sno, item_code, item_desc, hsn_code, qty, mrp,
                 unit_base_cost, taxable_value, cgst_rate, cgst_amt,
                 sgst_rate, sgst_amt, igst_rate, igst_amt, cess_rate,
                 cess_amt, add_cess, total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                po_number, item.get("sno"), item.get("item_code"),
                item.get("item_desc"), item.get("hsn_code"), item.get("qty"),
                item.get("mrp"), item.get("unit_base_cost"),
                item.get("taxable_value"), item.get("cgst_rate"),
                item.get("cgst_amt"), item.get("sgst_rate"),
                item.get("sgst_amt"), item.get("igst_rate"),
                item.get("igst_amt"), item.get("cess_rate"),
                item.get("cess_amt"), item.get("add_cess"), item.get("total"),
            ),
        )
    conn.commit()
    return po_number


def upsert_grn(conn, parsed, source="pdf", source_file=None, customer_name=DEFAULT_CUSTOMER):
    """Re-uploading a grn_number that was voided automatically un-voids
    it, same reasoning as upsert_po(). Its movements don't need a
    separate reset for this -- clear_movements_for_reference() below
    deletes the old (possibly voided) ones and the fresh INSERT creates
    brand new rows, which default to voided=0 already.

    Phase 10: the ON CONFLICT arbiter is (grn_number) WHERE voided = 0
    -- matching grn_receipts_active_grn_number_key, the partial unique
    index that replaced the old blanket-unique grn_number column. If
    the only existing row for this grn_number is already voided (e.g.
    it was superseded by a corrected replacement via grn_posting.
    replace_posted_grn()), this INSERTs a fresh row instead of resurrecting/
    mutating the voided one -- consistent with this whole phase's rule of
    never editing an old record in place. If an ACTIVE row already
    exists, this updates it in place exactly as before Phase 10."""
    grn_number = parsed["grn_number"]
    if not grn_number:
        raise ValueError("Parsed GRN has no grn_number, refusing to store it")
    record_flags(conn, "grn", grn_number, validate_grn(parsed), source_file)

    customer_id = _ensure_customer(conn, customer_name)
    _ensure_po_stub(conn, parsed.get("po_number"), customer_id)

    grn_row = conn.execute(
        """
        INSERT INTO grn_receipts
            (grn_number, po_number, customer_id, inbound_no, grn_date, create_date,
             invoice_no, invoice_date, challan_no, challan_date,
             vendor_name, facility_name, source, source_file)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(grn_number) WHERE voided = 0 DO UPDATE SET
            po_number=excluded.po_number,
            customer_id=excluded.customer_id,
            inbound_no=excluded.inbound_no,
            grn_date=excluded.grn_date,
            create_date=excluded.create_date,
            invoice_no=excluded.invoice_no,
            invoice_date=excluded.invoice_date,
            challan_no=excluded.challan_no,
            challan_date=excluded.challan_date,
            vendor_name=excluded.vendor_name,
            facility_name=excluded.facility_name,
            source=excluded.source,
            source_file=excluded.source_file,
            voided=0, void_reason=NULL, voided_at=NULL
        RETURNING grn_id
        """,
        (
            grn_number, parsed.get("po_number"), customer_id, parsed.get("inbound_no"),
            parsed.get("grn_date"), parsed.get("create_date"),
            parsed.get("invoice_no"), parsed.get("invoice_date"),
            parsed.get("challan_no"), parsed.get("challan_date"),
            parsed.get("vendor_name"), parsed.get("facility_name"), source, source_file,
        ),
    ).fetchone()
    grn_id = grn_row["grn_id"]

    # Phase 10: scoped by grn_id, not grn_number text -- a superseded
    # predecessor sharing this grn_number must never have its history
    # touched by a re-upload targeting the active row.
    conn.execute("DELETE FROM grn_line_items WHERE grn_id = ?", (grn_id,))
    clear_movements_for_reference(conn, "grn", grn_number)

    for item in parsed.get("line_items", []):
        sku_code = _ensure_product(conn, item.get("sku_code"), item.get("sku_desc"))
        conn.execute(
            """
            INSERT INTO grn_line_items
                (grn_number, grn_id, sku_code, sku_desc, lot_no, lot_mrp,
                 expected_qty, received_qty, unit_price, taxable_value,
                 cgst_rate, cgst_amt, sgst_rate, sgst_amt, igst_rate,
                 igst_amt, cess_rate, cess_amt, add_cess, total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                grn_number, grn_id, sku_code, item.get("sku_desc"),
                item.get("lot_no"), item.get("lot_mrp"), item.get("expected_qty"),
                item.get("received_qty"), item.get("unit_price"),
                item.get("taxable_value"), item.get("cgst_rate"),
                item.get("cgst_amt"), item.get("sgst_rate"),
                item.get("sgst_amt"), item.get("igst_rate"),
                item.get("igst_amt"), item.get("cess_rate"),
                item.get("cess_amt"), item.get("add_cess"), item.get("total"),
            ),
        )
    # The quantity actually counted in at the warehouse is what's sold --
    # NOT expected_qty. If fewer arrived than expected, that gap is a
    # discrepancy to investigate (see reconcile.py's canonical PO-vs-GRN
    # comparison), not an automatic loss -- we don't yet know why the
    # units are missing.
    #
    # Sale movements are created in this second pass (after every line item
    # is stored) so the source-location resolution below only has to run
    # once per GRN, not once per line. Which Drizzl location the sale comes
    # from is never guessed/defaulted to Bangalore -- it's the GRN's own
    # source_location_id if set, else its linked PO's, else nothing (see
    # reconcile.resolve_grn_source_location()). If neither is set yet, the
    # sale movement is deliberately NOT created; the GRN and its line items
    # are still stored, but flagged as needing a source location assigned
    # (via /lookup or the dashboard) before it can be treated as sold from
    # anywhere. Once assigned, _create_pending_grn_sales() creates it.
    source_location = resolve_grn_source_location(conn, grn_number)
    unresolved_lines = []
    for item in parsed.get("line_items", []):
        received_qty = item.get("received_qty")
        if not received_qty:
            continue
        sku_code = _normalize_sku(item.get("sku_code"))
        if source_location:
            _record_grn_sale(conn, grn_number, parsed.get("grn_date"), sku_code, received_qty, source_location)
        else:
            unresolved_lines.append(f"SKU {sku_code}, {received_qty:.0f} units")

    if unresolved_lines:
        record_flags(
            conn, "grn", grn_number,
            [f"Needs a Drizzl source location before its sale movement can be created ({line})" for line in unresolved_lines],
            source_file,
        )

    conn.commit()
    return grn_number


def upsert_debit_note(conn, parsed, source_file=None, customer_name=DEFAULT_CUSTOMER):
    """Purely the financial side (how much got deducted from payout)."""
    note_number = parsed["note_number"]
    if not note_number:
        raise ValueError("Parsed debit note has no note_number, refusing to store it")
    record_flags(conn, "debit_note", note_number, validate_debit_note(parsed), source_file)

    customer_id = _ensure_customer(conn, customer_name)
    _ensure_po_stub(conn, parsed.get("po_number"), customer_id)

    conn.execute(
        """
        INSERT INTO debit_notes
            (note_number, reference_number, po_number, invoice_number,
             discrepancy_type, note_date, customer_id, sub_total,
             tax_amount, total_amount, credits_remaining, source_file)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(note_number) DO UPDATE SET
            reference_number=excluded.reference_number,
            po_number=excluded.po_number,
            invoice_number=excluded.invoice_number,
            discrepancy_type=excluded.discrepancy_type,
            note_date=excluded.note_date,
            customer_id=excluded.customer_id,
            sub_total=excluded.sub_total,
            tax_amount=excluded.tax_amount,
            total_amount=excluded.total_amount,
            credits_remaining=excluded.credits_remaining,
            source_file=excluded.source_file
        """,
        (
            note_number, parsed.get("reference_number"), parsed.get("po_number"),
            parsed.get("invoice_number"), parsed.get("discrepancy_type"),
            parsed.get("note_date"), customer_id, parsed.get("sub_total"),
            parsed.get("tax_amount"), parsed.get("total_amount"),
            parsed.get("credits_remaining"), source_file,
        ),
    )

    conn.execute("DELETE FROM debit_note_items WHERE note_number = ?", (note_number,))
    for item in parsed.get("line_items", []):
        conn.execute(
            """
            INSERT INTO debit_note_items (note_number, description, sku_code, qty, rate, amount)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (note_number, item.get("description"), None, item.get("qty"),
             item.get("rate"), item.get("amount")),
        )
    conn.commit()
    return note_number


def import_appointments_csv(conn, csv_path):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    customer_id = _ensure_customer(conn, DEFAULT_CUSTOMER)
    n = 0
    for row in rows:
        po_number = (row["PurchaseOrderIds"] or "").split(",")[0].strip() or None
        _ensure_po_stub(conn, po_number, customer_id)
        conn.execute(
            """
            INSERT INTO appointments
                (appointment_id, po_number, facility_name, slot_date,
                 slot_time, booked_qty, state)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(appointment_id) DO UPDATE SET
                po_number=excluded.po_number,
                facility_name=excluded.facility_name,
                slot_date=excluded.slot_date,
                slot_time=excluded.slot_time,
                booked_qty=excluded.booked_qty,
                state=excluded.state
            """,
            (
                row["AppointmentId"], po_number, row["FacilityName"],
                row["SlotDate"], row["Slot"],
                float(row["BookedQuantity"] or 0), row["AppointmentState"],
            ),
        )
        n += 1

    conn.commit()
    return {"appointments": n}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    command, path = sys.argv[1], sys.argv[2]
    conn = get_connection()

    if command == "po":
        result = upsert_po(conn, parse_po_pdf(path), source_file=Path(path).name)
        print(f"Stored PO {result}")
    elif command == "grn":
        result = upsert_grn(conn, parse_grn_pdf(path), source_file=Path(path).name)
        print(f"Stored GRN {result}")
    elif command == "debit-note":
        result = upsert_debit_note(conn, parse_debit_note_pdf(path), source_file=Path(path).name)
        print(f"Stored debit note {result}")
    elif command == "appointments-csv":
        print(import_appointments_csv(conn, path))
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)

    conn.close()

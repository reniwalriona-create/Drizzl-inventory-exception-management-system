"""
Loads parsed documents into inventory.db, and keeps the inventory_movements
ledger in sync with them automatically. GRNs arrive as individual PDFs, one
per delivery -- there is no bulk CSV import path.

Usage:
    python3 ingest.py po <po_pdf_path>
    python3 ingest.py grn <grn_pdf_path>
    python3 ingest.py appointments-csv <appointments_csv_path>
    python3 ingest.py discrepancy-note <discrepancy_note_pdf_path>
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
from discrepancy_note_parser import parse_discrepancy_note_pdf
from debit_note_parser import parse_debit_note_pdf
from validate import validate_po, validate_grn, validate_discrepancy_note, validate_debit_note, record_flags

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
    conn.execute("INSERT OR IGNORE INTO customers (name) VALUES (?)", (name,))
    return conn.execute(
        "SELECT id FROM customers WHERE name = ?", (name,)
    ).fetchone()["id"]


def _ensure_location(conn, name, loc_type="own_facility"):
    if not name:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO locations (name, type) VALUES (?, ?)", (name, loc_type)
    )
    return conn.execute(
        "SELECT id FROM locations WHERE name = ?", (name,)
    ).fetchone()["id"]


def _ensure_product(conn, sku_code, sku_desc=None):
    sku_code = _normalize_sku(sku_code)
    if not sku_code:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO products (sku_code, sku_desc) VALUES (?, ?)",
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
        "INSERT OR IGNORE INTO purchase_orders (po_number, customer_id) VALUES (?, ?)",
        (po_number, customer_id),
    )


def _ensure_grn_stub(conn, grn_number, customer_id=None):
    """Discrepancy Notes reference a GRN that may not have been uploaded
    yet (or may never be, if only the CSV export covers it)."""
    if not grn_number:
        return
    conn.execute(
        "INSERT OR IGNORE INTO grn_receipts (grn_number, customer_id) VALUES (?, ?)",
        (grn_number, customer_id),
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
    (unvoid_grn(), reverses this exactly)."""
    row = conn.execute("SELECT grn_number FROM grn_receipts WHERE grn_number = ?", (grn_number,)).fetchone()
    if row is None:
        raise ValueError("GRN not found.")
    conn.execute(
        "UPDATE grn_receipts SET voided = 1, void_reason = ?, voided_at = CURRENT_TIMESTAMP WHERE grn_number = ?",
        (reason, grn_number),
    )
    conn.execute(
        "UPDATE inventory_movements SET voided = 1, void_reason = ?, voided_at = CURRENT_TIMESTAMP "
        "WHERE reference_type = 'grn' AND reference_id = ?",
        (reason, grn_number),
    )


def void_discrepancy_note(conn, dn_number, reason):
    """A Discrepancy Note is purely informational (see PROJECT_HANDOFF.md
    section 4) -- it never creates a ledger movement, so voiding it just
    flags the row itself; reconcile.grn_discrepancies() then treats the
    GRN line as if no Discrepancy Note had explained it."""
    row = conn.execute("SELECT dn_number FROM discrepancy_notes WHERE dn_number = ?", (dn_number,)).fetchone()
    if row is None:
        raise ValueError("Discrepancy Note not found.")
    conn.execute(
        "UPDATE discrepancy_notes SET voided = 1, void_reason = ?, voided_at = CURRENT_TIMESTAMP WHERE dn_number = ?",
        (reason, dn_number),
    )


def void_movement(conn, movement_id, reason):
    """Only a manually-entered movement (reference_type='manual') can be
    voided this way. A GRN/PO/Discrepancy-Note-generated movement is
    kept in sync with its parent document by upsert_grn() etc. -- voiding
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


def unvoid_grn(conn, grn_number):
    """Restores a mistakenly-voided GRN, and un-voids its sale
    movement(s) along with it -- the exact reverse of void_grn(). Safe
    to blanket-restore every movement referencing this GRN: a
    GRN-sourced movement can only ever have been voided as part of
    voiding this same GRN (void_movement() refuses to void a non-manual
    movement directly), so there's no risk of un-voiding something that
    was independently voided for a different reason."""
    conn.execute(
        "UPDATE grn_receipts SET voided = 0, void_reason = NULL, voided_at = NULL WHERE grn_number = ?",
        (grn_number,),
    )
    conn.execute(
        "UPDATE inventory_movements SET voided = 0, void_reason = NULL, voided_at = NULL "
        "WHERE reference_type = 'grn' AND reference_id = ?",
        (grn_number,),
    )


def unvoid_discrepancy_note(conn, dn_number):
    conn.execute(
        "UPDATE discrepancy_notes SET voided = 0, void_reason = NULL, voided_at = NULL WHERE dn_number = ?",
        (dn_number,),
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
    source location yet."""
    location_id = _ensure_location(conn, location_name, location_type)
    row = conn.execute("SELECT po_number FROM purchase_orders WHERE po_number = ?", (po_number,)).fetchone()
    if row is None:
        raise ValueError("PO not found.")
    conn.execute(
        "UPDATE purchase_orders SET source_location_id = ? WHERE po_number = ?",
        (location_id, po_number),
    )
    for grn in conn.execute("SELECT grn_number FROM grn_receipts WHERE po_number = ? AND voided = 0", (po_number,)).fetchall():
        _create_pending_grn_sales(conn, grn["grn_number"])


def assign_grn_source_location(conn, grn_number, location_name, location_type="own_facility"):
    """Sets a GRN's own source location directly -- the fallback used
    when the GRN has no po_number, or its PO hasn't been allocated a
    source location either (see reconcile.resolve_grn_source_location()).
    Also creates whichever of this GRN's sale movements were pending on
    a source location existing."""
    location_id = _ensure_location(conn, location_name, location_type)
    row = conn.execute("SELECT grn_number FROM grn_receipts WHERE grn_number = ?", (grn_number,)).fetchone()
    if row is None:
        raise ValueError("GRN not found.")
    conn.execute(
        "UPDATE grn_receipts SET source_location_id = ? WHERE grn_number = ?",
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

    grn = conn.execute(
        "SELECT grn_date FROM grn_receipts WHERE grn_number = ? AND voided = 0", (grn_number,)
    ).fetchone()
    if not grn:
        return

    existing_skus = {
        r["sku_code"] for r in conn.execute(
            "SELECT DISTINCT sku_code FROM inventory_movements WHERE reference_type = 'grn' AND reference_id = ?",
            (grn_number,),
        ).fetchall()
    }

    pending_lines = conn.execute(
        "SELECT sku_code, received_qty FROM grn_line_items WHERE grn_number = ? AND received_qty IS NOT NULL AND received_qty != 0",
        (grn_number,),
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


def record_movement(conn, movement_date, sku_code, movement_type, quantity,
                     location_from=None, location_to=None, reason=None,
                     reference_type=None, reference_id=None, notes=None,
                     recorded_by=None, sku_desc=None,
                     location_from_type="own_facility", location_to_type="own_facility",
                     negative_override_reason=None, commitment_override_reason=None):
    sku_code = _ensure_product(conn, sku_code, sku_desc)
    from_id = _ensure_location(conn, location_from, location_from_type) if location_from else None
    to_id = _ensure_location(conn, location_to, location_to_type) if location_to else None
    cur = conn.execute(
        """
        INSERT INTO inventory_movements
            (movement_date, sku_code, movement_type, quantity,
             location_from_id, location_to_id, reason, reference_type,
             reference_id, notes, recorded_by, negative_override_reason,
             commitment_override_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (movement_date, sku_code, movement_type, quantity, from_id, to_id,
         reason, reference_type, reference_id, notes, recorded_by,
         negative_override_reason, commitment_override_reason),
    )
    return cur.lastrowid


def record_inventory_flag(conn, sku_code, location_name, source, available_before,
                           requested_qty, resulting_balance, movement_id=None,
                           reference_id=None, reason=None):
    """Logs a negative-inventory incident -- either a human's explicit
    override on a manual transfer/sale/loss, or a real GRN's sale
    movement pushing its source location below zero. See inventory_flags
    in schema.sql; surfaced on the dashboard by
    reconcile.unresolved_inventory_flags()."""
    conn.execute(
        """
        INSERT INTO inventory_flags
            (movement_id, sku_code, location_name, source, reference_id,
             available_before, requested_qty, resulting_balance, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (movement_id, sku_code, location_name, source, reference_id,
         available_before, requested_qty, resulting_balance, reason),
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
    brand new rows, which default to voided=0 already."""
    grn_number = parsed["grn_number"]
    if not grn_number:
        raise ValueError("Parsed GRN has no grn_number, refusing to store it")
    record_flags(conn, "grn", grn_number, validate_grn(parsed), source_file)

    customer_id = _ensure_customer(conn, customer_name)
    _ensure_po_stub(conn, parsed.get("po_number"), customer_id)

    conn.execute(
        """
        INSERT INTO grn_receipts
            (grn_number, po_number, customer_id, inbound_no, grn_date, create_date,
             invoice_no, invoice_date, challan_no, challan_date,
             vendor_name, facility_name, source, source_file)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(grn_number) DO UPDATE SET
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
        """,
        (
            grn_number, parsed.get("po_number"), customer_id, parsed.get("inbound_no"),
            parsed.get("grn_date"), parsed.get("create_date"),
            parsed.get("invoice_no"), parsed.get("invoice_date"),
            parsed.get("challan_no"), parsed.get("challan_date"),
            parsed.get("vendor_name"), parsed.get("facility_name"), source, source_file,
        ),
    )

    conn.execute("DELETE FROM grn_line_items WHERE grn_number = ?", (grn_number,))
    clear_movements_for_reference(conn, "grn", grn_number)

    for item in parsed.get("line_items", []):
        sku_code = _ensure_product(conn, item.get("sku_code"), item.get("sku_desc"))
        conn.execute(
            """
            INSERT INTO grn_line_items
                (grn_number, sku_code, sku_desc, lot_no, lot_mrp,
                 expected_qty, received_qty, unit_price, taxable_value,
                 cgst_rate, cgst_amt, sgst_rate, sgst_amt, igst_rate,
                 igst_amt, cess_rate, cess_amt, add_cess, total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                grn_number, sku_code, item.get("sku_desc"),
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
    # discrepancy to investigate (see reconcile.py's grn_discrepancies()),
    # not an automatic loss -- we don't yet know why the units are missing.
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


def upsert_discrepancy_note(conn, parsed, source_file=None, customer_name=DEFAULT_CUSTOMER):
    """Re-uploading a dn_number that was voided automatically un-voids
    it, same reasoning as upsert_po()."""
    dn_number = parsed["dn_number"]
    if not dn_number:
        raise ValueError("Parsed discrepancy note has no dn_number, refusing to store it")
    record_flags(conn, "discrepancy_note", dn_number, validate_discrepancy_note(parsed), source_file)

    customer_id = _ensure_customer(conn, customer_name)
    _ensure_po_stub(conn, parsed.get("po_number"), customer_id)
    _ensure_grn_stub(conn, parsed.get("grn_number"), customer_id)

    conn.execute(
        """
        INSERT INTO discrepancy_notes
            (dn_number, dn_date, po_number, grn_number, invoice_number,
             inbound_no, customer_id, grn_qty, grn_amt, total_dn_qty,
             dn_amt, invoice_amt, source_file)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dn_number) DO UPDATE SET
            dn_date=excluded.dn_date,
            po_number=excluded.po_number,
            grn_number=excluded.grn_number,
            invoice_number=excluded.invoice_number,
            inbound_no=excluded.inbound_no,
            customer_id=excluded.customer_id,
            grn_qty=excluded.grn_qty,
            grn_amt=excluded.grn_amt,
            total_dn_qty=excluded.total_dn_qty,
            dn_amt=excluded.dn_amt,
            invoice_amt=excluded.invoice_amt,
            source_file=excluded.source_file,
            voided=0, void_reason=NULL, voided_at=NULL
        """,
        (
            dn_number, parsed.get("dn_date"), parsed.get("po_number"),
            parsed.get("grn_number"), parsed.get("invoice_number"),
            parsed.get("inbound_no"), customer_id, parsed.get("grn_qty"),
            parsed.get("grn_amt"), parsed.get("total_dn_qty"),
            parsed.get("dn_amt"), parsed.get("invoice_amt"), source_file,
        ),
    )

    conn.execute("DELETE FROM discrepancy_note_items WHERE dn_number = ?", (dn_number,))

    for item in parsed.get("line_items", []):
        sku_code = _ensure_product(conn, item.get("sku_code"), item.get("sku_desc"))
        conn.execute(
            """
            INSERT INTO discrepancy_note_items
                (dn_number, sno, sku_code, hsn_code, sku_desc, reason,
                 remarks, exp_qty, dn_qty, lot_mrp, unit_price,
                 taxable_value, cgst_rate, cgst_amt, sgst_rate, sgst_amt,
                 igst_rate, igst_amt, cess_rate, cess_amt, total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dn_number, item.get("sno"), sku_code, item.get("hsn_code"),
                item.get("sku_desc"), item.get("reason"), item.get("remarks"),
                item.get("exp_qty"), item.get("dn_qty"), item.get("lot_mrp"),
                item.get("unit_price"), item.get("taxable_value"),
                item.get("cgst_rate"), item.get("cgst_amt"),
                item.get("sgst_rate"), item.get("sgst_amt"),
                item.get("igst_rate"), item.get("igst_amt"),
                item.get("cess_rate"), item.get("cess_amt"), item.get("total"),
            ),
        )
        # MVP: a Discrepancy Note is stored as supporting/explanatory
        # detail for a GRN discrepancy that was already detected (see
        # reconcile.py's grn_discrepancies()) -- it does NOT automatically
        # create a 'loss' movement. We don't yet have a confirmed business
        # rule for converting a discrepancy reason into a specific
        # inventory adjustment, so it's left unresolved rather than
        # guessed. A real loss can still be recorded manually (see
        # record_movement / the /movements/new form) once a human decides
        # that's what actually happened.
    conn.commit()
    return dn_number


def upsert_debit_note(conn, parsed, source_file=None, customer_name=DEFAULT_CUSTOMER):
    """Purely the financial side (how much got deducted from payout).
    The physical loss itself is recorded by upsert_discrepancy_note --
    this table exists so the two can be cross-checked against each other."""
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
    elif command == "discrepancy-note":
        result = upsert_discrepancy_note(conn, parse_discrepancy_note_pdf(path), source_file=Path(path).name)
        print(f"Stored discrepancy note {result}")
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

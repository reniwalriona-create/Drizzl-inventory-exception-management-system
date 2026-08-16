"""
GRN posting (Phase 8) -- turns a VERIFIED staged GRN (Phase 6/7) into an
official grn_receipts/grn_line_items record, creates one canonical SALE
inventory_movements row per normalized receipt line (received_qty > 0),
and closes the FULL commitment of the matched official PO -- regardless
of whether every ordered product actually arrived (see reconcile.py's
committed_quantity(), header-level canonical release branch).

This is the first phase that writes official business records AND
modifies physical inventory -- correctness here is inventory-critical.
Staging rows are never deleted or edited: staged_grns.posted_grn_id/
posted_at and staged_grn_lines.posted_grn_line_item_id link the reviewed
snapshot to what it produced.

Transaction ownership: like grn_csv_staging.py, this module does NOT call
conn.commit()/conn.rollback() -- the caller (app.py's post route) owns
the transaction so a whole selected posting is atomic.

Never calls ingest.upsert_grn()/_record_grn_sale()/_ensure_product() --
those belong to the legacy PDF architecture and depend on legacy SKU
identity. Reuses ingest.record_movement()/record_inventory_flag() in
their Phase 8 canonical mode (product_id supplied) -- product_id is
authoritative there: sku_code is derived fresh from master_products.
barcode inside those functions, never trusted from a value passed here.
"""
import reconcile
from ingest import record_inventory_flag, record_movement


class PostingError(Exception):
    """Raised for a malformed request (unknown staged_grn_id, cross-batch
    id, empty selection) or a genuine data-integrity conflict (a staged
    record's posted_grn_id points at an official GRN that no longer
    exists) -- never for an ordinary 'this GRN isn't ready' business
    outcome, which comes back in post_staged_grns()'s `rejected` list."""


def _load_selection(conn, batch_id, staged_grn_ids):
    """Loads AND locks (SELECT ... FOR UPDATE, deterministic ascending-id
    order) every selected staged_grns row -- the lock, plus the PO-row
    lock in _lock_purchase_orders(), is what makes concurrent posting
    requests safe (see module docstring and PROJECT_HANDOFF.md)."""
    staged_grn_ids = list(staged_grn_ids)
    if not staged_grn_ids:
        raise PostingError("No staged GRNs were selected.")

    placeholders = ",".join(["?"] * len(staged_grn_ids))
    rows = conn.execute(
        f"""
        SELECT g.*, b.source_filename AS batch_source_filename
        FROM staged_grns g
        JOIN grn_import_batches b ON b.batch_id = g.batch_id
        WHERE g.staged_grn_id IN ({placeholders})
        ORDER BY g.staged_grn_id
        FOR UPDATE OF g
        """,
        tuple(staged_grn_ids),
    ).fetchall()
    found = {r["staged_grn_id"]: dict(r) for r in rows}

    missing = set(staged_grn_ids) - set(found)
    if missing:
        raise PostingError(f"Staged GRN id(s) {sorted(missing)} do not exist.")
    wrong_batch = {sid for sid, g in found.items() if g["batch_id"] != batch_id}
    if wrong_batch:
        raise PostingError(f"Staged GRN id(s) {sorted(wrong_batch)} do not belong to this batch.")

    return [found[sid] for sid in staged_grn_ids]


def _lock_purchase_orders(conn, po_ids):
    """Locks the distinct set of matched official PO rows, deterministic
    ascending-po_id order. This is what actually serializes concurrent
    posting attempts against the same PO: two transactions both trying to
    post a GRN for the same po_id block on this same row lock, so the
    active-GRN-for-PO check in _conflict_failures() is race-free even
    though grn_receipts itself has no row to lock yet for a PO that's
    never been posted against before (the classic phantom-insert problem
    -- locking the parent PO row avoids it)."""
    if not po_ids:
        return {}
    placeholders = ",".join(["?"] * len(po_ids))
    rows = conn.execute(
        f"SELECT * FROM purchase_orders WHERE po_id IN ({placeholders}) ORDER BY po_id FOR UPDATE",
        tuple(po_ids),
    ).fetchall()
    return {r["po_id"]: dict(r) for r in rows}


def _staged_lines(conn, staged_grn_id):
    return conn.execute(
        "SELECT * FROM staged_grn_lines WHERE staged_grn_id = ? ORDER BY staged_grn_line_id",
        (staged_grn_id,),
    ).fetchall()


def _readiness_failures(conn, staged_grn, lines, po_by_id):
    """Independently reconstructs whether this staged GRN is safe to
    post, ignoring whatever the browser/UI claims -- re-derives every
    condition from current database state, never trusting the stored
    VERIFIED badge."""
    reasons = []

    if staged_grn["validation_status"] == "blocked":
        reasons.append("staging validation is blocked (unresolved data/normalization problem).")
    if staged_grn["po_verification_status"] != "verified":
        reasons.append("PO verification is not currently 'verified'.")
    if not staged_grn["external_grn_number"]:
        reasons.append("it has no external GRN number.")
    if not staged_grn["external_po_number"]:
        reasons.append("it has no external PO number.")
    if conn.execute("SELECT 1 FROM customers WHERE id = ?", (staged_grn["customer_id"],)).fetchone() is None:
        reasons.append("its customer no longer exists.")
    if staged_grn["external_created_at"] is None:
        reasons.append("it has no usable GRN event date (external_created_at is missing) -- refusing to invent today's date.")

    official_po_id = staged_grn["official_po_id"]
    po = po_by_id.get(official_po_id) if official_po_id else None
    if official_po_id is None:
        reasons.append("no official PO is currently matched.")
    elif po is None:
        reasons.append(f"official PO id {official_po_id} no longer exists.")
    else:
        if po["voided"]:
            reasons.append(f"official PO {po['po_number']} is voided.")
        if po["source_location_id"] is None:
            reasons.append(f"official PO {po['po_number']} has no Drizzl source warehouse.")
        elif conn.execute("SELECT 1 FROM locations WHERE id = ?", (po["source_location_id"],)).fetchone() is None:
            reasons.append(f"official PO {po['po_number']}'s source warehouse no longer exists.")

    if not lines:
        reasons.append("it has no normalized receipt lines.")
    for line in lines:
        if line["validation_status"] != "valid":
            reasons.append(f"line {line['staged_grn_line_id']} (SKU {line['external_sku']}) failed staging validation.")
        elif line["product_id"] is None:
            reasons.append(f"line {line['staged_grn_line_id']} (SKU {line['external_sku']}) has no resolved Master Product.")
        elif conn.execute("SELECT 1 FROM master_products WHERE product_id = ?", (line["product_id"],)).fetchone() is None:
            reasons.append(f"line {line['staged_grn_line_id']}'s Master Product no longer exists.")
        elif not line["external_sku"]:
            reasons.append(f"line {line['staged_grn_line_id']} has no external SKU.")

    return reasons


def _conflict_failures(conn, staged_grn, po_by_id, po_ids_targeted_this_request):
    """Duplicate/active-GRN conflict checks, independent of readiness.
    po_ids_targeted_this_request guards against two staged GRNs in the
    SAME selection both targeting the same PO -- the row lock alone only
    protects against a DIFFERENT concurrent request, not two entries
    within this one."""
    reasons = []
    existing = conn.execute(
        "SELECT grn_id, customer_id FROM grn_receipts WHERE grn_number = ?",
        (staged_grn["external_grn_number"],),
    ).fetchone()
    if existing is not None:
        if existing["customer_id"] == staged_grn["customer_id"]:
            reasons.append(
                f"an official GRN {staged_grn['external_grn_number']!r} already exists (grn_id "
                f"{existing['grn_id']}) and is not linked to this staged record -- this needs the "
                "(not-yet-built) duplicate/correction review workflow."
            )
        else:
            reasons.append(
                f"GRN number {staged_grn['external_grn_number']!r} is already used by another "
                "customer's official GRN -- grn_receipts.grn_number is still globally unique "
                "(temporary Phase 8 compatibility scaffolding)."
            )

    po = po_by_id.get(staged_grn["official_po_id"])
    if po is not None:
        if po["po_id"] in po_ids_targeted_this_request:
            reasons.append(
                f"another staged GRN in this same posting request also targets official PO "
                f"{po['po_number']} -- only one active GRN per PO is currently supported."
            )
        else:
            active = conn.execute(
                "SELECT grn_id FROM grn_receipts WHERE po_id = ? AND voided = 0", (po["po_id"],)
            ).fetchone()
            if active is not None:
                reasons.append(
                    f"official PO {po['po_number']} already has an active official GRN (grn_id "
                    f"{active['grn_id']}) -- only one active GRN per PO is currently supported."
                )
    return reasons


def _insert_official_grn(conn, staged_grn, lines, po):
    movement_date = staged_grn["external_created_at"]
    movement_date_str = movement_date.date().isoformat() if hasattr(movement_date, "date") else str(movement_date)

    grn_number = staged_grn["external_grn_number"]
    grn_row = conn.execute(
        """
        INSERT INTO grn_receipts
            (grn_number, po_number, po_id, customer_id, invoice_no, invoice_date,
             create_date, vendor_name, facility_name, supplier_code, dn_number,
             source, source_file, source_location_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'csv', ?, ?)
        RETURNING grn_id
        """,
        (
            grn_number, staged_grn["external_po_number"], po["po_id"], staged_grn["customer_id"],
            staged_grn["invoice_number"],
            staged_grn["invoice_date"].isoformat() if staged_grn["invoice_date"] else None,
            movement_date.isoformat() if movement_date else None,
            staged_grn["vendor_name"], staged_grn["facility_name"], staged_grn["supplier_code"],
            staged_grn["dn_number"], staged_grn["batch_source_filename"], po["source_location_id"],
        ),
    ).fetchone()
    grn_id = grn_row["grn_id"]

    source_location_name = conn.execute(
        "SELECT name FROM locations WHERE id = ?", (po["source_location_id"],)
    ).fetchone()["name"]

    for line in lines:
        line_row = conn.execute(
            """
            INSERT INTO grn_line_items
                (grn_number, sku_code, sku_desc, lot_mrp, lot_expiry_date, received_qty,
                 taxable_value, total, product_id, external_sku, external_sku_description,
                 source_dn_quantity, source_dn_value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                grn_number, line["external_sku"], line["external_sku_description"],
                line["lot_mrp"], line["lot_expiry_date"].isoformat() if line["lot_expiry_date"] else None,
                line["received_qty"], line["grn_line_value_without_tax"], line["total_amount"],
                line["product_id"], line["external_sku"], line["external_sku_description"],
                line["dn_quantity"], line["dn_value"],
            ),
        ).fetchone()
        line_id = line_row["id"]
        conn.execute(
            "UPDATE staged_grn_lines SET posted_grn_line_item_id = ? WHERE staged_grn_line_id = ?",
            (line_id, line["staged_grn_line_id"]),
        )

        received_qty = float(line["received_qty"]) if line["received_qty"] is not None else None
        if received_qty and received_qty > 0:
            # A real GRN is never blocked for going negative -- see
            # ingest.py's _record_grn_sale() docstring, same reasoning
            # applies unchanged to the canonical path. float() above --
            # staged_grn_lines.received_qty is NUMERIC (Decimal via
            # psycopg2), current_balance_by_product() is REAL (float);
            # normalize to float before mixing them in arithmetic.
            available_before = reconcile.current_balance_by_product(conn, po["source_location_id"], line["product_id"])
            resulting_balance = available_before - received_qty
            movement_id = record_movement(
                conn, movement_date=movement_date_str, sku_code=None,
                movement_type="sale", quantity=received_qty,
                location_from=source_location_name, reason="sold_to_customer",
                reference_type="grn", reference_id=grn_number,
                product_id=line["product_id"], source_grn_line_item_id=line_id,
            )
            if resulting_balance < 0:
                record_inventory_flag(
                    conn, sku_code=None, location_name=source_location_name,
                    source="grn", available_before=available_before,
                    requested_qty=received_qty, resulting_balance=resulting_balance,
                    movement_id=movement_id, reference_id=grn_number,
                    product_id=line["product_id"],
                )

    conn.execute(
        "UPDATE staged_grns SET posted_grn_id = ?, posted_at = CURRENT_TIMESTAMP WHERE staged_grn_id = ?",
        (grn_id, staged_grn["staged_grn_id"]),
    )
    return grn_id


def post_staged_grns(conn, batch_id, staged_grn_ids):
    """Posts every staged GRN in staged_grn_ids (all must belong to
    batch_id) into the official ledger, creating canonical SALE movements
    and closing the matched PO's full commitment. All-or-nothing across
    the whole selection: if ANY not-yet-posted staged GRN in the
    selection isn't ready or conflicts, NOTHING is written for ANY of
    them. Already-posted staged GRNs in the selection are a harmless
    no-op and never block the rest.

    Returns:
        {
          "posted": [{"staged_grn_id", "grn_number", "grn_id"}, ...],
          "already_posted": [{"staged_grn_id", "grn_number", "grn_id"}, ...],
          "rejected": {staged_grn_id: [reason, ...]},
        }
    "rejected" non-empty means "posted" is always empty for this call.
    Raises PostingError for a malformed request or a data-integrity
    conflict (see that class's docstring). Caller owns commit/rollback."""
    selection = _load_selection(conn, batch_id, staged_grn_ids)

    already_posted = []
    to_post = []
    for g in selection:
        if g["posted_grn_id"] is not None:
            official = conn.execute("SELECT grn_id FROM grn_receipts WHERE grn_id = ?", (g["posted_grn_id"],)).fetchone()
            if official is None:
                raise PostingError(
                    f"Staged GRN {g['staged_grn_id']} claims posted_grn_id={g['posted_grn_id']} but that "
                    "official GRN no longer exists -- integrity conflict, refusing to silently recreate it."
                )
            already_posted.append(g)
        else:
            to_post.append(g)

    po_ids = sorted({g["official_po_id"] for g in to_post if g["official_po_id"] is not None})
    po_by_id = _lock_purchase_orders(conn, po_ids)

    rejected = {}
    lines_by_staged_grn = {}
    po_ids_targeted_this_request = set()
    for g in to_post:
        lines = _staged_lines(conn, g["staged_grn_id"])
        lines_by_staged_grn[g["staged_grn_id"]] = lines
        reasons = _readiness_failures(conn, g, lines, po_by_id)
        if not reasons:
            reasons = _conflict_failures(conn, g, po_by_id, po_ids_targeted_this_request)
        if reasons:
            rejected[g["staged_grn_id"]] = reasons
        elif g["official_po_id"] is not None:
            po_ids_targeted_this_request.add(g["official_po_id"])

    if rejected:
        return {"posted": [], "already_posted": [], "rejected": rejected}

    posted = []
    for g in to_post:
        po = po_by_id[g["official_po_id"]]
        grn_id = _insert_official_grn(conn, g, lines_by_staged_grn[g["staged_grn_id"]], po)
        posted.append({"staged_grn_id": g["staged_grn_id"], "grn_number": g["external_grn_number"], "grn_id": grn_id})

    already_posted_out = [
        {"staged_grn_id": g["staged_grn_id"], "grn_number": g["external_grn_number"], "grn_id": g["posted_grn_id"]}
        for g in already_posted
    ]
    return {"posted": posted, "already_posted": already_posted_out, "rejected": {}}

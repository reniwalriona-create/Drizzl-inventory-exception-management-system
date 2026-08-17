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
from ingest import record_inventory_flag, record_movement, void_grn


class PostingError(Exception):
    """Raised for a malformed request (unknown staged_grn_id, cross-batch
    id, empty selection) or a genuine data-integrity conflict (a staged
    record's posted_grn_id points at an official GRN that no longer
    exists) -- never for an ordinary 'this GRN isn't ready' business
    outcome, which comes back in post_staged_grns()'s `rejected` list."""


class CorrectionError(Exception):
    """Phase 10: raised by replace_posted_grn() for a malformed
    correction request or a data-integrity mismatch (the staged/official
    GRN pair doesn't actually correspond, or the official GRN being
    replaced isn't the PO's currently active one) -- never for an
    ordinary 'this corrected GRN isn't ready to post' outcome, which
    raises a plain ValueError listing every reason instead (mirrors
    post_staged_grns()'s rejected-vs-PostingError split)."""


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


def _readiness_failures(conn, staged_grn, lines, po_by_id, expect_replacing_grn_id=None):
    """Independently reconstructs whether this staged GRN is safe to
    post, ignoring whatever the browser/UI claims -- re-derives every
    condition from current database state, never trusting the stored
    VERIFIED badge.

    expect_replacing_grn_id (Phase 10, default None): the ONE relaxation
    replace_posted_grn() is allowed to make to normal readiness. When
    set, a po_verification_status of 'blocked' is tolerated IF its only
    error(s) are official_grn_already_exists (required) and,
    optionally, duplicate_grn_in_other_batch -- and both conditions
    below hold:
      - official_grn_already_exists refers to exactly
        expect_replacing_grn_id (same grn_number + customer_id).
      - if duplicate_grn_in_other_batch is present too (it always will
        be in practice: the original upload that produced the GRN being
        replaced is itself "another batch" with the same grn_number,
        and staging rows are never deleted -- see grn_csv_staging.
        validate_staged_grn()), every OTHER staged_grns row sharing this
        grn_number is either this correction's own already-posted origin
        (posted_grn_id == expect_replacing_grn_id) or itself. A
        genuinely separate THIRD staged candidate for the same
        grn_number still blocks, unchanged -- that's real ambiguity,
        not the expected correction shape.
    Any other blocking reason (mismatched PO, over-received, unresolved
    product, missing event date, etc.) still fails readiness exactly as
    normal posting would; this never weakens any other rule."""
    reasons = []

    if staged_grn["validation_status"] == "blocked":
        reasons.append("staging validation is blocked (unresolved data/normalization problem).")

    if staged_grn["po_verification_status"] != "verified":
        allowed = False
        if expect_replacing_grn_id is not None and staged_grn["po_verification_status"] == "blocked":
            errors = staged_grn["po_verification_errors"] or []
            codes = {e["code"] for e in errors}
            # duplicate_grn_in_other_batch is expected noise during a
            # correction -- the original staged GRN that produced the
            # official GRN being replaced always lives in a different
            # batch and always shares this external_grn_number, so it
            # always trips this finding too. It says nothing about
            # whether THIS staged candidate is safe to post as the
            # replacement; the actual safety guarantee (that old_grn_id
            # really is the PO's currently active GRN, and that a
            # concurrent replacement attempt can't also win) comes from
            # replace_posted_grn()'s own row locks, not from this
            # staging-time heuristic -- so it's always tolerated
            # alongside official_grn_already_exists, regardless of how
            # many OTHER staged siblings exist or their own posted
            # state (a second, still-unposted correction candidate
            # racing this one must not block either from being
            # attempted).
            allowed_codes = {"official_grn_already_exists", "duplicate_grn_in_other_batch"}
            if codes and codes <= allowed_codes and "official_grn_already_exists" in codes:
                # AND voided = 0 -- the conflict this correction is
                # allowed to override is with the CURRENTLY ACTIVE GRN
                # under this number, never a historical (already
                # superseded) one that happens to share it.
                conflicting = conn.execute(
                    "SELECT grn_id FROM grn_receipts WHERE grn_number = ? AND customer_id = ? AND voided = 0",
                    (staged_grn["external_grn_number"], staged_grn["customer_id"]),
                ).fetchone()
                allowed = conflicting is not None and conflicting["grn_id"] == expect_replacing_grn_id
        if not allowed:
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
    # AND voided = 0 -- a historical (already voided/superseded) GRN
    # sharing this grn_number no longer blocks a fresh posting attempt
    # (Phase 10's whole point); only a currently ACTIVE conflict does.
    existing = conn.execute(
        "SELECT grn_id, customer_id FROM grn_receipts WHERE grn_number = ? AND voided = 0",
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


def _insert_official_grn(conn, staged_grn, lines, po, supersedes_grn_id=None):
    """supersedes_grn_id (Phase 10, default None): set only by
    replace_posted_grn() when this INSERT is the corrected replacement
    for an already-voided official GRN -- see grn_receipts.
    supersedes_grn_id in schema_postgres.sql. Every other caller
    (post_staged_grns(), normal first-time posting) leaves it None."""
    movement_date = staged_grn["external_created_at"]
    movement_date_str = movement_date.date().isoformat() if hasattr(movement_date, "date") else str(movement_date)

    grn_number = staged_grn["external_grn_number"]
    grn_row = conn.execute(
        """
        INSERT INTO grn_receipts
            (grn_number, po_number, po_id, customer_id, invoice_no, invoice_date,
             create_date, vendor_name, facility_name, supplier_code, dn_number,
             source, source_file, source_location_id, supersedes_grn_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'csv', ?, ?, ?)
        RETURNING grn_id
        """,
        (
            grn_number, staged_grn["external_po_number"], po["po_id"], staged_grn["customer_id"],
            staged_grn["invoice_number"],
            staged_grn["invoice_date"].isoformat() if staged_grn["invoice_date"] else None,
            movement_date.isoformat() if movement_date else None,
            staged_grn["vendor_name"], staged_grn["facility_name"], staged_grn["supplier_code"],
            staged_grn["dn_number"], staged_grn["batch_source_filename"], po["source_location_id"],
            supersedes_grn_id,
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
                (grn_number, grn_id, sku_code, sku_desc, lot_mrp, lot_expiry_date, received_qty,
                 taxable_value, total, product_id, external_sku, external_sku_description,
                 source_dn_quantity, source_dn_value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                grn_number, grn_id, line["external_sku"], line["external_sku_description"],
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


# ---------------------------------------------------------------------------
# Phase 10: corrected-GRN replacement.
#
# Once posted, an official GRN is never edited/deleted in place -- a
# correction is always "void old + post replacement", atomically, with a
# durable link (grn_receipts.supersedes_grn_id) between the two. This is
# NOT automatic: the operator must explicitly choose a specific staged
# GRN as the replacement for a specific official GRN (never inferred
# from filename/timestamp/GRN number alone).
# ---------------------------------------------------------------------------


def find_correction_target(conn, staged_grn_id):
    """The currently-active official GRN a not-yet-posted staged GRN
    collides with (same customer + same external grn_number) -- what the
    UI uses to decide whether to offer the Correct/Replace action on a
    QUARANTINED staged GRN (staged_grn_detail.html). None if there's no
    such collision (nothing to correct), the staged GRN has already been
    posted itself, or the conflicting official GRN is already voided
    (nothing currently active to replace)."""
    staged_grn = conn.execute(
        "SELECT customer_id, external_grn_number, posted_grn_id FROM staged_grns WHERE staged_grn_id = ?",
        (staged_grn_id,),
    ).fetchone()
    if staged_grn is None or staged_grn["posted_grn_id"] is not None or not staged_grn["external_grn_number"]:
        return None
    return conn.execute(
        "SELECT * FROM grn_receipts WHERE grn_number = ? AND customer_id = ? AND voided = 0",
        (staged_grn["external_grn_number"], staged_grn["customer_id"]),
    ).fetchone()


def replace_posted_grn(conn, old_grn_id, corrected_staged_grn_id, reason):
    """Atomically replaces an already-posted official GRN with a
    corrected one from a re-uploaded, re-staged CSV (Phase 10). Never
    edits/deletes the original: voids old_grn_id (which also voids its
    SALE movement(s) and resolves any inventory_flags tied to them --
    see ingest.void_grn()), then posts corrected_staged_grn_id fresh
    through the SAME canonical posting path normal posting uses
    (_insert_official_grn()), linking the new official GRN back to the
    old one via grn_receipts.supersedes_grn_id. Both effects happen in
    ONE transaction the caller commits/rolls back -- if anything here
    raises, nothing written by this call persists once the caller rolls
    back (no partial correction is possible).

    Requires an explicit, non-empty reason -- never inferred from
    filename/timestamp/GRN number. Requires the corrected staged GRN to
    still pass every normal Phase 8 readiness rule except the one
    specific conflict this workflow exists to resolve (see
    _readiness_failures()'s expect_replacing_grn_id parameter) -- every
    other rule (PO exists/not voided/has source, product identity valid,
    quantities not over ordered, event date available, etc.) is
    unchanged.

    Row-locks, in this exact order, so two concurrent replacement
    attempts against the same GRN/PO can't both succeed: the old
    official GRN, the corrected staged GRN, then the official PO. A
    second call for the same old_grn_id blocks on the first lock until
    the first call's transaction ends, then fails cleanly (old_grn_id is
    no longer the PO's active GRN, or is already voided).

    Returns {"grn_id": <new official grn_id>, "grn_number": ...} on
    success. Raises CorrectionError for a malformed request or a
    data-integrity mismatch (the staged/official pair doesn't actually
    correspond, or old_grn_id isn't the PO's currently active GRN), or a
    plain ValueError listing every readiness reason if the corrected
    staged GRN isn't actually ready to post. Caller owns commit/rollback
    -- no hidden commits."""
    if not reason or not reason.strip():
        raise CorrectionError("Replacing a GRN needs a correction reason.")

    old_grn = conn.execute(
        "SELECT * FROM grn_receipts WHERE grn_id = ? FOR UPDATE", (old_grn_id,)
    ).fetchone()
    if old_grn is None:
        raise CorrectionError(f"Official GRN id {old_grn_id} does not exist.")
    if old_grn["voided"]:
        raise CorrectionError(f"GRN {old_grn['grn_number']} is already voided -- nothing active to replace.")

    staged_grn = conn.execute(
        """
        SELECT g.*, b.source_filename AS batch_source_filename
        FROM staged_grns g
        JOIN grn_import_batches b ON b.batch_id = g.batch_id
        WHERE g.staged_grn_id = ?
        FOR UPDATE OF g
        """,
        (corrected_staged_grn_id,),
    ).fetchone()
    if staged_grn is None:
        raise CorrectionError(f"Staged GRN id {corrected_staged_grn_id} does not exist.")
    if staged_grn["posted_grn_id"] is not None:
        raise CorrectionError(
            f"Staged GRN {corrected_staged_grn_id} has already been posted (as official GRN "
            f"{staged_grn['posted_grn_id']}) -- it can't also replace another GRN."
        )
    if staged_grn["customer_id"] != old_grn["customer_id"] or staged_grn["external_grn_number"] != old_grn["grn_number"]:
        raise CorrectionError(
            f"Staged GRN {corrected_staged_grn_id} (customer {staged_grn['customer_id']}, GRN "
            f"{staged_grn['external_grn_number']!r}) does not match official GRN {old_grn_id} (customer "
            f"{old_grn['customer_id']}, GRN {old_grn['grn_number']!r}) -- refusing to treat these as a "
            "correction pair."
        )

    po_id = staged_grn["official_po_id"]
    if po_id is None or old_grn["po_id"] != po_id:
        # Legacy GRNs (ingest.upsert_grn(), the PDF path) never set
        # grn_receipts.po_id -- Phase 10 correction only covers canonical
        # (CSV-posted) GRNs, so an old_grn without a matching po_id can
        # never be a valid replacement target here.
        raise CorrectionError(
            f"Official GRN {old_grn_id} and staged GRN {corrected_staged_grn_id} are not matched to the same "
            "official PO (or the official GRN has no canonical PO linkage) -- refusing to replace."
        )

    po_by_id = _lock_purchase_orders(conn, [po_id])
    po = po_by_id.get(po_id)
    if po is None:
        raise CorrectionError(f"Official PO id {po_id} no longer exists.")

    active = conn.execute(
        "SELECT grn_id FROM grn_receipts WHERE po_id = ? AND voided = 0", (po_id,)
    ).fetchone()
    if active is None or active["grn_id"] != old_grn_id:
        raise CorrectionError(
            f"Official GRN {old_grn_id} is not the currently active GRN for PO {po['po_number']} -- refusing "
            "to replace a GRN that isn't the one actually in effect."
        )

    lines = _staged_lines(conn, corrected_staged_grn_id)
    reasons = _readiness_failures(conn, staged_grn, lines, po_by_id, expect_replacing_grn_id=old_grn_id)
    if reasons:
        raise ValueError(
            f"Corrected staged GRN {corrected_staged_grn_id} is not ready to post: " + "; ".join(reasons)
        )

    void_grn(conn, old_grn["grn_number"], reason)
    new_grn_id = _insert_official_grn(conn, dict(staged_grn), lines, po, supersedes_grn_id=old_grn_id)

    return {"grn_id": new_grn_id, "grn_number": staged_grn["external_grn_number"]}

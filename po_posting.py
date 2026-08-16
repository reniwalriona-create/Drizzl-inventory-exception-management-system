"""
PO posting (Phase 5) -- turns a READY staged PO (Phase 3/4) into an
official purchase_orders/po_line_items record.

This is the first phase where staged data creates official business
records. Staging rows are never deleted or edited by this module -- they
remain the permanent audit source, linked to what they produced via
staged_purchase_orders.posted_po_id/posted_at and
staged_po_lines.posted_line_item_id.

Transaction ownership: like po_csv_staging.py, this module does NOT call
conn.commit()/conn.rollback() -- the caller (app.py's post route) owns the
transaction so a whole selected posting is atomic.

Product identity: every line this module creates copies product_id
straight from the already-reviewed staged_po_lines snapshot (never
re-resolves the customer SKU -- that mapping was already reviewed at
staging time). item_code/item_desc are mirrored from external_sku/
external_sku_description on every posted line specifically so the
existing item_code-keyed commitment/GRN-matching code in reconcile.py
keeps matching without modification -- see PROJECT_HANDOFF.md and
reconcile.committed_quantity(). product_id itself is not used as a join
key by any of that legacy code yet; it's additive canonical identity.

Never uses ingest.py's upsert_po() -- that function is for the legacy
PDF/SKU architecture (deletes+recreates line items, creates legacy
`products` rows via _ensure_product(), owns its own commit). Posting a
staged CSV PO is always a plain INSERT of a brand-new official record,
never an UPSERT/replace of an existing one.
"""


class PostingError(Exception):
    """Raised for a malformed request itself (unknown staged_po_id,
    cross-batch id, empty selection) -- the same class of problem
    po_csv_staging.assign_source_location() raises ValueError for. Not
    raised for ordinary 'this PO isn't ready to post' business outcomes --
    those come back in post_staged_purchase_orders()'s `rejected` list."""


def _load_selection(conn, batch_id, staged_po_ids):
    staged_po_ids = list(staged_po_ids)
    if not staged_po_ids:
        raise PostingError("No staged purchase orders were selected.")

    placeholders = ",".join(["?"] * len(staged_po_ids))
    rows = conn.execute(
        f"""
        SELECT p.*, b.source_filename AS batch_source_filename
        FROM staged_purchase_orders p
        JOIN po_import_batches b ON b.batch_id = p.batch_id
        WHERE p.staged_po_id IN ({placeholders})
        """,
        tuple(staged_po_ids),
    ).fetchall()
    found = {r["staged_po_id"]: dict(r) for r in rows}

    missing = set(staged_po_ids) - set(found)
    if missing:
        raise PostingError(f"Staged PO id(s) {sorted(missing)} do not exist.")
    wrong_batch = {sid for sid, po in found.items() if po["batch_id"] != batch_id}
    if wrong_batch:
        raise PostingError(f"Staged PO id(s) {sorted(wrong_batch)} do not belong to this batch.")

    # Preserve caller-supplied order.
    return [found[sid] for sid in staged_po_ids]


def _readiness_failures(conn, staged_po, lines):
    """Independently reconstructs whether this staged PO is safe to post,
    ignoring whatever the browser/UI claims. Returns a list of
    human-readable reason strings -- empty means ready."""
    reasons = []

    if staged_po["validation_status"] == "blocked":
        reasons.append("staging validation is blocked (unresolved data/product problem).")
    if staged_po["source_location_id"] is None:
        reasons.append("no Drizzl source warehouse has been assigned.")
    elif conn.execute(
        "SELECT 1 FROM locations WHERE id = ?", (staged_po["source_location_id"],)
    ).fetchone() is None:
        reasons.append("its assigned Drizzl source warehouse no longer exists.")

    if conn.execute("SELECT 1 FROM customers WHERE id = ?", (staged_po["customer_id"],)).fetchone() is None:
        reasons.append("its customer no longer exists.")
    if not staged_po["external_po_number"]:
        reasons.append("it has no external PO number.")

    if not lines:
        reasons.append("it has no staged line items.")
    for line in lines:
        if line["validation_status"] != "valid":
            reasons.append(f"line {line['staged_line_id']} (SKU {line['external_sku']}) failed staging validation.")
        elif line["product_id"] is None:
            reasons.append(f"line {line['staged_line_id']} (SKU {line['external_sku']}) has no resolved Master Product.")
        elif conn.execute(
            "SELECT 1 FROM master_products WHERE product_id = ?", (line["product_id"],)
        ).fetchone() is None:
            reasons.append(f"line {line['staged_line_id']}'s Master Product no longer exists.")

    return reasons


def _conflict_failures(conn, staged_po):
    """Pre-insert conflict detection -- translates what would otherwise be
    a raw UNIQUE-violation exception (or, worse, a silent overwrite) into a
    clear business reason. Returns a list of reason strings."""
    reasons = []
    existing = conn.execute(
        "SELECT po_id, customer_id FROM purchase_orders WHERE po_number = ?",
        (staged_po["external_po_number"],),
    ).fetchone()
    if existing is not None:
        if existing["customer_id"] == staged_po["customer_id"]:
            reasons.append(
                f"an official PO {staged_po['external_po_number']!r} already exists (po_id "
                f"{existing['po_id']}) and is not linked to this staged record -- looks like a "
                "duplicate/revised staging of an already-posted PO. Posting does not overwrite "
                "an existing official PO; this needs the (not-yet-built) duplicate/revision "
                "review workflow."
            )
        else:
            reasons.append(
                f"PO number {staged_po['external_po_number']!r} is already used by another "
                "customer's official PO. purchase_orders.po_number is still globally unique "
                "(temporary Phase 2 compatibility scaffolding) until the remaining child tables "
                "migrate to po_id -- this is a known, temporary limitation, not a bug."
            )
    return reasons


def get_posting_conflicts(conn, batch_id, staged_po_ids):
    """Read-only preview of what post_staged_purchase_orders() would
    reject and why, without writing anything. Returns a dict
    {staged_po_id: [reason, ...]} -- only entries that would actually be
    rejected are included; already-posted and cleanly-ready staged POs are
    omitted."""
    problems = {}
    for staged_po in _load_selection(conn, batch_id, staged_po_ids):
        if staged_po["posted_po_id"] is not None:
            continue
        lines = _staged_lines(conn, staged_po["staged_po_id"])
        reasons = _readiness_failures(conn, staged_po, lines)
        if not reasons:
            reasons = _conflict_failures(conn, staged_po)
        if reasons:
            problems[staged_po["staged_po_id"]] = reasons
    return problems


def _staged_lines(conn, staged_po_id):
    return conn.execute(
        "SELECT * FROM staged_po_lines WHERE staged_po_id = ? ORDER BY staged_line_id",
        (staged_po_id,),
    ).fetchall()


def _insert_official_po(conn, staged_po, lines):
    destination_facility_name = staged_po["destination_facility_name"]
    expected_delivery_date = staged_po["expected_delivery_date"]
    po_expiry_date = staged_po["po_expiry_date"]

    po_row = conn.execute(
        """
        INSERT INTO purchase_orders
            (po_number, customer_id, expected_delivery_date, po_expiry_date,
             vendor_name, facility_name, grand_total, source_file,
             destination_facility_id, destination_facility_name, destination_city,
             external_po_created_at, external_po_modified_at, external_status,
             supplier_code, source_location_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING po_id
        """,
        (
            staged_po["external_po_number"], staged_po["customer_id"],
            expected_delivery_date.isoformat() if expected_delivery_date else None,
            po_expiry_date.isoformat() if po_expiry_date else None,
            staged_po["vendor_name"], destination_facility_name, staged_po["po_amount"],
            staged_po["batch_source_filename"],
            staged_po["destination_facility_id"], destination_facility_name, staged_po["destination_city"],
            staged_po["po_created_at"], staged_po["po_modified_at"], staged_po["external_status"],
            staged_po["supplier_code"], staged_po["source_location_id"],
        ),
    ).fetchone()
    po_id = po_row["po_id"]
    po_number = staged_po["external_po_number"]

    for line in lines:
        line_row = conn.execute(
            """
            INSERT INTO po_line_items
                (po_number, sno, item_code, item_desc, qty, mrp, unit_base_cost,
                 taxable_value, total, product_id, external_sku,
                 external_sku_description, external_tax_amount)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                po_number, str(line["source_row_number"]), line["external_sku"], line["external_sku_description"],
                line["ordered_qty"], line["mrp"], line["unit_based_cost"],
                line["line_value_without_tax"], line["line_value_with_tax"], line["product_id"],
                line["external_sku"], line["external_sku_description"], line["tax"],
            ),
        ).fetchone()
        conn.execute(
            "UPDATE staged_po_lines SET posted_line_item_id = ? WHERE staged_line_id = ?",
            (line_row["id"], line["staged_line_id"]),
        )

    conn.execute(
        "UPDATE staged_purchase_orders SET posted_po_id = ?, posted_at = CURRENT_TIMESTAMP WHERE staged_po_id = ?",
        (po_id, staged_po["staged_po_id"]),
    )
    return po_id


def post_staged_purchase_orders(conn, batch_id, staged_po_ids):
    """Posts every staged PO in staged_po_ids (all must belong to
    batch_id) into the official ledger. All-or-nothing across the whole
    selection: if ANY not-yet-posted staged PO in the selection isn't
    ready or conflicts with an existing official PO, NOTHING is written
    for ANY of them (see PROJECT_HANDOFF.md). Already-posted staged POs in
    the selection are a harmless no-op and never block the rest.

    Returns:
        {
          "posted": [{"staged_po_id", "po_number", "po_id"}, ...],
          "already_posted": [{"staged_po_id", "po_number", "po_id"}, ...],
          "rejected": {staged_po_id: [reason, ...]},   # empty dict if nothing rejected
        }
    "rejected" non-empty means "posted" is always empty for this call --
    nothing was written. Raises PostingError only for a malformed request
    (see that class's docstring). Caller owns commit/rollback."""
    selection = _load_selection(conn, batch_id, staged_po_ids)

    already_posted = []
    to_post = []
    for staged_po in selection:
        if staged_po["posted_po_id"] is not None:
            already_posted.append(staged_po)
        else:
            to_post.append(staged_po)

    rejected = {}
    lines_by_staged_po = {}
    for staged_po in to_post:
        lines = _staged_lines(conn, staged_po["staged_po_id"])
        lines_by_staged_po[staged_po["staged_po_id"]] = lines
        reasons = _readiness_failures(conn, staged_po, lines)
        if not reasons:
            reasons = _conflict_failures(conn, staged_po)
        if reasons:
            rejected[staged_po["staged_po_id"]] = reasons

    if rejected:
        return {"posted": [], "already_posted": [], "rejected": rejected}

    posted = []
    for staged_po in to_post:
        po_id = _insert_official_po(conn, staged_po, lines_by_staged_po[staged_po["staged_po_id"]])
        posted.append({
            "staged_po_id": staged_po["staged_po_id"],
            "po_number": staged_po["external_po_number"],
            "po_id": po_id,
        })

    already_posted_out = [
        {
            "staged_po_id": p["staged_po_id"],
            "po_number": p["external_po_number"],
            "po_id": p["posted_po_id"],
        }
        for p in already_posted
    ]
    return {"posted": posted, "already_posted": already_posted_out, "rejected": {}}

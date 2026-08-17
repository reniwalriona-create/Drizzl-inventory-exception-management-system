"""
Reconciliation and reporting, built on top of the inventory_movements ledger.
"""
from db import get_connection


def stock_by_location(conn, location=None, sku_code=None):
    """Current stock per SKU per location, derived from the ledger itself
    -- not stored anywhere, always computed fresh from history. Also
    attaches two derived columns per row:
      qty_committed   = inventory reserved against an open, unresolved PO
                        allocated to this location (see below for which
                        identity space this is looked up in).
      qty_uncommitted = qty_on_hand - qty_committed -- what's actually
                        free to promise/move without eating into an open
                        PO's reserved stock.
    A PO never creates a ledger movement (an order still isn't a stock
    event -- qty_on_hand is exactly the same as before this existed), so
    a SKU/product can have real committed quantity at a location with
    zero physical stock so far; a synthetic qty_on_hand=0 row is added
    for that case so the commitment isn't invisible just because
    nothing's arrived yet. Returns plain dicts (not sqlite3.Row) since
    these two columns are computed in Python, not SQL.

    Dual identity (Phase 8): product_id, not the sku_code string, is the
    true internal inventory identity for a canonical movement --
    canonical rows (product_id IS NOT NULL) are grouped by product_id and
    matched against committed_by_location_product(); legacy/manual rows
    (product_id IS NULL) keep grouping by the sku_code string and match
    against committed_by_location_sku(), exactly as before Phase 8. The
    SQL groups by (product_id, sku_code) together -- safe and never
    ambiguous, because product_id is NULL for every legacy row and a
    real, non-NULL value for every canonical row, so a canonical group
    and a legacy group can never collide regardless of what either
    sku_code string happens to contain. The legacy-products description
    join is explicitly restricted to product_id IS NULL rows so a
    coincidental sku_code/barcode text match could never pull in the
    wrong description either."""
    query = """
        SELECT l.name AS location, m.product_id, m.sku_code,
               COALESCE(MAX(mp.product_name), MAX(p.sku_desc)) AS sku_desc,
               MAX(mp.barcode) AS barcode,
               SUM(delta) AS qty_on_hand
        FROM (
            SELECT location_to_id AS location_id, sku_code, product_id, quantity AS delta
            FROM inventory_movements WHERE location_to_id IS NOT NULL AND voided = 0
            UNION ALL
            SELECT location_from_id AS location_id, sku_code, product_id, -quantity AS delta
            FROM inventory_movements WHERE location_from_id IS NOT NULL AND voided = 0
        ) m
        JOIN locations l ON l.id = m.location_id
        LEFT JOIN products p ON p.sku_code = m.sku_code AND m.product_id IS NULL
        LEFT JOIN master_products mp ON mp.product_id = m.product_id
    """
    conditions, params = [], []
    if location:
        conditions.append("l.name = ?")
        params.append(location)
    if sku_code:
        conditions.append("m.sku_code = ?")
        params.append(sku_code)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " GROUP BY l.name, m.product_id, m.sku_code HAVING SUM(delta) != 0 ORDER BY l.name, qty_on_hand DESC"
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    committed_sku_map = committed_by_location_sku(conn)
    committed_product_map = committed_by_location_product(conn)
    seen_product = {(r["location"], r["product_id"]) for r in rows if r["product_id"] is not None}
    seen_sku = {(r["location"], r["sku_code"]) for r in rows if r["product_id"] is None}
    for r in rows:
        if r["product_id"] is not None:
            c = committed_product_map.get((r["location"], r["product_id"]), 0)
        else:
            c = committed_sku_map.get((r["location"], r["sku_code"]), 0)
        r["qty_committed"] = c
        r["qty_uncommitted"] = r["qty_on_hand"] - c

    master_desc = None
    for (loc, product_id), c in committed_product_map.items():
        if location and loc != location:
            continue
        if sku_code:  # sku_code filter is legacy-identity space only, see docstring
            continue
        if (loc, product_id) in seen_product:
            continue
        if master_desc is None:
            master_desc = {
                p["product_id"]: (p["barcode"], p["product_name"])
                for p in conn.execute("SELECT product_id, barcode, product_name FROM master_products").fetchall()
            }
        barcode, product_name = master_desc.get(product_id, (None, None))
        rows.append({
            "location": loc, "product_id": product_id, "sku_code": barcode, "sku_desc": product_name,
            "barcode": barcode, "qty_on_hand": 0, "qty_committed": c, "qty_uncommitted": 0 - c,
        })

    product_desc = None
    for (loc, sku), c in committed_sku_map.items():
        if location and loc != location:
            continue
        if sku_code and sku != sku_code:
            continue
        if (loc, sku) in seen_sku:
            continue
        if product_desc is None:
            product_desc = {p["sku_code"]: p["sku_desc"] for p in conn.execute("SELECT sku_code, sku_desc FROM products").fetchall()}
        rows.append({
            "location": loc, "product_id": None, "sku_code": sku, "sku_desc": product_desc.get(sku),
            "barcode": None, "qty_on_hand": 0, "qty_committed": c, "qty_uncommitted": 0 - c,
        })

    rows.sort(key=lambda r: (r["location"], -r["qty_on_hand"]))
    return rows


def current_balance(conn, location_name, sku_code):
    """Current on-hand quantity for exactly one SKU at exactly one
    location. Unlike stock_by_location(), this does NOT hide an
    exact-zero balance (its `HAVING qty_on_hand != 0` would silently
    return nothing for a genuinely-zero location/SKU) -- needed by the
    negative-inventory check in app.py's new_movement() and
    ingest.py's upsert_grn(), which need to know "how much is actually
    there," not just "is there anything.\""""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(delta), 0) AS qty_on_hand FROM (
            SELECT m.quantity AS delta
            FROM inventory_movements m JOIN locations l ON l.id = m.location_to_id
            WHERE l.name = ? AND m.sku_code = ? AND m.voided = 0
            UNION ALL
            SELECT -m.quantity AS delta
            FROM inventory_movements m JOIN locations l ON l.id = m.location_from_id
            WHERE l.name = ? AND m.sku_code = ? AND m.voided = 0
        )
        """,
        (location_name, sku_code, location_name, sku_code),
    ).fetchone()
    return row["qty_on_hand"]


def current_balance_by_product(conn, location_id, product_id):
    """Current on-hand quantity for exactly one canonical Master Product
    at exactly one Drizzl location -- the Phase 8 canonical counterpart
    to current_balance(), used by grn_posting.py instead of it. Operates
    strictly on product_id + location_id (never a SKU string of any
    kind), and only ever sums movements that themselves carry that same
    product_id -- a legacy/manual movement (product_id IS NULL) never
    contributes here, and this never contributes to current_balance()'s
    legacy sku_code-keyed total either. The two identity spaces are
    separate pools during the transition (see PROJECT_HANDOFF.md) -- not
    an oversight, a deliberate refusal to guess a legacy-SKU-to-
    product_id mapping retroactively. Does NOT hide an exact-zero
    balance, same reasoning as current_balance()."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(delta), 0) AS qty_on_hand FROM (
            SELECT quantity AS delta FROM inventory_movements
            WHERE location_to_id = ? AND product_id = ? AND voided = 0
            UNION ALL
            SELECT -quantity AS delta FROM inventory_movements
            WHERE location_from_id = ? AND product_id = ? AND voided = 0
        )
        """,
        (location_id, product_id, location_id, product_id),
    ).fetchone()
    return row["qty_on_hand"]


def committed_quantity(conn):
    """Every still-open (unresolved) PO line's committed quantity --
    reserved against that PO until a GRN resolves it.

    Returns one row per still-committed PO line: po_number, sku_code,
    sku_desc, qty, source_location (the PO's assigned Drizzl location
    name, or None if not yet allocated -- see purchase_orders.source_
    location_id in schema.sql), plus (Phase 5) product_id, barcode,
    product_name, external_sku.

    sku_code/sku_desc identity note: sku_code is deliberately STILL
    item_code (mirrored external_sku for a Phase-5-posted canonical line,
    same as always for a legacy PDF line) -- it remains the join key every
    downstream consumer relies on (committed_by_location_sku() -> the
    manual-movement commitment-shortfall check via committed_at_location(),
    which only ever knows a legacy SKU string typed into the form, never a
    product_id) and those all key against inventory_movements.sku_code,
    which itself stays in document-SKU space for legacy/manual movements.
    Switching sku_code to the master barcode here would silently break
    that check. sku_desc IS safe to upgrade, because it's purely
    descriptive and never used as a join/lookup key anywhere: it now
    prefers the canonical master_products.product_name when a line has a
    product_id, falling back to the legacy item_desc otherwise. For
    canonical (product_id-identified) physical stock, see
    committed_by_location_product() instead -- stock_by_location() uses
    that one, product_id-keyed, to merge commitment onto canonical rows;
    committed_by_location_sku() stays reserved for the legacy/manual path.
    See PROJECT_HANDOFF.md.

    Release rule, two branches (Phase 8):
      canonical PO line (product_id IS NOT NULL): the moment ANY
        non-voided OFFICIAL canonical GRN exists for this PO's po_id --
        regardless of which SKUs that GRN's own lines cover -- the
        ENTIRE PO's commitment closes, header-level, not per-line. A
        product completely absent from the GRN still releases; the gap
        becomes a discrepancy (grn_csv_staging.get_grn_po_comparison()),
        never a re-armed commitment. If the resolving GRN is later
        voided, the commitment correctly reappears (gr.voided = 0 below).
      legacy PO line (product_id IS NULL): unchanged, original per-
        (po_number, sku_code) GRN-line-match behavior -- deliberately NOT
        `ordered - received`, the moment any non-voided GRN line exists
        for that (po_number, sku_code) pair, the line's commitment drops
        to 0 for good; a shortfall becomes a discrepancy (see
        po_vs_received_shortfall()), not a remaining PO commitment."""
    return conn.execute(
        """
        SELECT
            p.po_number, p.item_code AS sku_code,
            COALESCE(mp.product_name, p.item_desc) AS sku_desc, p.qty,
            l.name AS source_location,
            p.product_id, mp.barcode, mp.product_name, p.external_sku
        FROM po_line_items p
        JOIN purchase_orders po ON po.po_number = p.po_number AND po.voided = 0
        LEFT JOIN locations l ON l.id = po.source_location_id
        LEFT JOIN master_products mp ON mp.product_id = p.product_id
        WHERE
            CASE WHEN p.product_id IS NOT NULL THEN
                NOT EXISTS (
                    SELECT 1 FROM grn_receipts gr
                    WHERE gr.po_id = po.po_id AND gr.voided = 0
                )
            ELSE
                NOT EXISTS (
                    SELECT 1
                    FROM grn_receipts gr
                    JOIN grn_line_items gli ON gli.grn_id = gr.grn_id
                    WHERE gr.po_number = p.po_number
                      AND gr.voided = 0
                      AND gli.sku_code = p.item_code
                )
            END
        ORDER BY p.po_number, p.item_code
        """
    ).fetchall()


def committed_by_location_sku(conn):
    """committed_quantity(), summed by (Drizzl source location, SKU) --
    only for PO lines that actually have a source location assigned.
    Feeds the commitment-shortfall warning in app.py's new_movement(),
    which only ever knows a legacy SKU string typed into the manual-
    movement form, never a product_id -- this stays sku_code-keyed for
    exactly that reason, unchanged by Phase 8. For canonical
    (product_id-identified) physical stock, stock_by_location() uses
    committed_by_location_product() instead, not this one. Returns a
    plain {(location, sku_code): qty} dict."""
    by_key = {}
    for r in committed_quantity(conn):
        if not r["source_location"]:
            continue
        key = (r["source_location"], r["sku_code"])
        by_key[key] = by_key.get(key, 0) + r["qty"]
    return by_key


def committed_by_location_product(conn):
    """committed_quantity(), summed by (Drizzl source location,
    product_id) -- companion to committed_by_location_sku() above, for
    CANONICAL PO lines only (product_id IS NOT NULL). This is what
    stock_by_location() uses to merge commitment onto a canonical
    (product_id-grouped) physical stock row, so that merge never depends
    on sku_code string equality -- product_id is the true internal
    inventory identity (Phase 8, see PROJECT_HANDOFF.md). Returns a plain
    {(location, product_id): qty} dict."""
    by_key = {}
    for r in committed_quantity(conn):
        if not r["source_location"] or r["product_id"] is None:
            continue
        key = (r["source_location"], r["product_id"])
        by_key[key] = by_key.get(key, 0) + r["qty"]
    return by_key


def committed_at_location(conn, location_name, sku_code):
    """Committed quantity for exactly one (location, SKU) pair -- the
    lookup app.py's new_movement() needs for the commitment-shortfall
    check. 0 if nothing is committed there (including "no PO has ever
    been allocated to this location for this SKU")."""
    return committed_by_location_sku(conn).get((location_name, sku_code), 0)


def unallocated_commitments(conn):
    """committed_quantity(), summed by SKU, for PO lines with NO Drizzl
    source location assigned yet. Shown as its own dashboard total
    rather than guessed into any location's Committed column -- assigns
    via ingest.py's assign_po_source_location()."""
    by_sku = {}
    desc_by_sku = {}
    for r in committed_quantity(conn):
        if r["source_location"]:
            continue
        by_sku[r["sku_code"]] = by_sku.get(r["sku_code"], 0) + r["qty"]
        desc_by_sku[r["sku_code"]] = r["sku_desc"]
    return sorted(
        ({"sku_code": sku, "sku_desc": desc_by_sku[sku], "qty": qty} for sku, qty in by_sku.items()),
        key=lambda r: -r["qty"],
    )


def resolve_grn_source_location(conn, grn_number):
    """The Drizzl location a GRN's sale movement(s) should come from --
    the GRN's own source_location_id if explicitly set (assign_grn_
    source_location()), else its linked PO's source_location_id, else
    None (never guessed/defaulted -- see ingest.py's upsert_grn(), which
    refuses to create a sale movement when this returns None). Used both
    at initial GRN upload and when retroactively backfilling pending
    sales after a location gets assigned later."""
    row = conn.execute(
        """
        SELECT
            COALESCE(gl.name, pl.name) AS source_location
        FROM grn_receipts gr
        LEFT JOIN locations gl ON gl.id = gr.source_location_id
        LEFT JOIN purchase_orders po ON po.po_number = gr.po_number AND po.voided = 0
        LEFT JOIN locations pl ON pl.id = po.source_location_id
        WHERE gr.grn_number = ? AND gr.voided = 0
        """,
        (grn_number,),
    ).fetchone()
    return row["source_location"] if row else None


def stock_by_flavor(conn, location=None, sku_code=None):
    """Same as stock_by_location() but grouped by flavor (see
    _flavor_name()) instead of raw SKU code -- feeds the stock-by-location
    chart, which stacks flavors within each location bar rather than
    individual SKUs. This sums inventory_movements.quantity directly
    across every SKU variant of a flavor (e.g. a single can and a 6-pack
    would both just add their raw quantity). That's exact today because
    only single-can SKUs have ever actually been used in a real
    movement -- confirmed 2026-08-13, no "Pack of 6" SKU has shipped yet.
    If one ever does, check whether its logged quantity means "cans" or
    "packs" before trusting this sum (see po_quantity_by_flavor()'s
    docstring for the same caveat on the PO side)."""
    rows = stock_by_location(conn, location=location, sku_code=sku_code)
    by_key = {}
    for r in rows:
        flavor = _flavor_name(r["sku_desc"]) or r["sku_code"]
        key = (r["location"], flavor)
        by_key[key] = by_key.get(key, 0) + r["qty_on_hand"]
    return [
        {"location": loc, "flavor": flavor, "qty_on_hand": qty}
        for (loc, flavor), qty in by_key.items()
        if qty != 0
    ]


def damaged_units_by_sku(conn, sku_code=None, location=None):
    """The running damage counter -- per your own framing, if this
    climbs over time that's a real problem to chase, even if any single
    write-off looks small. `location` filters to where the damaged stock
    left from (loss movements always have a From location)."""
    query = """
        SELECT m.sku_code, MAX(p.sku_desc) AS sku_desc, SUM(m.quantity) AS total_damaged, COUNT(*) AS n_events
        FROM inventory_movements m
        LEFT JOIN products p ON p.sku_code = m.sku_code
        LEFT JOIN locations lf ON lf.id = m.location_from_id
        WHERE m.movement_type = 'loss' AND m.voided = 0
    """
    params = []
    if sku_code:
        query += " AND m.sku_code = ?"
        params.append(sku_code)
    if location:
        query += " AND lf.name = ?"
        params.append(location)
    query += " GROUP BY m.sku_code ORDER BY total_damaged DESC"
    return conn.execute(query, params).fetchall()


def damaged_units_by_cause(conn, sku_code=None, location=None):
    """Same counter, grouped by who/what was at fault (the Discrepancy
    Note's Remarks field, e.g. 'DP WORLD-DAMAGE') -- useful for deciding
    who to push back on if a pattern shows up."""
    query = """
        SELECT COALESCE(m.notes, 'unspecified') AS cause, SUM(m.quantity) AS total_damaged, COUNT(*) AS n_events
        FROM inventory_movements m
        LEFT JOIN locations lf ON lf.id = m.location_from_id
        WHERE m.movement_type = 'loss' AND m.voided = 0
    """
    params = []
    if sku_code:
        query += " AND m.sku_code = ?"
        params.append(sku_code)
    if location:
        query += " AND lf.name = ?"
        params.append(location)
    query += " GROUP BY cause ORDER BY total_damaged DESC"
    return conn.execute(query, params).fetchall()


def po_vs_received_shortfall(conn, sku_code=None):
    """LEGACY-ONLY (Phase 9): per PO line item on a legacy PDF-sourced PO
    (product_id IS NULL), across all GRNs tied to that PO, has the full
    ordered quantity been received (sold) yet? Only meaningful for POs
    whose PDF has actually been parsed (stub POs have no line items).
    No location filter -- a PO/GRN isn't tied to a specific Drizzl
    location, it's what Scootsy received.

    Restricted to product_id IS NULL on BOTH sides (the PO line and the
    matched GRN line) so this can never overlap with a canonical
    (Phase 5/8, product_id IS NOT NULL) PO/GRN -- canonical discrepancy
    reporting is official_po_grn_discrepancies()/official_discrepancies()
    only, which key on (product_id, external_sku), not this function's
    sku_code text match. Before Phase 9 this function silently covered
    canonical POs too (item_code mirrors external_sku by construction),
    which meant two different algorithms could evaluate the same
    canonical PO -- the sku_code-string one here, and the product_id-keyed
    one in official_po_grn_discrepancies(). That overlap is now closed:
    a canonical PO line never appears in this report's output, full stop."""
    query = """
        SELECT
            p.item_code AS sku_code,
            MAX(p.item_desc) AS sku_desc,
            p.po_number,
            MAX(p.qty) AS ordered_qty,
            COALESCE(SUM(CASE WHEN g.voided = 0 THEN gli.received_qty END), 0) AS received_qty,
            MAX(p.qty) - COALESCE(SUM(CASE WHEN g.voided = 0 THEN gli.received_qty END), 0) AS shortfall
        FROM po_line_items p
        JOIN purchase_orders po ON po.po_number = p.po_number AND po.voided = 0
        LEFT JOIN grn_receipts g ON g.po_number = p.po_number
        LEFT JOIN grn_line_items gli
            ON gli.grn_number = g.grn_number AND gli.sku_code = p.item_code AND gli.product_id IS NULL
        WHERE p.product_id IS NULL
    """
    params = []
    if sku_code:
        query += " AND p.item_code = ?"
        params.append(sku_code)
    query += (
        " GROUP BY p.po_number, p.item_code"
        " HAVING MAX(p.qty) - COALESCE(SUM(CASE WHEN g.voided = 0 THEN gli.received_qty END), 0) != 0"
        " ORDER BY shortfall DESC"
    )
    return conn.execute(query, params).fetchall()


def official_po_grn_discrepancies(conn, po_number):
    """Canonical PO-vs-GRN discrepancy comparison for one official,
    non-voided PO (Phase 9). Replaces the old grn_discrepancies(), which
    depended on the Discrepancy Note PDF workflow removed in Phase 9 --
    the system now has authoritative ordered/received data of its own
    (official purchase_orders/po_line_items vs. grn_receipts/
    grn_line_items), so a separately-uploaded document is no longer
    needed to know there's a shortfall, only to know *why* (out of scope
    here -- see PROJECT_HANDOFF.md's "what's left" list for the future
    PR/DN workflow).

    Operates strictly on OFFICIAL posted records, never staging tables --
    once a PO/GRN is posted, these are the business truth (staging stays
    audit lineage; see grn_csv_staging.get_grn_po_comparison() for the
    pre-posting equivalent this mirrors). Grouping key is
    (product_id, external_sku), never a bare SKU string -- product_id is
    the true internal inventory identity (Phase 8). Only CANONICAL PO
    lines (product_id IS NOT NULL) are considered; a legacy PDF-sourced
    PO line has no product_id and isn't covered here -- see
    po_vs_received_shortfall() for that path, kept as the still-needed
    legacy fallback since PO/GRN PDF upload remains active.

    Returns [] if the PO doesn't exist, is voided, has no canonical
    lines, or has no non-voided official GRN posted against it yet --
    matches the spec's "for a posted canonical GRN" framing: an
    unfulfilled PO isn't a discrepancy, it's just still open (see
    committed_quantity() for the commitment side of that, unaffected by
    this function).

    computed_shortfall_qty = ordered_qty - received_qty, always -- never
    derived from source_dn_quantity, which is carried through purely as a
    separate audit fact (see grn_line_items.source_dn_quantity in
    schema_postgres.sql) and must never be confused with the computed
    shortfall. A product on the PO but completely absent from every GRN
    line shows received_qty=0, shortfall=the full ordered quantity.
    Multi-lot GRN lines for the same (product_id, external_sku) are
    summed before comparing, so a split-lot receipt isn't mistaken for a
    partial one.

    status is COMPLETE (shortfall == 0) or SHORT (shortfall > 0) -- no
    richer states yet (no resolved/approved/DN-verified/financially
    settled), since only the operational quantity difference is known at
    this phase."""
    po = conn.execute(
        "SELECT po_id FROM purchase_orders WHERE po_number = ? AND voided = 0", (po_number,)
    ).fetchone()
    if po is None:
        return []
    has_grn = conn.execute(
        "SELECT 1 FROM grn_receipts WHERE po_id = ? AND voided = 0 LIMIT 1", (po["po_id"],)
    ).fetchone()
    if not has_grn:
        return []

    po_rows = conn.execute(
        """
        SELECT pli.product_id, pli.external_sku, MAX(mp.barcode) AS barcode, MAX(mp.product_name) AS product_name,
               SUM(pli.qty) AS ordered_qty
        FROM po_line_items pli
        LEFT JOIN master_products mp ON mp.product_id = pli.product_id
        WHERE pli.po_number = ? AND pli.product_id IS NOT NULL
        GROUP BY pli.product_id, pli.external_sku
        """,
        (po_number,),
    ).fetchall()

    grn_rows = conn.execute(
        """
        SELECT gli.product_id, gli.external_sku,
               SUM(gli.received_qty) AS received_qty,
               SUM(COALESCE(gli.source_dn_quantity, 0)) AS source_dn_quantity
        FROM grn_line_items gli
        JOIN grn_receipts gr ON gr.grn_id = gli.grn_id
        WHERE gr.po_id = ? AND gr.voided = 0 AND gli.product_id IS NOT NULL
        GROUP BY gli.product_id, gli.external_sku
        """,
        (po["po_id"],),
    ).fetchall()
    grn_by_key = {(r["product_id"], r["external_sku"]): r for r in grn_rows}

    results = []
    for r in po_rows:
        key = (r["product_id"], r["external_sku"])
        g = grn_by_key.get(key)
        received_qty = g["received_qty"] if g else 0
        source_dn_quantity = g["source_dn_quantity"] if g else None
        ordered_qty = r["ordered_qty"] or 0
        shortfall = ordered_qty - received_qty
        results.append({
            "po_number": po_number,
            "product_id": r["product_id"],
            "external_sku": r["external_sku"],
            "barcode": r["barcode"],
            "product_name": r["product_name"],
            "ordered_qty": ordered_qty,
            "received_qty": received_qty,
            "computed_shortfall_qty": shortfall,
            "source_dn_quantity": source_dn_quantity,
            "status": "COMPLETE" if shortfall == 0 else "SHORT",
        })
    results.sort(key=lambda r: (r["product_name"] or "", r["external_sku"] or ""))
    return results


def official_discrepancies(conn, sku_code=None):
    """official_po_grn_discrepancies() across every posted canonical PO
    that has a non-voided official GRN against it -- feeds the
    dashboard's discrepancy panel. sku_code filters on external_sku (the
    dashboard's existing SKU filter box happens to line up with
    external_sku for canonical lines by construction). Covers exactly
    the PO lines po_vs_received_shortfall() now deliberately excludes
    (product_id IS NOT NULL) -- the two never overlap on the same PO
    line, see po_vs_received_shortfall()'s docstring for why that
    separation matters."""
    po_numbers = [
        r["po_number"] for r in conn.execute(
            """
            SELECT DISTINCT po.po_number
            FROM purchase_orders po
            JOIN grn_receipts gr ON gr.po_id = po.po_id AND gr.voided = 0
            WHERE po.voided = 0
            ORDER BY po.po_number
            """
        ).fetchall()
    ]
    rows = []
    for po_number in po_numbers:
        rows.extend(official_po_grn_discrepancies(conn, po_number))
    if sku_code:
        rows = [r for r in rows if r["external_sku"] == sku_code]
    rows.sort(key=lambda r: (-r["computed_shortfall_qty"], r["po_number"]))
    return rows


def po_quantity_by_facility(conn, sku_code=None):
    """Total ordered quantity (summed across every SKU on the PO) per
    Scootsy receiving facility -- feeds the "PO quantity by warehouse"
    chart. Facility is the Scootsy warehouse a PO shipped to (e.g.
    Hyderabad, Mumbai), not one of Drizzl's own `locations`."""
    query = """
        SELECT COALESCE(po.facility_name, 'Unknown') AS facility, SUM(p.qty) AS total_qty
        FROM po_line_items p
        JOIN purchase_orders po ON po.po_number = p.po_number AND po.voided = 0
    """
    params = []
    if sku_code:
        query += " WHERE p.item_code = ?"
        params.append(sku_code)
    query += " GROUP BY facility ORDER BY total_qty DESC"
    return conn.execute(query, params).fetchall()


def damage_trend_over_time(conn, sku_code=None, location=None):
    """Daily total of 'loss' movements -- feeds the damage-trend chart's
    time axis (paired with damaged_units_by_cause() for the cause
    breakdown)."""
    query = """
        SELECT m.movement_date AS date, SUM(m.quantity) AS qty
        FROM inventory_movements m
        LEFT JOIN locations lf ON lf.id = m.location_from_id
        WHERE m.movement_type = 'loss' AND m.voided = 0
    """
    params = []
    if sku_code:
        query += " AND m.sku_code = ?"
        params.append(sku_code)
    if location:
        query += " AND lf.name = ?"
        params.append(location)
    query += " GROUP BY m.movement_date ORDER BY m.movement_date"
    return conn.execute(query, params).fetchall()


def _flavor_name(sku_desc):
    """Every product description is "Drizzl {Flavor} | {rest}" -- e.g.
    "Drizzl Passionfruit | Probiotic Soda | ... | 250 ml" for a single
    can and "Drizzl Passionfruit | ... | Pack of 6 1.5 ltr" for a 6-pack
    of the same flavor. Taking everything before the first "|" and
    stripping the brand name collapses those pack-size/variant SKUs into
    one flavor category."""
    if not sku_desc:
        return None
    return sku_desc.split("|")[0].replace("Drizzl", "").strip() or None


def po_quantity_by_flavor(conn):
    """Total ordered quantity across every parsed PO, grouped by flavor
    rather than raw SKU code -- feeds the "flavor popularity" pie chart.
    Not filtered by the dashboard's location/SKU filters: narrowing to
    one SKU would collapse the whole point of a flavor breakdown down to
    a single 100% slice.

    Units caveat, confirmed 2026-08-13: this sums po_line_items.qty
    directly, no pack-size normalization, because every real PO line so
    far is a single-can SKU (verified: qty * unit_base_cost ==
    taxable_value exactly). If a "Pack of 6" SKU ever appears on a real
    PO, check that same math before trusting this total -- we don't yet
    know whether Scootsy's PDF would report that line's qty in packs or
    in individual cans, and summing the wrong one in would silently
    understate or overstate the real can count.

    Description resolution, updated Phase 5: prefers the canonical
    master_products.product_name (for lines with a product_id) over the
    legacy products.sku_desc lookup by item_code -- without this, every
    Phase-5-posted canonical PO line would show up here as flavor
    "Unknown", since Phase 5 deliberately never creates a legacy
    `products` row for a customer SKU. Grouping key is unchanged
    (pli.item_code) -- this only changes which description wins."""
    rows = conn.execute(
        """
        SELECT MAX(COALESCE(mp.product_name, p.sku_desc)) AS sku_desc, SUM(pli.qty) AS total_qty
        FROM po_line_items pli
        JOIN purchase_orders po ON po.po_number = pli.po_number AND po.voided = 0
        LEFT JOIN products p ON p.sku_code = pli.item_code
        LEFT JOIN master_products mp ON mp.product_id = pli.product_id
        GROUP BY pli.item_code
        """
    ).fetchall()
    by_flavor = {}
    for r in rows:
        flavor = _flavor_name(r["sku_desc"]) or "Unknown"
        by_flavor[flavor] = by_flavor.get(flavor, 0) + (r["total_qty"] or 0)
    return sorted(
        ({"flavor": f, "total_qty": q} for f, q in by_flavor.items()),
        key=lambda r: -r["total_qty"],
    )


def unresolved_flags(conn):
    """Documents whose parsed numbers didn't add up as expected -- still
    stored, but worth a human glancing at before trusting them fully."""
    return conn.execute(
        """
        SELECT id, document_type, document_id, issue, source_file, created_at
        FROM ingestion_flags
        WHERE resolved = 0
        ORDER BY created_at DESC
        """
    ).fetchall()


def negative_balances(conn):
    """Every (location, SKU) currently sitting below zero -- surfaced as
    its own prominent dashboard alert rather than only shown in red
    inside the full stock table, since a negative balance almost always
    means a real bookkeeping gap (missing production/transfer/opening
    balance) worth investigating, not just a display quirk. Reuses
    stock_by_location(), which already includes negative rows (its
    `HAVING qty_on_hand != 0` only excludes exact zero)."""
    return [r for r in stock_by_location(conn) if r["qty_on_hand"] < 0]


def movements_for_location_sku(conn, location_name, sku_code, limit=10):
    """Most recent movements touching one SKU at one location (either
    side), most-recent-first -- feeds "what led to this" under a
    negative-balance alert on the dashboard."""
    return conn.execute(
        """
        SELECT m.*, lf.name AS from_name, lt.name AS to_name
        FROM inventory_movements m
        LEFT JOIN locations lf ON lf.id = m.location_from_id
        LEFT JOIN locations lt ON lt.id = m.location_to_id
        WHERE (lf.name = ? OR lt.name = ?) AND m.sku_code = ? AND m.voided = 0
        ORDER BY m.id DESC LIMIT ?
        """,
        (location_name, location_name, sku_code, limit),
    ).fetchall()


def unresolved_inventory_flags(conn):
    """Negative-inventory incidents (manual overrides + GRN-caused) not
    yet marked resolved -- see inventory_flags in schema.sql."""
    return conn.execute(
        """
        SELECT * FROM inventory_flags
        WHERE resolved = 0
        ORDER BY created_at DESC
        """
    ).fetchall()


def voided_entries(conn):
    """Every voided PO/GRN/manual movement, most recent first -- the
    review trail that makes void safe to use liberally. If
    something gets voided by mistake, this is where a human would notice
    it (nothing else on the dashboard surfaces a voided entry unless you
    already know to look it up) and restore it via the matching
    unvoid_*() in ingest.py. Movements are restricted to
    reference_type='manual' -- a GRN-sourced movement's void is already
    represented by its GRN's row here (void_grn() cascades to it), so
    listing it separately would just be the same voiding event twice."""
    rows = []
    for r in conn.execute("SELECT po_number, void_reason, voided_at FROM purchase_orders WHERE voided = 1").fetchall():
        rows.append({"type": "po", "id": r["po_number"], "label": f"PO {r['po_number']}",
                      "reason": r["void_reason"], "voided_at": r["voided_at"]})
    for r in conn.execute(
        "SELECT grn_id, grn_number, void_reason, voided_at, supersedes_grn_id FROM grn_receipts WHERE voided = 1"
    ).fetchall():
        # Phase 10: "id" is grn_id (the real, unambiguous identity), not
        # grn_number -- a voided GRN's grn_number is no longer
        # guaranteed unique on its own (a superseded predecessor and its
        # active replacement can share one). superseded is True when
        # ANOTHER, currently-active GRN explicitly replaced this one --
        # the dashboard hides the Restore button for those (see
        # ingest.unvoid_grn()'s matching server-side refusal).
        superseded_by = conn.execute(
            "SELECT grn_number FROM grn_receipts WHERE supersedes_grn_id = ? AND voided = 0", (r["grn_id"],)
        ).fetchone()
        rows.append({
            "type": "grn", "id": r["grn_id"], "label": f"GRN {r['grn_number']}",
            "reason": r["void_reason"], "voided_at": r["voided_at"],
            "superseded_by_grn_number": superseded_by["grn_number"] if superseded_by else None,
        })
    for r in conn.execute(
        "SELECT id, sku_code, movement_type, quantity, void_reason, voided_at FROM inventory_movements "
        "WHERE voided = 1 AND reference_type = 'manual'"
    ).fetchall():
        rows.append({
            "type": "movement", "id": r["id"],
            "label": f"{r['movement_type']} of {r['quantity']:g} x {r['sku_code']} (movement #{r['id']})",
            "reason": r["void_reason"], "voided_at": r["voided_at"],
        })
    rows.sort(key=lambda r: r["voided_at"] or "", reverse=True)
    return rows


def purchase_orders_by_facility(conn, facility=None):
    """Every parsed PO, optionally narrowed to one Scootsy receiving
    facility (e.g. "DEMO FACILITY A", or whatever a Hyderabad facility's code
    turns out to be). Unlike po_vs_received_shortfall(), this lists
    every PO regardless of whether it's fully received yet -- it answers
    "what POs were ever placed at this facility", not just the
    outstanding ones. Also returns source_location (the Drizzl location
    assigned via assign_po_source_location(), or None/"Not allocated" if
    nobody's set one yet) -- a completely separate field from
    facility_name; see purchase_orders.source_location_id in schema.sql."""
    query = """
        SELECT po.po_number, po.po_date, po.facility_name, po.grand_total,
               c.name AS customer_name, l.name AS source_location
        FROM purchase_orders po
        LEFT JOIN customers c ON c.id = po.customer_id
        LEFT JOIN locations l ON l.id = po.source_location_id
        WHERE po.voided = 0
    """
    params = []
    if facility:
        query += " AND po.facility_name = ?"
        params.append(facility)
    query += " ORDER BY po.po_date DESC, po.po_number"
    return conn.execute(query, params).fetchall()


def lookup_document(conn, query):
    """Given a PO number or GRN number, trace the whole chain -- PO ->
    GRN(s) -- plus the ledger movements and ingestion flags tied to it,
    and (Phase 9) the canonical PO-vs-GRN discrepancy comparison for the
    PO. Unlike the dashboard's location/SKU filters (which only narrow
    the live balance and the 25-row recent-movements list), this searches
    the actual document tables directly, so it isn't limited by a row
    cap or by whether a movement's location happens to match a filter.

    Returns None if nothing matches, otherwise a dict with whichever of
    'po' / 'grns' were found, plus their line items, linked movements,
    and any ingestion flags."""
    query = (query or "").strip()
    if not query:
        return None

    po = conn.execute("SELECT * FROM purchase_orders WHERE po_number = ?", (query,)).fetchone()
    # Phase 10: a bare grn_number is no longer guaranteed to identify a
    # single row (a superseded predecessor can share one with its active
    # replacement), so this collects every matching ROW (by grn_id), not
    # just whichever one .fetchone() happened to pick. When resolving
    # po_number below, the active one wins if there is one (matches what
    # a human searching this number almost always means), else the most
    # recently created historical one.
    grn_direct_candidates = [
        dict(r) for r in conn.execute(
            "SELECT * FROM grn_receipts WHERE grn_number = ? ORDER BY grn_id DESC", (query,)
        ).fetchall()
    ]
    grn_direct = next((g for g in grn_direct_candidates if not g["voided"]), None) or \
        (grn_direct_candidates[0] if grn_direct_candidates else None)

    if not (po or grn_direct):
        return None

    po_number = (po["po_number"] if po else None) or (grn_direct["po_number"] if grn_direct else None)

    # Every grn_receipts ROW (grn_id) tied to this PO -- a correction
    # never changes po_number (a replacement is inserted with the exact
    # same one as what it supersedes), so this naturally captures the
    # WHOLE history chain, not just whichever row is currently active.
    grn_ids = {g["grn_id"] for g in grn_direct_candidates}
    if po_number:
        for row in conn.execute("SELECT grn_id FROM grn_receipts WHERE po_number = ?", (po_number,)).fetchall():
            grn_ids.add(row["grn_id"])

    result = {"query": query, "po": None, "grns": [], "flags": []}

    if po:
        result["po"] = dict(po)
        source_row = conn.execute(
            "SELECT l.name FROM locations l WHERE l.id = ?", (po["source_location_id"],)
        ).fetchone() if po["source_location_id"] else None
        result["po"]["source_location"] = source_row["name"] if source_row else None
        result["po"]["line_items"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM po_line_items WHERE po_number = ? ORDER BY sno", (po_number,)
            ).fetchall()
        ]
        result["po"]["discrepancies"] = official_po_grn_discrepancies(conn, po_number)

    grn_numbers = set()
    for grn_id in sorted(grn_ids):
        grn = conn.execute("SELECT * FROM grn_receipts WHERE grn_id = ?", (grn_id,)).fetchone()
        if not grn:
            continue
        grn_number = grn["grn_number"]
        grn_numbers.add(grn_number)
        grn_dict = dict(grn)
        # Only meaningful (and only correct to resolve) for the ACTIVE
        # occurrence of this grn_number -- a voided/superseded row's
        # historical source is whatever its own SALE movements' From
        # location already shows below, not a live re-resolution.
        grn_dict["source_location"] = resolve_grn_source_location(conn, grn_number) if not grn["voided"] else None
        # Phase 10 correction linkage -- "supersedes" is a stored FK,
        # "superseded_by" is deliberately derived with the reverse query
        # rather than a second hand-maintained column (see
        # grn_receipts.supersedes_grn_id in schema_postgres.sql).
        grn_dict["supersedes_grn_number"] = None
        if grn["supersedes_grn_id"]:
            old = conn.execute(
                "SELECT grn_number FROM grn_receipts WHERE grn_id = ?", (grn["supersedes_grn_id"],)
            ).fetchone()
            grn_dict["supersedes_grn_number"] = old["grn_number"] if old else None
        superseding = conn.execute(
            "SELECT grn_number FROM grn_receipts WHERE supersedes_grn_id = ?", (grn["grn_id"],)
        ).fetchone()
        grn_dict["superseded_by_grn_number"] = superseding["grn_number"] if superseding else None
        grn_dict["line_items"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM grn_line_items WHERE grn_id = ? ORDER BY sku_code", (grn_id,)
            ).fetchall()
        ]
        if grn["po_id"] is not None:
            # Canonical -- scoped via source_grn_line_item_id ->
            # grn_line_items.grn_id, never plain reference_id text,
            # which a superseded ancestor/descendant sharing this
            # grn_number could also match (Phase 10).
            movements_query = """
                SELECT m.*, lf.name AS from_name, lt.name AS to_name
                FROM inventory_movements m
                LEFT JOIN locations lf ON lf.id = m.location_from_id
                LEFT JOIN locations lt ON lt.id = m.location_to_id
                WHERE m.reference_type = 'grn' AND m.source_grn_line_item_id IN (
                    SELECT id FROM grn_line_items WHERE grn_id = ?
                )
                ORDER BY m.id
            """
            movements_param = grn_id
        else:
            # Legacy PDF GRN -- never part of a supersede chain, so
            # reference_id text matching is unambiguous exactly as
            # before Phase 10.
            movements_query = """
                SELECT m.*, lf.name AS from_name, lt.name AS to_name
                FROM inventory_movements m
                LEFT JOIN locations lf ON lf.id = m.location_from_id
                LEFT JOIN locations lt ON lt.id = m.location_to_id
                WHERE m.reference_type = 'grn' AND m.reference_id = ?
                ORDER BY m.id
            """
            movements_param = grn_number
        grn_dict["movements"] = [dict(r) for r in conn.execute(movements_query, (movements_param,)).fetchall()]
        result["grns"].append(grn_dict)

    doc_ids = set()
    if po_number:
        doc_ids.add(po_number)
    doc_ids |= grn_numbers
    if doc_ids:
        placeholders = ",".join("?" * len(doc_ids))
        result["flags"] = [
            dict(r) for r in conn.execute(
                f"SELECT * FROM ingestion_flags WHERE document_id IN ({placeholders}) ORDER BY created_at DESC",
                tuple(doc_ids),
            ).fetchall()
        ]

    return result


def print_report():
    conn = get_connection()

    print("=== Current stock by location ===")
    rows = stock_by_location(conn)
    if not rows:
        print("None found.")
    for r in rows:
        desc = (r["sku_desc"] or "")[:40]
        print(f"  {r['location']}: SKU {r['sku_code']} {desc!r} - {r['qty_on_hand']:.0f} units")

    print()
    print("=== Damaged units by SKU (running counter) ===")
    rows = damaged_units_by_sku(conn)
    if not rows:
        print("None found.")
    for r in rows:
        desc = (r["sku_desc"] or "")[:40]
        print(f"  SKU {r['sku_code']} {desc!r}: {r['total_damaged']:.0f} units damaged across {r['n_events']} event(s)")

    print()
    print("=== Damaged units by cause ===")
    rows = damaged_units_by_cause(conn)
    if not rows:
        print("None found.")
    for r in rows:
        print(f"  {r['cause']}: {r['total_damaged']:.0f} units across {r['n_events']} event(s)")

    print()
    print("=== Ordered vs Received shortfall (legacy PDF-sourced PO lines only) ===")
    rows = po_vs_received_shortfall(conn)
    if not rows:
        print("None found.")
    for r in rows:
        desc = (r["sku_desc"] or "")[:40]
        print(
            f"  PO {r['po_number']} - SKU {r['sku_code']} {desc!r}: "
            f"ordered {r['ordered_qty']:.0f}, received {r['received_qty']:.0f} "
            f"(shortfall {r['shortfall']:+.0f})"
        )

    print()
    print("=== Official PO-vs-GRN discrepancies (canonical, product_id/external_sku) ===")
    rows = official_discrepancies(conn)
    if not rows:
        print("None found.")
    for r in rows:
        desc = (r["product_name"] or "")[:40]
        print(
            f"  PO {r['po_number']} - SKU {r['external_sku']} {desc!r}: "
            f"ordered {r['ordered_qty']:.0f}, received {r['received_qty']:.0f} "
            f"(shortfall {r['computed_shortfall_qty']:+.0f}) -- {r['status']}"
        )

    print()
    print("=== Documents flagged for review (parsed numbers didn't add up) ===")
    rows = unresolved_flags(conn)
    if not rows:
        print("None found.")
    for r in rows:
        print(f"  [{r['document_type']}] {r['document_id']} ({r['source_file']}): {r['issue']}")

    conn.close()


if __name__ == "__main__":
    print_report()

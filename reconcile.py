"""
Reconciliation and reporting, built on top of the inventory_movements ledger.
"""
from db import get_connection


def stock_by_location(conn, location=None, sku_code=None):
    """Current stock per SKU per location, derived from the ledger itself
    -- not stored anywhere, always computed fresh from history. Also
    attaches two derived columns per row:
      qty_committed   = committed_by_location_sku()'s value for this
                        (location, SKU) -- inventory reserved against an
                        open, unresolved PO allocated to this location.
      qty_uncommitted = qty_on_hand - qty_committed -- what's actually
                        free to promise/move without eating into an open
                        PO's reserved stock.
    A PO never creates a ledger movement (an order still isn't a stock
    event -- qty_on_hand is exactly the same as before this existed), so
    a SKU can have real committed quantity at a location with zero
    physical stock so far; a synthetic qty_on_hand=0 row is added for
    that case so the commitment isn't invisible just because nothing's
    arrived yet. Returns plain dicts (not sqlite3.Row) since these two
    columns are computed in Python, not SQL."""
    query = """
        SELECT l.name AS location, m.sku_code, MAX(p.sku_desc) AS sku_desc, SUM(delta) AS qty_on_hand
        FROM (
            SELECT location_to_id AS location_id, sku_code, quantity AS delta
            FROM inventory_movements WHERE location_to_id IS NOT NULL AND voided = 0
            UNION ALL
            SELECT location_from_id AS location_id, sku_code, -quantity AS delta
            FROM inventory_movements WHERE location_from_id IS NOT NULL AND voided = 0
        ) m
        JOIN locations l ON l.id = m.location_id
        LEFT JOIN products p ON p.sku_code = m.sku_code
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
    query += " GROUP BY l.name, m.sku_code HAVING SUM(delta) != 0 ORDER BY l.name, qty_on_hand DESC"
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    committed_map = committed_by_location_sku(conn)
    seen = {(r["location"], r["sku_code"]) for r in rows}
    for r in rows:
        c = committed_map.get((r["location"], r["sku_code"]), 0)
        r["qty_committed"] = c
        r["qty_uncommitted"] = r["qty_on_hand"] - c

    product_desc = None
    for (loc, sku), c in committed_map.items():
        if location and loc != location:
            continue
        if sku_code and sku != sku_code:
            continue
        if (loc, sku) in seen:
            continue
        if product_desc is None:
            product_desc = {p["sku_code"]: p["sku_desc"] for p in conn.execute("SELECT sku_code, sku_desc FROM products").fetchall()}
        rows.append({
            "location": loc, "sku_code": sku, "sku_desc": product_desc.get(sku),
            "qty_on_hand": 0, "qty_committed": c, "qty_uncommitted": 0 - c,
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


def committed_quantity(conn):
    """Every still-open (unresolved) PO line's committed quantity --
    reserved against that PO until a GRN resolves it. This is
    deliberately NOT `ordered - received`: the moment ANY non-voided GRN
    line exists for a (po_number, sku_code) pair, that line's commitment
    drops to 0 entirely and for good, regardless of how much was
    actually received. A shortfall between expected and received
    becomes a discrepancy (see grn_discrepancies()), not a remaining PO
    commitment -- Drizzl isn't expected to deliver the difference again
    against the same PO; that's a separate discrepancy/financial
    process. If the resolving GRN is later voided, the commitment
    correctly reappears here (the EXISTS check below only counts
    non-voided GRNs).

    Returns one row per still-committed PO line: po_number, sku_code,
    sku_desc, qty, source_location (the PO's assigned Drizzl location
    name, or None if not yet allocated -- see purchase_orders.source_
    location_id in schema.sql)."""
    return conn.execute(
        """
        SELECT
            p.po_number, p.item_code AS sku_code, p.item_desc AS sku_desc, p.qty,
            l.name AS source_location
        FROM po_line_items p
        JOIN purchase_orders po ON po.po_number = p.po_number AND po.voided = 0
        LEFT JOIN locations l ON l.id = po.source_location_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM grn_receipts gr
            JOIN grn_line_items gli ON gli.grn_number = gr.grn_number
            WHERE gr.po_number = p.po_number
              AND gr.voided = 0
              AND gli.sku_code = p.item_code
        )
        ORDER BY p.po_number, p.item_code
        """
    ).fetchall()


def committed_by_location_sku(conn):
    """committed_quantity(), summed by (Drizzl source location, SKU) --
    only for PO lines that actually have a source location assigned.
    Feeds the Committed/Uncommitted columns in stock_by_location() and
    the commitment-shortfall warning in app.py's new_movement(). Returns
    a plain {(location, sku_code): qty} dict."""
    by_key = {}
    for r in committed_quantity(conn):
        if not r["source_location"]:
            continue
        key = (r["source_location"], r["sku_code"])
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
        WHERE gr.grn_number = ?
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
    """Per PO line item: across all GRNs tied to that PO, has the full
    ordered quantity been received (sold) yet? Only meaningful for POs
    whose PDF has actually been parsed (stub POs have no line items).
    No location filter -- a PO/GRN isn't tied to a specific Drizzl
    location, it's what Scootsy received."""
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
            ON gli.grn_number = g.grn_number AND gli.sku_code = p.item_code
    """
    params = []
    if sku_code:
        query += " WHERE p.item_code = ?"
        params.append(sku_code)
    query += (
        " GROUP BY p.po_number, p.item_code"
        " HAVING MAX(p.qty) - COALESCE(SUM(CASE WHEN g.voided = 0 THEN gli.received_qty END), 0) != 0"
        " ORDER BY shortfall DESC"
    )
    return conn.execute(query, params).fetchall()


def grn_discrepancies(conn, sku_code=None):
    """GRN lines where the delivery's expected quantity (only known for
    PDF-sourced GRNs -- see schema.sql) doesn't match what was actually
    received. This is the MVP discrepancy workflow: PO -> GRN -> if
    expected != received, this shows up here as unresolved until a
    matching Discrepancy Note is attached (or a human otherwise resolves
    it) -- see ingest.py, which deliberately does NOT auto-create a loss
    movement for this. No location filter -- GRN lines aren't tied to a
    specific Drizzl location."""
    query = """
        SELECT
            gr.po_number,
            gli.grn_number,
            gli.sku_code,
            p.sku_desc,
            gli.expected_qty,
            gli.received_qty,
            (gli.expected_qty - gli.received_qty) AS discrepancy_qty,
            dn.dn_number,
            dni.reason,
            dni.remarks
        FROM grn_line_items gli
        JOIN grn_receipts gr ON gr.grn_number = gli.grn_number AND gr.voided = 0
        LEFT JOIN products p ON p.sku_code = gli.sku_code
        LEFT JOIN discrepancy_notes dn ON dn.grn_number = gli.grn_number AND dn.voided = 0
        LEFT JOIN discrepancy_note_items dni
            ON dni.dn_number = dn.dn_number AND dni.sku_code = gli.sku_code
        WHERE gli.expected_qty IS NOT NULL
          AND gli.expected_qty != gli.received_qty
    """
    params = []
    if sku_code:
        query += " AND gli.sku_code = ?"
        params.append(sku_code)
    query += " ORDER BY gli.grn_number, gli.sku_code"
    return conn.execute(query, params).fetchall()


def debit_note_vs_discrepancy_note(conn):
    """Does Swiggy's flat financial deduction (Debit Note) match the
    itemized damage detail we actually have (Discrepancy Note) for the
    same PO? A mismatch -- or a missing Discrepancy Note entirely --
    means you're trusting a lump-sum number with no itemized backup."""
    return conn.execute(
        """
        SELECT
            dn.note_number,
            dn.po_number,
            dn.total_amount AS debit_note_total,
            dc.dn_amt AS discrepancy_note_total,
            dc.dn_number
        FROM debit_notes dn
        LEFT JOIN discrepancy_notes dc ON dc.po_number = dn.po_number
        WHERE dc.dn_number IS NULL
           OR ABS(COALESCE(dc.dn_amt, 0) - COALESCE(dn.total_amount, 0)) > 0.01
        """
    ).fetchall()


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
    understate or overstate the real can count."""
    rows = conn.execute(
        """
        SELECT MAX(p.sku_desc) AS sku_desc, SUM(pli.qty) AS total_qty
        FROM po_line_items pli
        JOIN purchase_orders po ON po.po_number = pli.po_number AND po.voided = 0
        LEFT JOIN products p ON p.sku_code = pli.item_code
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
    """Every voided PO/GRN/Discrepancy Note/manual movement, most recent
    first -- the review trail that makes void safe to use liberally. If
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
    for r in conn.execute("SELECT grn_number, void_reason, voided_at FROM grn_receipts WHERE voided = 1").fetchall():
        rows.append({"type": "grn", "id": r["grn_number"], "label": f"GRN {r['grn_number']}",
                      "reason": r["void_reason"], "voided_at": r["voided_at"]})
    for r in conn.execute("SELECT dn_number, void_reason, voided_at FROM discrepancy_notes WHERE voided = 1").fetchall():
        rows.append({"type": "discrepancy_note", "id": r["dn_number"], "label": f"Discrepancy Note {r['dn_number']}",
                      "reason": r["void_reason"], "voided_at": r["voided_at"]})
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
    """Given a PO number, GRN number, or Discrepancy Note number (any one
    of the three), trace the whole chain -- PO -> GRN(s) -> Discrepancy
    Note(s) -- plus the ledger movements and ingestion flags tied to it.
    Unlike the dashboard's location/SKU filters (which only narrow the
    live balance and the 25-row recent-movements list), this searches
    the actual document tables directly, so it isn't limited by a row
    cap or by whether a movement's location happens to match a filter.

    Returns None if nothing matches, otherwise a dict with whichever of
    'po' / 'grns' / 'discrepancy_notes' were found, plus their line
    items, linked movements, and any ingestion flags."""
    query = (query or "").strip()
    if not query:
        return None

    po = conn.execute("SELECT * FROM purchase_orders WHERE po_number = ?", (query,)).fetchone()
    grn_direct = conn.execute("SELECT * FROM grn_receipts WHERE grn_number = ?", (query,)).fetchone()
    dn_direct = conn.execute("SELECT * FROM discrepancy_notes WHERE dn_number = ?", (query,)).fetchone()

    if not (po or grn_direct or dn_direct):
        return None

    po_number = (po["po_number"] if po else None) or (grn_direct["po_number"] if grn_direct else None) \
        or (dn_direct["po_number"] if dn_direct else None)

    grn_numbers = set()
    if grn_direct:
        grn_numbers.add(grn_direct["grn_number"])
    if dn_direct and dn_direct["grn_number"]:
        grn_numbers.add(dn_direct["grn_number"])
    if po_number:
        for row in conn.execute("SELECT grn_number FROM grn_receipts WHERE po_number = ?", (po_number,)).fetchall():
            grn_numbers.add(row["grn_number"])

    result = {"query": query, "po": None, "grns": [], "discrepancy_notes": [], "flags": []}

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

    for grn_number in sorted(grn_numbers):
        grn = conn.execute("SELECT * FROM grn_receipts WHERE grn_number = ?", (grn_number,)).fetchone()
        if not grn:
            continue
        grn_dict = dict(grn)
        grn_dict["source_location"] = resolve_grn_source_location(conn, grn_number)
        grn_dict["line_items"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM grn_line_items WHERE grn_number = ? ORDER BY sku_code", (grn_number,)
            ).fetchall()
        ]
        grn_dict["movements"] = [
            dict(r) for r in conn.execute(
                """
                SELECT m.*, lf.name AS from_name, lt.name AS to_name
                FROM inventory_movements m
                LEFT JOIN locations lf ON lf.id = m.location_from_id
                LEFT JOIN locations lt ON lt.id = m.location_to_id
                WHERE m.reference_type = 'grn' AND m.reference_id = ?
                ORDER BY m.id
                """,
                (grn_number,),
            ).fetchall()
        ]
        result["grns"].append(grn_dict)

    dn_rows = []
    if dn_direct:
        dn_rows.append(dn_direct)
    if po_number:
        dn_rows.extend(conn.execute(
            "SELECT * FROM discrepancy_notes WHERE po_number = ?", (po_number,)
        ).fetchall())
    if grn_numbers:
        placeholders = ",".join("?" * len(grn_numbers))
        dn_rows.extend(conn.execute(
            f"SELECT * FROM discrepancy_notes WHERE grn_number IN ({placeholders})",
            tuple(grn_numbers),
        ).fetchall())

    seen_dn = set()
    for dn in dn_rows:
        if dn["dn_number"] in seen_dn:
            continue
        seen_dn.add(dn["dn_number"])
        dn_dict = dict(dn)
        dn_dict["line_items"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM discrepancy_note_items WHERE dn_number = ? ORDER BY sno", (dn["dn_number"],)
            ).fetchall()
        ]
        result["discrepancy_notes"].append(dn_dict)

    doc_ids = set()
    if po_number:
        doc_ids.add(po_number)
    doc_ids |= grn_numbers
    doc_ids |= seen_dn
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
    print("=== Ordered vs Received shortfall (per PO line, parsed POs only) ===")
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
    print("=== GRN discrepancies (expected vs received) ===")
    rows = grn_discrepancies(conn)
    if not rows:
        print("None found.")
    for r in rows:
        desc = (r["sku_desc"] or "")[:40]
        dn_status = r["dn_number"] or "Missing / Awaiting Upload"
        print(
            f"  PO {r['po_number']} / GRN {r['grn_number']} - SKU {r['sku_code']} {desc!r}: "
            f"expected {r['expected_qty']:.0f}, received {r['received_qty']:.0f} "
            f"(difference {r['discrepancy_qty']:+.0f}) -- Discrepancy Note: {dn_status}"
        )
        if r["dn_number"]:
            print(f"      reason: {r['reason']}, remarks: {r['remarks']}")

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

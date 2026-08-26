"""
Verifies Phase 9: removal of the legacy Discrepancy Note PDF workflow,
and reconcile.py's canonical PO-vs-GRN discrepancy reporting
(official_po_grn_discrepancies() / official_discrepancies()) built to
replace it.

Two kinds of checks:
  1. Static checks that the old PDF workflow (upload option, parser
     calls, routes, UI, discrepancy_notes/discrepancy_note_items tables)
     is actually gone -- run against the real drizzl_inventory database
     and the app's own source, since there's nothing to stage/post for
     "is this removed."
  2. The new canonical discrepancy calculation, run entirely against a
     disposable throwaway Postgres database (drizzl_inventory_test_phase9)
     -- created/dropped for this run, never the real drizzl_inventory
     database. Uses small synthetic official PO/GRN rows (inserted
     directly, not through the full staging/posting pipeline, since the
     scenarios here are about the comparison math, not the posting
     pipeline itself -- that's already covered by verify_po_posting.py/
     verify_grn_posting.py).
"""
import re
import sys
from pathlib import Path

import psycopg2

import app as app_module
import db as db_module
import reconcile

TEST_DB_NAME = "drizzl_inventory_test_phase9"


def check(label, condition, detail=""):
    condition = bool(condition)
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    return condition


def _admin_conn():
    c = psycopg2.connect(dbname="postgres")
    c.autocommit = True
    return c


def create_test_database():
    admin = _admin_conn()
    cur = admin.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,))
    if cur.fetchone():
        cur.execute(f'DROP DATABASE "{TEST_DB_NAME}"')
    cur.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    cur.close()
    admin.close()


def drop_test_database():
    admin = _admin_conn()
    cur = admin.cursor()
    cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')
    cur.close()
    admin.close()


def get_test_connection():
    raw = psycopg2.connect(dbname=TEST_DB_NAME)
    conn = db_module._PGConnection(raw)
    conn.executescript(db_module.SCHEMA_PATH.read_text())
    db_module._seed(conn)
    db_module._seed_catalog(conn)
    conn.commit()
    return conn


def get_customer_id(conn, name="Scootsy Logistics Private Limited"):
    return conn.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()["id"]


def get_location_id(conn, name="Drizzl Demo Warehouse"):
    return conn.execute("SELECT id FROM locations WHERE name = ?", (name,)).fetchone()["id"]


def product_id_for_sku(conn, sku):
    r = conn.execute(
        "SELECT mp.product_id FROM master_products mp JOIN customer_product_skus c ON c.product_id = mp.product_id "
        "WHERE c.external_sku = ?", (sku,)
    ).fetchone()
    return r["product_id"] if r else None


def insert_official_po(conn, po_number, customer_id, lines):
    """lines: list of (product_id, external_sku, qty)."""
    po_id = conn.execute(
        "INSERT INTO purchase_orders (po_number, customer_id) VALUES (?, ?) RETURNING po_id",
        (po_number, customer_id),
    ).fetchone()["po_id"]
    for product_id, external_sku, qty in lines:
        conn.execute(
            "INSERT INTO po_line_items (po_number, item_code, product_id, external_sku, qty) VALUES (?, ?, ?, ?, ?)",
            (po_number, external_sku, product_id, external_sku, qty),
        )
    return po_id


def insert_official_grn(conn, grn_number, po_id, po_number, customer_id, lines, voided=0):
    """lines: list of (product_id, external_sku, received_qty, source_dn_quantity)."""
    grn_id = conn.execute(
        "INSERT INTO grn_receipts (grn_number, po_id, po_number, customer_id, source, voided) "
        "VALUES (?, ?, ?, ?, 'csv', ?) RETURNING grn_id",
        (grn_number, po_id, po_number, customer_id, voided),
    ).fetchone()["grn_id"]
    for product_id, external_sku, received_qty, source_dn_quantity in lines:
        conn.execute(
            "INSERT INTO grn_line_items (grn_number, grn_id, sku_code, product_id, external_sku, received_qty, source_dn_quantity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (grn_number, grn_id, external_sku, product_id, external_sku, received_qty, source_dn_quantity),
        )
    return grn_id


def check_workflow_removed():
    print("\n--- 1-3: old Discrepancy Note PDF workflow is gone ---")
    ok = True

    ok &= check("discrepancy_note_parser.py deleted", not Path("discrepancy_note_parser.py").exists())
    ok &= check("verify_discrepancy_note_parser.py deleted", not Path("verify_discrepancy_note_parser.py").exists())

    rules = {r.rule for r in app_module.app.url_map.iter_rules()}
    ok &= check("legacy direct PDF upload route is removed", "/upload" not in rules)
    ok &= check("no /discrepancy-note/<x>/void route", not any("discrepancy-note" in r for r in rules))
    ok &= check(
        "no upsert_discrepancy_note/void_discrepancy_note/unvoid_discrepancy_note in ingest.py",
        not any(
            hasattr(__import__("ingest"), name)
            for name in ("upsert_discrepancy_note", "void_discrepancy_note", "unvoid_discrepancy_note")
        ),
    )
    ok &= check(
        "no validate_discrepancy_note in validate.py",
        not hasattr(__import__("validate"), "validate_discrepancy_note"),
    )
    ok &= check(
        "no grn_discrepancies/debit_note_vs_discrepancy_note in reconcile.py",
        not hasattr(reconcile, "grn_discrepancies") and not hasattr(reconcile, "debit_note_vs_discrepancy_note"),
    )

    template_text = "\n".join(
        Path(p).read_text() for p in Path("templates").glob("*.html")
    )
    ok &= check(
        "no active-UI reference to the DN upload/void workflow in templates",
        "discrepancy_note_upload" not in template_text
        and "void_discrepancy_note_route" not in template_text
        and "restore_discrepancy_note_route" not in template_text
        and "result.discrepancy_notes" not in template_text,
    )

    real_conn = db_module.get_connection()
    try:
        row = real_conn.execute(
            "SELECT to_regclass('discrepancy_notes') AS t, to_regclass('discrepancy_note_items') AS i"
        ).fetchone()
        ok &= check("discrepancy_notes table dropped from real database", row["t"] is None)
        ok &= check("discrepancy_note_items table dropped from real database", row["i"] is None)
    finally:
        real_conn.close()

    return ok


def run():
    ok = check_workflow_removed()

    print(f"\nCreating throwaway database {TEST_DB_NAME}...")
    create_test_database()
    conn = get_test_connection()

    try:
        customer_id = get_customer_id(conn)
        p_passion = product_id_for_sku(conn, "DEMO-SKU-001")  # Passionfruit
        p_orange = product_id_for_sku(conn, "DEMO-SKU-005")    # Orange

        # -----------------------------------------------------------------
        print("\n--- 4: exact receipt (100 ordered / 100 received) ---")
        po_id = insert_official_po(conn, "PO-EXACT", customer_id, [(p_passion, "DEMO-SKU-001", 100)])
        insert_official_grn(conn, "GRN-EXACT", po_id, "PO-EXACT", customer_id, [(p_passion, "DEMO-SKU-001", 100, 0)])
        conn.commit()
        rows = reconcile.official_po_grn_discrepancies(conn, "PO-EXACT")
        ok &= check("one comparison row", len(rows) == 1, f"got {len(rows)}")
        if rows:
            r = rows[0]
            ok &= check("ordered 100", r["ordered_qty"] == 100)
            ok &= check("received 100", r["received_qty"] == 100)
            ok &= check("shortfall 0", r["computed_shortfall_qty"] == 0, f"got {r['computed_shortfall_qty']}")
            ok &= check("status COMPLETE", r["status"] == "COMPLETE", f"got {r['status']}")

        # -----------------------------------------------------------------
        print("\n--- 5: partial receipt (600 ordered / 200 received), Source DN Qty stays separate ---")
        po_id = insert_official_po(conn, "PO-SHORT", customer_id, [(p_passion, "DEMO-SKU-001", 600)])
        insert_official_grn(conn, "GRN-SHORT", po_id, "PO-SHORT", customer_id, [(p_passion, "DEMO-SKU-001", 200, 13)])
        conn.commit()
        rows = reconcile.official_po_grn_discrepancies(conn, "PO-SHORT")
        ok &= check("one comparison row", len(rows) == 1, f"got {len(rows)}")
        if rows:
            r = rows[0]
            ok &= check("ordered 600", r["ordered_qty"] == 600)
            ok &= check("received 200", r["received_qty"] == 200)
            ok &= check("computed_shortfall_qty 400", r["computed_shortfall_qty"] == 400, f"got {r['computed_shortfall_qty']}")
            ok &= check("status SHORT", r["status"] == "SHORT")
            # 10: source_dn_quantity preserved as a separate audit fact --
            # never blended with / substituted for the computed shortfall.
            ok &= check("source_dn_quantity is 13, not folded into shortfall", r["source_dn_quantity"] == 13, f"got {r['source_dn_quantity']}")
            ok &= check("source_dn_quantity (13) != computed_shortfall_qty (400)", r["source_dn_quantity"] != r["computed_shortfall_qty"])

        # -----------------------------------------------------------------
        print("\n--- 6: product on PO but absent from GRN (A=100/B=50, GRN only has A) ---")
        po_id = insert_official_po(conn, "PO-ABSENT", customer_id, [(p_passion, "DEMO-SKU-001", 100), (p_orange, "DEMO-SKU-005", 50)])
        insert_official_grn(conn, "GRN-ABSENT", po_id, "PO-ABSENT", customer_id, [(p_passion, "DEMO-SKU-001", 100, 0)])
        conn.commit()
        rows = reconcile.official_po_grn_discrepancies(conn, "PO-ABSENT")
        ok &= check("two comparison rows (both PO lines present)", len(rows) == 2, f"got {len(rows)}")
        by_sku = {r["external_sku"]: r for r in rows}
        if "DEMO-SKU-001" in by_sku:
            ok &= check("A: received 100, shortfall 0", by_sku["DEMO-SKU-001"]["received_qty"] == 100 and by_sku["DEMO-SKU-001"]["computed_shortfall_qty"] == 0)
        if "DEMO-SKU-005" in by_sku:
            ok &= check("B: received 0 (absent from GRN)", by_sku["DEMO-SKU-005"]["received_qty"] == 0, f"got {by_sku['DEMO-SKU-005']['received_qty']}")
            ok &= check("B: shortfall = full ordered qty (50)", by_sku["DEMO-SKU-005"]["computed_shortfall_qty"] == 50, f"got {by_sku['DEMO-SKU-005']['computed_shortfall_qty']}")
            ok &= check("B: status SHORT", by_sku["DEMO-SKU-005"]["status"] == "SHORT")

        # -----------------------------------------------------------------
        print("\n--- 7: multi-lot GRN lines aggregate before comparison ---")
        po_id = insert_official_po(conn, "PO-MULTILOT", customer_id, [(p_passion, "DEMO-SKU-001", 72)])
        insert_official_grn(
            conn, "GRN-MULTILOT", po_id, "PO-MULTILOT", customer_id,
            [(p_passion, "DEMO-SKU-001", 48, 0), (p_passion, "DEMO-SKU-001", 24, 0)],  # two distinct lots, same product/sku
        )
        conn.commit()
        rows = reconcile.official_po_grn_discrepancies(conn, "PO-MULTILOT")
        ok &= check("one comparison row (lots collapsed to one key)", len(rows) == 1, f"got {len(rows)}")
        if rows:
            ok &= check("received = 48 + 24 = 72, not double-counted", rows[0]["received_qty"] == 72, f"got {rows[0]['received_qty']}")
            ok &= check("shortfall 0", rows[0]["computed_shortfall_qty"] == 0)

        # -----------------------------------------------------------------
        print("\n--- 8: an already-collapsed duplicate-representation line isn't re-doubled ---")
        # Simulates the real Scootsy CSV's DNQuantity=0/positive-pair case,
        # already normalized to one official line of 203 by Phase 6/8 --
        # this function must not re-sum it into 406.
        po_id = insert_official_po(conn, "PO-COLLAPSED", customer_id, [(p_passion, "DEMO-SKU-001", 203)])
        insert_official_grn(conn, "GRN-COLLAPSED", po_id, "PO-COLLAPSED", customer_id, [(p_passion, "DEMO-SKU-001", 203, 0)])
        conn.commit()
        rows = reconcile.official_po_grn_discrepancies(conn, "PO-COLLAPSED")
        ok &= check("received stays 203, not 406", rows and rows[0]["received_qty"] == 203, f"got {rows[0]['received_qty'] if rows else None}")

        # -----------------------------------------------------------------
        print("\n--- 9: (product_id, external_sku) identity respected -- a same-product different-SKU GRN line doesn't match ---")
        po_id = insert_official_po(conn, "PO-IDENTITY", customer_id, [(p_passion, "DEMO-SKU-001", 50)])
        # GRN line has the SAME product_id but a DIFFERENT external_sku --
        # must not be treated as satisfying the PO line.
        insert_official_grn(conn, "GRN-IDENTITY", po_id, "PO-IDENTITY", customer_id, [(p_passion, "OTHER-SKU", 50, 0)])
        conn.commit()
        rows = reconcile.official_po_grn_discrepancies(conn, "PO-IDENTITY")
        ok &= check("one comparison row (the PO's own key)", len(rows) == 1, f"got {len(rows)}")
        if rows:
            ok &= check("received 0 -- mismatched external_sku does not satisfy the PO line", rows[0]["received_qty"] == 0, f"got {rows[0]['received_qty']}")
            ok &= check("shortfall = full ordered qty (50)", rows[0]["computed_shortfall_qty"] == 50)

        # -----------------------------------------------------------------
        print("\n--- 11: official-records-only -- no GRN posted yet, and a legacy (non-canonical) PO line are both excluded ---")
        insert_official_po(conn, "PO-NOGR", customer_id, [(p_passion, "DEMO-SKU-001", 100)])
        conn.commit()
        rows = reconcile.official_po_grn_discrepancies(conn, "PO-NOGR")
        ok &= check("no GRN yet -> [] (not a discrepancy, just still open)", rows == [], f"got {rows}")

        po_id_legacy = conn.execute(
            "INSERT INTO purchase_orders (po_number, customer_id) VALUES (?, ?) RETURNING po_id",
            ("PO-LEGACY", customer_id),
        ).fetchone()["po_id"]
        conn.execute(
            "INSERT INTO po_line_items (po_number, item_code, qty) VALUES (?, ?, ?)",
            ("PO-LEGACY", "LEGACYSKU", 10),
        )
        insert_official_grn(conn, "GRN-LEGACY", po_id_legacy, "PO-LEGACY", customer_id, [])
        conn.commit()
        rows = reconcile.official_po_grn_discrepancies(conn, "PO-LEGACY")
        ok &= check("legacy (product_id IS NULL) PO line excluded from canonical comparison", rows == [], f"got {rows}")

        # official_discrepancies() dashboard-wide listing picks up the
        # short ones and respects the sku_code filter.
        all_rows = reconcile.official_discrepancies(conn)
        short_pos = {r["po_number"] for r in all_rows if r["status"] == "SHORT"}
        ok &= check(
            "official_discrepancies() dashboard listing includes the SHORT POs",
            {"PO-SHORT", "PO-ABSENT", "PO-IDENTITY"} <= short_pos,
            f"got {short_pos}",
        )
        filtered = reconcile.official_discrepancies(conn, sku_code="DEMO-SKU-001")
        ok &= check(
            "sku_code filter narrows to external_sku=DEMO-SKU-001 only",
            all(r["external_sku"] == "DEMO-SKU-001" for r in filtered) and len(filtered) > 0,
        )

        tracker = {r["po_number"]: r for r in reconcile.po_grn_fulfillment(conn)}
        ok &= check("fulfillment tracker marks exact receipt GRN POSTED", tracker["PO-EXACT"]["fulfillment_status"] == "grn_posted")
        ok &= check("fulfillment tracker marks short receipt NEEDS DISCREPANCY until a note is classified", tracker["PO-SHORT"]["fulfillment_status"] == "needs_discrepancy")
        ok &= check("fulfillment tracker marks PO with no GRN AWAITING GRN", tracker["PO-NOGR"]["fulfillment_status"] == "awaiting_grn")
        awaiting = reconcile.po_grn_fulfillment(conn, status="awaiting_grn")
        ok &= check("fulfillment tracker status filter returns only awaiting POs", awaiting and all(r["fulfillment_status"] == "awaiting_grn" for r in awaiting))

        # -----------------------------------------------------------------
        print("\n--- follow-up: po_vs_received_shortfall() is legacy-only, no overlap with the canonical report ---")
        legacy_rows = reconcile.po_vs_received_shortfall(conn)
        legacy_po_numbers = {r["po_number"] for r in legacy_rows}
        ok &= check(
            "legacy report includes PO-LEGACY (product_id IS NULL, unresolved shortfall)",
            "PO-LEGACY" in legacy_po_numbers, f"got {legacy_po_numbers}",
        )
        if "PO-LEGACY" in legacy_po_numbers:
            legacy_row = next(r for r in legacy_rows if r["po_number"] == "PO-LEGACY")
            ok &= check("PO-LEGACY shortfall is 10 (ordered 10, received 0)", legacy_row["shortfall"] == 10, f"got {legacy_row['shortfall']}")
        canonical_po_numbers = {"PO-EXACT", "PO-SHORT", "PO-ABSENT", "PO-MULTILOT", "PO-COLLAPSED", "PO-IDENTITY"}
        ok &= check(
            "no canonical PO (product_id IS NOT NULL) appears in the legacy report",
            not (canonical_po_numbers & legacy_po_numbers),
            f"unexpected overlap: {canonical_po_numbers & legacy_po_numbers}",
        )
        legacy_sku_filtered = reconcile.po_vs_received_shortfall(conn, sku_code="DEMO-SKU-001")
        ok &= check(
            "legacy report with sku_code='DEMO-SKU-001' (a canonical external_sku) returns nothing",
            legacy_sku_filtered == [], f"got {legacy_sku_filtered}",
        )

        # -----------------------------------------------------------------
        print("\n--- 12: no inventory or commitment side effects ---")
        stock_before = reconcile.stock_by_location(conn)
        committed_before = [dict(r) for r in reconcile.committed_quantity(conn)]
        movements_before = conn.execute("SELECT COUNT(*) AS n FROM inventory_movements").fetchone()["n"]
        reconcile.official_po_grn_discrepancies(conn, "PO-SHORT")
        reconcile.official_discrepancies(conn)
        movements_after = conn.execute("SELECT COUNT(*) AS n FROM inventory_movements").fetchone()["n"]
        stock_after = reconcile.stock_by_location(conn)
        committed_after = [dict(r) for r in reconcile.committed_quantity(conn)]
        ok &= check("inventory_movements row count unchanged", movements_before == movements_after, f"{movements_before} -> {movements_after}")
        ok &= check("stock_by_location() output unchanged", stock_before == stock_after)
        ok &= check("committed_quantity() output unchanged", committed_before == committed_after)

        # -----------------------------------------------------------------
        print("\n--- lookup_document() wires the canonical comparison onto the PO, and has no dn handling left ---")
        result = reconcile.lookup_document(conn, "PO-SHORT")
        ok &= check("lookup_document result has po.discrepancies", result and "discrepancies" in result["po"])
        ok &= check("lookup_document result has no discrepancy_notes key", "discrepancy_notes" not in (result or {}))
        if result:
            ok &= check("po.discrepancies matches official_po_grn_discrepancies()", result["po"]["discrepancies"] == reconcile.official_po_grn_discrepancies(conn, "PO-SHORT"))

    finally:
        conn.close()
        print(f"\nDropping throwaway database {TEST_DB_NAME}...")
        drop_test_database()

    return ok


if __name__ == "__main__":
    ok = run()
    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    sys.exit(0 if ok else 1)

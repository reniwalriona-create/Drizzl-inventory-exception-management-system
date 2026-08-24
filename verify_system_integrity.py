"""
System integrity audit (Phase 11) -- read-only checks of canonical
invariants against whatever the live drizzl_inventory database currently
holds. This is a health check, not a test suite: it never writes
anything, and it never repairs what it finds -- a detected inconsistency
is reported for a human to investigate and fix deliberately (see
PROJECT_HANDOFF.md's void/restore and correction-workflow philosophy:
historical records are never silently mutated).

Run directly: `python3 verify_system_integrity.py`. Exits 0 if every
check passes, 1 if any check finds a violation.
"""
import sys

from db import get_connection


def check(conn, label, sql, params=()):
    """Runs a query expected to return ZERO rows when the invariant
    holds. Prints PASS/FAIL and, on failure, the offending row count and
    up to 5 example ids/keys so a human has somewhere to start looking."""
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print(f"  [PASS] {label}")
        return True
    examples = [dict(r) for r in rows[:5]]
    print(f"  [FAIL] {label} -- {len(rows)} violation(s), e.g. {examples}")
    return False


def run():
    conn = get_connection()
    ok = True
    try:
        print("=== Staging -> official linkage integrity ===")
        ok &= check(
            conn, "every staged PO's posted_po_id points at a real official PO",
            "SELECT staged_po_id, posted_po_id FROM staged_purchase_orders "
            "WHERE posted_po_id IS NOT NULL AND posted_po_id NOT IN (SELECT po_id FROM purchase_orders)",
        )
        ok &= check(
            conn, "every staged PO line's posted_line_item_id points at a real official line",
            "SELECT staged_line_id, posted_line_item_id FROM staged_po_lines "
            "WHERE posted_line_item_id IS NOT NULL AND posted_line_item_id NOT IN (SELECT id FROM po_line_items)",
        )
        ok &= check(
            conn, "every staged GRN's posted_grn_id points at a real official GRN",
            "SELECT staged_grn_id, posted_grn_id FROM staged_grns "
            "WHERE posted_grn_id IS NOT NULL AND posted_grn_id NOT IN (SELECT grn_id FROM grn_receipts)",
        )
        ok &= check(
            conn, "every staged GRN line's posted_grn_line_item_id points at a real official line",
            "SELECT staged_grn_line_id, posted_grn_line_item_id FROM staged_grn_lines "
            "WHERE posted_grn_line_item_id IS NOT NULL AND posted_grn_line_item_id NOT IN (SELECT id FROM grn_line_items)",
        )

        print("\n=== Canonical product identity integrity ===")
        ok &= check(
            conn, "every canonical GRN line (belongs to a po_id-linked GRN) has product_id set",
            """
            SELECT gli.id FROM grn_line_items gli
            JOIN grn_receipts gr ON gr.grn_id = gli.grn_id
            WHERE gr.po_id IS NOT NULL AND gli.product_id IS NULL
            """,
        )
        ok &= check(
            conn, "every GRN-sourced movement (source_grn_line_item_id set) has product_id set",
            "SELECT id FROM inventory_movements WHERE source_grn_line_item_id IS NOT NULL AND product_id IS NULL",
        )
        ok &= check(
            conn, "every GRN-sourced canonical SALE movement links back to its source GRN line",
            "SELECT id FROM inventory_movements "
            "WHERE movement_type = 'sale' AND reference_type = 'grn' "
            "AND product_id IS NOT NULL AND source_grn_line_item_id IS NULL",
        )
        ok &= check(
            conn, "no GRN line is linked from more than one movement (source_grn_line_item_id uniqueness)",
            "SELECT source_grn_line_item_id, COUNT(*) AS n FROM inventory_movements "
            "WHERE source_grn_line_item_id IS NOT NULL GROUP BY source_grn_line_item_id HAVING COUNT(*) > 1",
        )
        ok &= check(
            conn, "every canonical movement's sku_code matches master_products.barcode for its product_id",
            """
            SELECT m.id, m.sku_code, mp.barcode FROM inventory_movements m
            JOIN master_products mp ON mp.product_id = m.product_id
            WHERE m.product_id IS NOT NULL AND m.sku_code != mp.barcode
            """,
        )
        ok &= check(
            conn, "no legacy products.sku_code collides with a master_products.barcode (join-safety canary)",
            "SELECT p.sku_code FROM products p JOIN master_products mp ON mp.barcode = p.sku_code",
        )

        print("\n=== Active/voided GRN state invariants ===")
        ok &= check(
            conn, "every active canonical GRN's positive received lines have an active SALE movement",
            """
            SELECT gli.id AS grn_line_item_id FROM grn_line_items gli
            JOIN grn_receipts gr ON gr.grn_id = gli.grn_id
            WHERE gr.voided = 0 AND gr.po_id IS NOT NULL AND gli.product_id IS NOT NULL
              AND gli.received_qty IS NOT NULL AND gli.received_qty > 0
              AND NOT EXISTS (
                  SELECT 1 FROM inventory_movements m
                  WHERE m.source_grn_line_item_id = gli.id AND m.voided = 0
              )
            """,
        )
        ok &= check(
            conn, "no voided canonical GRN has an active SALE movement",
            """
            SELECT m.id AS movement_id FROM inventory_movements m
            JOIN grn_line_items gli ON gli.id = m.source_grn_line_item_id
            JOIN grn_receipts gr ON gr.grn_id = gli.grn_id
            WHERE gr.voided = 1 AND m.voided = 0
            """,
        )
        ok &= check(
            conn, "at most one active GRN per canonical PO",
            "SELECT po_id, COUNT(*) AS n FROM grn_receipts WHERE po_id IS NOT NULL AND voided = 0 "
            "GROUP BY po_id HAVING COUNT(*) > 1",
        )

        print("\n=== Supersession chain integrity ===")
        ok &= check(
            conn, "a GRN referenced by another's supersedes_grn_id is itself voided (never active)",
            """
            SELECT old.grn_id AS superseded_grn_id, new.grn_id AS superseding_grn_id
            FROM grn_receipts old
            JOIN grn_receipts new ON new.supersedes_grn_id = old.grn_id
            WHERE old.voided = 0
            """,
        )
        ok &= check(
            conn, "no GRN is superseded by more than one other GRN",
            "SELECT supersedes_grn_id, COUNT(*) AS n FROM grn_receipts "
            "WHERE supersedes_grn_id IS NOT NULL GROUP BY supersedes_grn_id HAVING COUNT(*) > 1",
        )

        print()
        if ok:
            print("ALL CHECKS PASSED")
        else:
            print("SOME CHECKS FAILED")
        return ok
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

"""
Controlled tests for the Committed/Uncommitted inventory layer (Layer 2 on
top of the physical negative-inventory warning system). Runs entirely
against an isolated in-memory database -- never touches the real
inventory.db. Mirrors the scenarios (A-G) from the spec this was built
against.

Core rule under test: Committed is NOT `PO ordered - GRN received`. The
moment ANY non-voided GRN line exists for a (po_number, sku_code), that
line's commitment drops to 0 for good -- a shortfall becomes a discrepancy,
not a remaining PO commitment.
"""
import sqlite3
import sys
from pathlib import Path

from ingest import (
    assign_po_source_location,
    record_movement,
    upsert_grn,
    upsert_po,
)
from reconcile import (
    committed_at_location,
    committed_quantity,
    current_balance,
    grn_discrepancies,
    stock_by_location,
    unallocated_commitments,
)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
SKU = "DEMO-SKU-001"


def fresh_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    conn.execute("INSERT INTO customers (name) VALUES ('Scootsy Logistics Private Limited')")
    conn.execute("INSERT INTO locations (name, type) VALUES ('Mumbai', 'own_facility')")
    conn.commit()
    return conn


def fake_po(po_number, sku_code, qty):
    return {
        "po_number": po_number,
        "po_date": "2026-08-14",
        "po_release_date": "2026-08-14",
        "payment_terms": "21 Days",
        "expected_delivery_date": "2026-08-28",
        "po_expiry_date": "2026-08-30",
        "vendor_name": "DRIZZL DEMO VENDOR",
        "vendor_gstin": "29AALCG4490J1Z0",
        "facility_name": "DEMO FACILITY A",
        "grand_total": 120.0 * qty,
        "line_items": [{
            "sno": "1", "item_code": sku_code, "item_desc": "Drizzl Passionfruit | Test",
            "hsn_code": "22029990", "qty": qty, "mrp": 120.0, "unit_base_cost": 60.0,
            "taxable_value": 60.0 * qty, "cgst_rate": 0, "cgst_amt": 0, "sgst_rate": 0,
            "sgst_amt": 0, "igst_rate": 0, "igst_amt": 0, "cess_rate": 0, "cess_amt": 0,
            "add_cess": 0, "total": 60.0 * qty,
        }],
    }


def fake_grn(grn_number, po_number, sku_code, expected_qty, received_qty):
    return {
        "grn_number": grn_number, "po_number": po_number, "grn_date": "2026-08-20",
        "inbound_no": "TEST-INB", "create_date": "2026-08-20", "invoice_no": "TEST-INV",
        "invoice_date": "2026-08-20", "challan_no": None, "challan_date": None,
        "vendor_name": "DRIZZL DEMO VENDOR", "facility_name": "DEMO FACILITY A",
        "line_items": [{
            "sku_code": sku_code, "sku_desc": "Test SKU", "lot_no": "LOT1", "lot_mrp": 100.0,
            "expected_qty": expected_qty, "received_qty": received_qty, "unit_price": 50.0,
            "taxable_value": 50.0 * received_qty, "total": 50.0 * received_qty,
        }],
    }


def on_hand(conn, location, sku_code=SKU):
    return current_balance(conn, location, sku_code)


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    return condition


def movement_check(conn, location, quantity):
    """Replicates app.py's new_movement() severity decision exactly, for
    a manual transfer/sale/loss of `quantity` at `location`, without
    going through Flask -- returns (severity, available, resulting,
    committed, uncommitted_resulting)."""
    available = current_balance(conn, location, SKU)
    resulting = available - quantity
    committed = uncommitted_resulting = None
    if resulting >= 0:
        committed = committed_at_location(conn, location, SKU)
        uncommitted_resulting = resulting - committed
    if resulting < 0:
        severity = "negative"
    elif uncommitted_resulting is not None and uncommitted_resulting < 0:
        severity = "commitment"
    else:
        severity = None
    return severity, available, resulting, committed, uncommitted_resulting


def test_a_commitment_only():
    print("Test A -- commitment only, no GRN yet")
    conn = fresh_conn()
    ok = True
    record_movement(conn, "2026-08-14", SKU, "opening_balance", 1000, location_to="Mumbai", reference_type="manual")
    conn.commit()
    upsert_po(conn, fake_po("PO-A", SKU, 600))
    assign_po_source_location(conn, "PO-A", "Mumbai")

    ok &= check("On Hand = 1000", on_hand(conn, "Mumbai") == 1000, f"got {on_hand(conn, 'Mumbai')}")
    committed = committed_at_location(conn, "Mumbai", SKU)
    ok &= check("Committed = 600", committed == 600, f"got {committed}")
    ok &= check("Uncommitted = 400", on_hand(conn, "Mumbai") - committed == 400)

    n_movements = conn.execute("SELECT COUNT(*) c FROM inventory_movements").fetchone()["c"]
    ok &= check("PO created no ledger movement of its own (only the 1 opening_balance)", n_movements == 1, f"got {n_movements}")
    conn.close()
    return ok


def test_b_safe_transfer():
    print("Test B -- safe manual transfer (stays within uncommitted)")
    conn = fresh_conn()
    ok = True
    record_movement(conn, "2026-08-14", SKU, "opening_balance", 1000, location_to="Mumbai", reference_type="manual")
    conn.commit()
    upsert_po(conn, fake_po("PO-B", SKU, 600))
    assign_po_source_location(conn, "PO-B", "Mumbai")

    severity, available, resulting, committed, uncommitted = movement_check(conn, "Mumbai", 300)
    ok &= check("no warning at all", severity is None, f"got {severity}")
    ok &= check("On Hand afterward = 700", resulting == 700, f"got {resulting}")
    ok &= check("Committed = 600", committed == 600)
    ok &= check("Uncommitted afterward = 100", uncommitted == 100, f"got {uncommitted}")
    conn.close()
    return ok


def test_c_uses_committed():
    print("Test C -- transfer eats into committed inventory")
    conn = fresh_conn()
    ok = True
    record_movement(conn, "2026-08-14", SKU, "opening_balance", 1000, location_to="Mumbai", reference_type="manual")
    conn.commit()
    upsert_po(conn, fake_po("PO-C", SKU, 600))
    assign_po_source_location(conn, "PO-C", "Mumbai")

    severity, available, resulting, committed, uncommitted = movement_check(conn, "Mumbai", 700)
    ok &= check("commitment warning (not negative)", severity == "commitment", f"got {severity}")
    ok &= check("On Hand afterward = 300 (still positive)", resulting == 300, f"got {resulting}")
    ok &= check("Uncommitted afterward = -300", uncommitted == -300, f"got {uncommitted}")
    conn.close()
    return ok


def test_d_physical_negative():
    print("Test D -- physical negative takes priority")
    conn = fresh_conn()
    ok = True
    record_movement(conn, "2026-08-14", SKU, "opening_balance", 200, location_to="Mumbai", reference_type="manual")
    conn.commit()

    severity, available, resulting, committed, uncommitted = movement_check(conn, "Mumbai", 250)
    ok &= check("strong negative warning", severity == "negative", f"got {severity}")
    ok &= check("On Hand afterward = -50", resulting == -50, f"got {resulting}")
    conn.close()
    return ok


def test_e_grn_fully_received():
    print("Test E -- GRN fully received resolves the PO cleanly")
    conn = fresh_conn()
    ok = True
    record_movement(conn, "2026-08-14", SKU, "opening_balance", 1000, location_to="Mumbai", reference_type="manual")
    conn.commit()
    upsert_po(conn, fake_po("PO-E", SKU, 600))
    assign_po_source_location(conn, "PO-E", "Mumbai")
    ok &= check("Committed = 600 before GRN", committed_at_location(conn, "Mumbai", SKU) == 600)

    upsert_grn(conn, fake_grn("GRN-E", "PO-E", SKU, 600, 600), source_file="test")

    sales = conn.execute(
        "SELECT * FROM inventory_movements WHERE reference_type='grn' AND reference_id='GRN-E' AND movement_type='sale'"
    ).fetchall()
    ok &= check("sale = 600 from Mumbai", sales and sales[0]["quantity"] == 600, f"got {sales[0]['quantity'] if sales else None}")
    ok &= check("On Hand after = 400", on_hand(conn, "Mumbai") == 400, f"got {on_hand(conn, 'Mumbai')}")
    ok &= check("Committed after = 0", committed_at_location(conn, "Mumbai", SKU) == 0, f"got {committed_at_location(conn, 'Mumbai', SKU)}")
    ok &= check("Uncommitted after = 400", on_hand(conn, "Mumbai") - committed_at_location(conn, "Mumbai", SKU) == 400)
    discrepancies = [r for r in grn_discrepancies(conn) if r["grn_number"] == "GRN-E"]
    ok &= check("no discrepancy", len(discrepancies) == 0, f"got {len(discrepancies)}")
    conn.close()
    return ok


def test_f_grn_large_discrepancy():
    print("Test F -- GRN with large discrepancy releases full commitment, no leftover")
    conn = fresh_conn()
    ok = True
    record_movement(conn, "2026-08-14", SKU, "opening_balance", 1000, location_to="Mumbai", reference_type="manual")
    conn.commit()
    upsert_po(conn, fake_po("PO-F", SKU, 600))
    assign_po_source_location(conn, "PO-F", "Mumbai")
    ok &= check("Committed = 600 before GRN", committed_at_location(conn, "Mumbai", SKU) == 600)

    upsert_grn(conn, fake_grn("GRN-F", "PO-F", SKU, 600, 200), source_file="test")

    sales = conn.execute(
        "SELECT * FROM inventory_movements WHERE reference_type='grn' AND reference_id='GRN-F' AND movement_type='sale'"
    ).fetchall()
    ok &= check("sale = 200 from Mumbai (not 600)", sales and sales[0]["quantity"] == 200, f"got {sales[0]['quantity'] if sales else None}")
    ok &= check("On Hand after = 800", on_hand(conn, "Mumbai") == 800, f"got {on_hand(conn, 'Mumbai')}")

    committed_after = committed_at_location(conn, "Mumbai", SKU)
    ok &= check("Committed after = 0 (NOT 400)", committed_after == 0, f"got {committed_after}")
    ok &= check("Uncommitted after = 800 (NOT 400)", on_hand(conn, "Mumbai") - committed_after == 800)

    discrepancies = [r for r in grn_discrepancies(conn) if r["grn_number"] == "GRN-F"]
    ok &= check("discrepancy_qty = 400 in the discrepancy workflow", discrepancies and discrepancies[0]["discrepancy_qty"] == 400,
                f"got {discrepancies[0]['discrepancy_qty'] if discrepancies else None}")
    conn.close()
    return ok


def test_g_unallocated_po():
    print("Test G -- unallocated PO commitment, then resolved by its GRN")
    conn = fresh_conn()
    ok = True
    upsert_po(conn, fake_po("PO-G", SKU, 600))
    # deliberately never call assign_po_source_location()

    unalloc = unallocated_commitments(conn)
    match = [u for u in unalloc if u["sku_code"] == SKU]
    ok &= check("shows as 600 unallocated commitment", match and match[0]["qty"] == 600, f"got {match}")
    ok &= check("Mumbai's committed_at_location is 0 (not guessed)", committed_at_location(conn, "Mumbai", SKU) == 0)

    upsert_grn(conn, fake_grn("GRN-G", "PO-G", SKU, 600, 250), source_file="test")

    sales = conn.execute(
        "SELECT * FROM inventory_movements WHERE reference_type='grn' AND reference_id='GRN-G' AND movement_type='sale'"
    ).fetchall()
    ok &= check("no sale movement created (no source location resolvable)", len(sales) == 0, f"got {len(sales)}")

    unalloc_after = [u for u in unallocated_commitments(conn) if u["sku_code"] == SKU]
    ok &= check("commitment closed once the GRN exists (no longer unallocated)", len(unalloc_after) == 0, f"got {unalloc_after}")

    still_committed = [r for r in committed_quantity(conn) if r["po_number"] == "PO-G"]
    ok &= check("PO-G no longer shows up as committed anywhere", len(still_committed) == 0, f"got {still_committed}")

    flags = conn.execute(
        "SELECT * FROM ingestion_flags WHERE document_type='grn' AND document_id='GRN-G' AND resolved=0"
    ).fetchall()
    ok &= check("GRN flagged as needing a source location", len(flags) == 1 and "source location" in flags[0]["issue"],
                f"got {[f['issue'] for f in flags]}")

    discrepancies = [r for r in grn_discrepancies(conn) if r["grn_number"] == "GRN-G"]
    ok &= check("expected-vs-received difference (350) still visible in the discrepancy workflow",
                discrepancies and discrepancies[0]["discrepancy_qty"] == 350,
                f"got {discrepancies[0]['discrepancy_qty'] if discrepancies else None}")
    conn.close()
    return ok


def main():
    results = [
        ("A: commitment only", test_a_commitment_only()),
        ("B: safe manual transfer", test_b_safe_transfer()),
        ("C: uses committed inventory", test_c_uses_committed()),
        ("D: physical negative", test_d_physical_negative()),
        ("E: GRN fully received", test_e_grn_fully_received()),
        ("F: GRN with large discrepancy", test_f_grn_large_discrepancy()),
        ("G: unallocated PO", test_g_unallocated_po()),
    ]
    print()
    print("=== Summary ===")
    all_ok = True
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} -- {name}")
        all_ok &= ok
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

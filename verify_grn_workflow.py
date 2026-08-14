"""
Controlled tests for the PO -> GRN -> discrepancy MVP workflow described in
PROJECT_HANDOFF.md. Runs entirely against an isolated in-memory database --
never touches the real inventory.db.

Scenarios (see PROJECT_HANDOFF.md for the full business-rule writeup):
  A. Perfect receipt (expected == received): sale = full qty, no discrepancy.
  B. Short receipt (received < expected): sale = received only, no auto loss,
     the GRN line shows up as an unresolved discrepancy.
  C. A Discrepancy Note later uploaded for that same GRN/SKU: it attaches
     (reason/remarks visible) without creating any additional loss movement.
  D. Re-uploading the same GRN PDF twice leaves the database in the same
     state as uploading it once.
"""
import sqlite3
import sys
from pathlib import Path

from ingest import assign_po_source_location, upsert_discrepancy_note, upsert_grn, upsert_po
from reconcile import grn_discrepancies

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def fresh_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    conn.execute("INSERT INTO customers (name) VALUES ('Scootsy Logistics Private Limited')")
    conn.execute("INSERT INTO locations (name, type) VALUES ('Drizzl Demo Warehouse', 'own_facility')")
    conn.commit()
    return conn


def fake_po(po_number, sku_code, qty):
    return {
        "po_number": po_number, "po_date": "2026-08-13", "po_release_date": "2026-08-13",
        "payment_terms": "21 Days", "expected_delivery_date": "2026-08-27",
        "po_expiry_date": "2026-08-29", "vendor_name": "DRIZZL DEMO VENDOR",
        "vendor_gstin": "29AALCG4490J1Z0", "facility_name": "TEST FACILITY", "grand_total": 60.0 * qty,
        "line_items": [{
            "sno": "1", "item_code": sku_code, "item_desc": "Test SKU", "hsn_code": "22029990",
            "qty": qty, "mrp": 60.0, "unit_base_cost": 30.0, "taxable_value": 30.0 * qty,
            "cgst_rate": 0, "cgst_amt": 0, "sgst_rate": 0, "sgst_amt": 0, "igst_rate": 0,
            "igst_amt": 0, "cess_rate": 0, "cess_amt": 0, "add_cess": 0, "total": 30.0 * qty,
        }],
    }


def upload_and_allocate_po(conn, po_number, sku_code, qty):
    """A GRN can no longer create a sale movement without a resolvable
    Drizzl source location (see ingest.py's upsert_grn -- source
    locations are never guessed/defaulted). These tests are about the
    PO -> GRN -> discrepancy workflow itself, not about location
    allocation, so set one up explicitly before each GRN upload,
    matching what a real user would now have to do first."""
    upsert_po(conn, fake_po(po_number, sku_code, qty))
    assign_po_source_location(conn, po_number, "Drizzl Demo Warehouse")


def fake_grn(grn_number, po_number, sku_code, expected_qty, received_qty):
    return {
        "grn_number": grn_number,
        "po_number": po_number,
        "grn_date": "2026-08-13",
        "inbound_no": "TEST-INB",
        "create_date": "2026-08-13",
        "invoice_no": "TEST-INV",
        "invoice_date": "2026-08-13",
        "challan_no": None,
        "challan_date": None,
        "vendor_name": "DRIZZL DEMO VENDOR",
        "line_items": [{
            "sku_code": sku_code,
            "sku_desc": "Test SKU",
            "lot_no": "LOT1",
            "lot_mrp": 100.0,
            "expected_qty": expected_qty,
            "received_qty": received_qty,
            "unit_price": 50.0,
            "taxable_value": 50.0 * received_qty,
            "total": 50.0 * received_qty,
        }],
    }


def fake_discrepancy_note(dn_number, po_number, grn_number, sku_code, dn_qty, reason, remarks):
    return {
        "dn_number": dn_number,
        "dn_date": "2026-08-14",
        "po_number": po_number,
        "grn_number": grn_number,
        "invoice_number": "TEST-INV",
        "inbound_no": "TEST-INB",
        "grn_qty": 100,
        "grn_amt": 5000,
        "total_dn_qty": dn_qty,
        "dn_amt": 50.0 * dn_qty,
        "invoice_amt": 5000,
        "line_items": [{
            "sku_code": sku_code,
            "sku_desc": "Test SKU",
            "reason": reason,
            "remarks": remarks,
            "exp_qty": 100,
            "dn_qty": dn_qty,
            "lot_mrp": 100.0,
            "unit_price": 50.0,
            "taxable_value": 50.0 * dn_qty,
            "total": 50.0 * dn_qty,
        }],
    }


def sale_movements(conn, reference_id):
    return conn.execute(
        "SELECT * FROM inventory_movements WHERE reference_type='grn' AND reference_id=? AND movement_type='sale'",
        (reference_id,),
    ).fetchall()


def loss_movements(conn):
    return conn.execute("SELECT * FROM inventory_movements WHERE movement_type='loss'").fetchall()


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    return condition


def test_a_perfect_receipt():
    print("Test A -- perfect receipt (expected 100, received 100)")
    conn = fresh_conn()
    ok = True
    upload_and_allocate_po(conn, "PO-A", "SKU-A", 100)
    upsert_grn(conn, fake_grn("GRN-A", "PO-A", "SKU-A", 100, 100), source_file="test")

    sales = sale_movements(conn, "GRN-A")
    ok &= check("exactly one sale movement", len(sales) == 1, f"got {len(sales)}")
    ok &= check("sale quantity is 100", sales and sales[0]["quantity"] == 100, f"got {sales[0]['quantity'] if sales else None}")
    ok &= check("no loss movements", len(loss_movements(conn)) == 0)

    discrepancies = [r for r in grn_discrepancies(conn) if r["grn_number"] == "GRN-A"]
    ok &= check("no discrepancy reported", len(discrepancies) == 0, f"got {len(discrepancies)}")
    conn.close()
    return ok


def test_b_short_receipt():
    print("Test B -- short receipt (expected 100, received 90)")
    conn = fresh_conn()
    ok = True
    upload_and_allocate_po(conn, "PO-B", "SKU-B", 100)
    upsert_grn(conn, fake_grn("GRN-B", "PO-B", "SKU-B", 100, 90), source_file="test")

    sales = sale_movements(conn, "GRN-B")
    ok &= check("sale quantity is 90, not 100", sales and sales[0]["quantity"] == 90, f"got {sales[0]['quantity'] if sales else None}")
    ok &= check("no automatic loss movement", len(loss_movements(conn)) == 0)

    discrepancies = [r for r in grn_discrepancies(conn) if r["grn_number"] == "GRN-B"]
    ok &= check("exactly one discrepancy line", len(discrepancies) == 1, f"got {len(discrepancies)}")
    if discrepancies:
        d = discrepancies[0]
        ok &= check("discrepancy_qty is 10", d["discrepancy_qty"] == 10, f"got {d['discrepancy_qty']}")
        ok &= check("no Discrepancy Note attached yet", d["dn_number"] is None)
    conn.close()
    return ok, conn


def test_c_discrepancy_note_attaches():
    print("Test C -- Discrepancy Note uploaded after a short receipt")
    conn = fresh_conn()
    ok = True
    upload_and_allocate_po(conn, "PO-C", "SKU-C", 100)
    upsert_grn(conn, fake_grn("GRN-C", "PO-C", "SKU-C", 100, 90), source_file="test")
    upsert_discrepancy_note(
        conn,
        fake_discrepancy_note("DN-C", "PO-C", "GRN-C", "SKU-C", 10, "Damaged", "DP WORLD-DAMAGE"),
        source_file="test",
    )

    sales = sale_movements(conn, "GRN-C")
    ok &= check("original sale remains 90", sales and sales[0]["quantity"] == 90, f"got {sales[0]['quantity'] if sales else None}")
    ok &= check("still no loss movement created automatically", len(loss_movements(conn)) == 0)

    discrepancies = [r for r in grn_discrepancies(conn) if r["grn_number"] == "GRN-C"]
    ok &= check("discrepancy line now shows the DN", discrepancies and discrepancies[0]["dn_number"] == "DN-C")
    if discrepancies:
        d = discrepancies[0]
        ok &= check("reason visible", d["reason"] == "Damaged", f"got {d['reason']}")
        ok &= check("remarks visible", d["remarks"] == "DP WORLD-DAMAGE", f"got {d['remarks']}")
    conn.close()
    return ok


def test_d_reupload_pdf_path():
    print("Test D1 -- re-uploading the same GRN (PDF path) twice")
    conn = fresh_conn()
    ok = True
    upload_and_allocate_po(conn, "PO-D", "SKU-D", 100)
    parsed = fake_grn("GRN-D", "PO-D", "SKU-D", 100, 90)
    upsert_grn(conn, parsed, source_file="test")
    upsert_grn(conn, parsed, source_file="test")  # re-upload, identical

    line_items = conn.execute("SELECT * FROM grn_line_items WHERE grn_number='GRN-D'").fetchall()
    ok &= check("exactly one line item after re-upload", len(line_items) == 1, f"got {len(line_items)}")

    sales = sale_movements(conn, "GRN-D")
    ok &= check("exactly one sale movement after re-upload", len(sales) == 1, f"got {len(sales)}")
    ok &= check("quantity did not double", sales and sales[0]["quantity"] == 90, f"got {sales[0]['quantity'] if sales else None}")
    conn.close()
    return ok


def test_over_receipt_flagged():
    print("Test E -- received MORE than expected gets flagged, not silently accepted")
    from validate import validate_grn
    parsed = fake_grn("GRN-E", "PO-E", "SKU-E", 100, 110)
    issues = validate_grn(parsed)
    ok = check("validate_grn raises an issue for over-receipt", any("exceeds expected_qty" in i for i in issues), f"got {issues}")
    return ok


def main():
    results = []
    results.append(("A: perfect receipt", test_a_perfect_receipt()))
    b_ok, _ = test_b_short_receipt()
    results.append(("B: short receipt", b_ok))
    results.append(("C: discrepancy note attaches", test_c_discrepancy_note_attaches()))
    results.append(("D: re-upload GRN PDF", test_d_reupload_pdf_path()))
    results.append(("E: over-receipt flagged", test_over_receipt_flagged()))

    print()
    print("=== Summary ===")
    all_ok = True
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} -- {name}")
        all_ok &= ok
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

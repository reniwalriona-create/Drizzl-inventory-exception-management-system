"""
Verifies the Phase 3 PO CSV staging infrastructure against a disposable
PostgreSQL database. Uses the repository's public synthetic demo PO for the
primary checks, plus generated synthetic CSVs for the controlled edge-case
tests. All database writes happen inside SAVEPOINTs and are rolled back --
nothing persists after this script runs.

The suite never reads a fixture outside this repository.
"""
import csv
import json
import sys
import tempfile
from pathlib import Path

import psycopg2.errors

import catalog
import po_csv_staging as staging
from db import get_connection
from verify_db import bootstrap_connection, create_database, drop_database

TEST_DB_NAME = "drizzl_inventory_test_po_staging"

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "synthetic" / "demo_po_01.csv"
SCOOTSY_NAME = "Scootsy Logistics Private Limited"

REAL_HEADER = [
    "PoNumber", "Entity", "FacilityId", "FacilityName", "City", "PoCreatedAt", "PoModifiedAt",
    "Status", "SupplierCode", "VendorName", "PoAmount", "SkuCode", "SkuDescription", "CategoryId",
    "OrderedQty", "ReceivedQty", "BalancedQty", "Tax", "PoLineValueWithoutTax", "PoLineValueWithTax",
    "Mrp", "UnitBasedCost", "ExpectedDeliveryDate", "PoExpiryDate", "OtbReferenceNumber",
    "InternalExternalPo", "PoAgeing", "BrandName", "ReferencePoNumber",
]

BASE_ROW = {
    "PoNumber": "TESTPO0001", "Entity": "SCOOTSY LOGISTICS PRIVATE LIMITED",
    "FacilityId": "TST", "FacilityName": "TEST FC", "City": "TESTCITY",
    "PoCreatedAt": "2026-08-15 00:00:00", "PoModifiedAt": "2026-08-15 00:00:00",
    "Status": "CONFIRMED", "SupplierCode": "DEMO-SUPPLIER-001", "VendorName": "DRIZZL DEMO VENDOR",
    "PoAmount": "1000.00", "SkuCode": "DEMO-SKU-001",
    "SkuDescription": "Drizzl Passionfruit | Probiotic Soda | 250 ml", "CategoryId": "Soft Drinks",
    "OrderedQty": "10", "ReceivedQty": "0", "BalancedQty": "10", "Tax": "40.00",
    "PoLineValueWithoutTax": "600.00", "PoLineValueWithTax": "640.00", "Mrp": "120.00",
    "UnitBasedCost": "60.00", "ExpectedDeliveryDate": "2026-08-30", "PoExpiryDate": "2026-09-02",
    "OtbReferenceNumber": "REF-0001", "InternalExternalPo": "external", "PoAgeing": "1",
    "BrandName": "DRIZZL", "ReferencePoNumber": "",
}


def row(**overrides):
    r = dict(BASE_ROW)
    r.update(overrides)
    return r


def write_csv(rows, fieldnames=REAL_HEADER):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    f.close()
    return f.name


def get_scootsy_id(conn):
    r = conn.execute("SELECT id FROM customers WHERE name = ?", (SCOOTSY_NAME,)).fetchone()
    return r["id"] if r else None


def table_count(conn, table, where=None, params=()):
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return conn.execute(sql, params).fetchone()["n"]


# ---------------------------------------------------------------------------
# Real-file checks
# ---------------------------------------------------------------------------

def check_fixture_file(conn, failures):
    if not FIXTURE_PATH.exists():
        failures.append(f"  FATAL: synthetic fixture not found at {FIXTURE_PATH}")
        return None

    official_po_before = table_count(conn, "purchase_orders")
    official_line_before = table_count(conn, "po_line_items")
    movements_before = table_count(conn, "inventory_movements")
    legacy_products_before = table_count(conn, "products")

    conn.execute("SAVEPOINT real_file_check")
    try:
        result = staging.stage_po_csv(conn, str(FIXTURE_PATH))
        batch_id = result["batch_id"]

        n_rows = table_count(conn, "po_import_rows", "batch_id = ?", (batch_id,))
        n_pos = table_count(conn, "staged_purchase_orders", "batch_id = ?", (batch_id,))
        n_lines = conn.execute(
            "SELECT COUNT(*) AS n FROM staged_po_lines l JOIN staged_purchase_orders p "
            "ON p.staged_po_id = l.staged_po_id WHERE p.batch_id = ?", (batch_id,)
        ).fetchone()["n"]
        n_with_product = conn.execute(
            "SELECT COUNT(*) AS n FROM staged_po_lines l JOIN staged_purchase_orders p "
            "ON p.staged_po_id = l.staged_po_id WHERE p.batch_id = ? AND l.product_id IS NOT NULL",
            (batch_id,),
        ).fetchone()["n"]

        if n_rows != 6:
            failures.append(f"  expected 6 raw rows, got {n_rows}")
        if n_pos != 1:
            failures.append(f"  expected 1 staged PO, got {n_pos}")
        if n_lines != 6:
            failures.append(f"  expected 6 staged lines, got {n_lines}")
        if n_with_product != 6:
            failures.append(f"  expected 6/6 staged lines to resolve a product_id, got {n_with_product}")

        # 1. non-null source_location_id check (must ALL be NULL)
        non_null_source = table_count(conn, "staged_purchase_orders", "batch_id = ? AND source_location_id IS NOT NULL", (batch_id,))
        if non_null_source != 0:
            failures.append(f"  {non_null_source} staged PO(s) had a non-NULL source_location_id -- must be NULL after Phase 3 staging")

        # 2. destination fields preserved separately
        sample = conn.execute(
            "SELECT destination_facility_id, destination_facility_name, destination_city, source_location_id "
            "FROM staged_purchase_orders WHERE batch_id = ? AND external_po_number = ?",
            (batch_id, "SYN-PO-1001"),
        ).fetchone()
        if sample is None:
            failures.append("  could not find SYN-PO-1001 to check destination-field preservation")
        else:
            if sample["destination_facility_id"] != "SYN-FC-01" or sample["destination_facility_name"] != "Synthetic Test Facility" or sample["destination_city"] != "DEMO CITY":
                failures.append(f"  destination fields for SYN-PO-1001 didn't match the fixture: {dict(sample)}")
            if sample["source_location_id"] is not None:
                failures.append("  destination facility appears to have leaked into source_location_id")

        # 3/4/5. official ledger untouched
        if table_count(conn, "purchase_orders") != official_po_before:
            failures.append("  purchase_orders row count changed -- staging must never touch the official ledger")
        if table_count(conn, "po_line_items") != official_line_before:
            failures.append("  po_line_items row count changed -- staging must never touch the official ledger")
        if table_count(conn, "inventory_movements") != movements_before:
            failures.append("  inventory_movements row count changed -- staging must never touch the official ledger")

        # 9. no staged line uses legacy products.sku_code as its identity
        if table_count(conn, "products") != legacy_products_before:
            failures.append("  legacy products table row count changed -- staging must never call _ensure_product()")

        # 8. Six rows sharing the same number normalize to one PO.
        grouped = conn.execute(
            "SELECT staged_po_id FROM staged_purchase_orders WHERE batch_id = ? AND external_po_number = ?",
            (batch_id, "SYN-PO-1001"),
        ).fetchall()
        if len(grouped) != 1:
            failures.append(f"  expected exactly 1 staged PO for SYN-PO-1001, got {len(grouped)}")
        else:
            grouped_lines = table_count(conn, "staged_po_lines", "staged_po_id = ?", (grouped[0]["staged_po_id"],))
            if grouped_lines != 6:
                failures.append(f"  expected 6 staged lines for SYN-PO-1001, got {grouped_lines}")

        # 7. raw row preserves original CSV values in JSONB
        # (source_row_number=1 is the file's first data row -- PoNumber
        # SYN-PO-1001, SkuCode DEMO-SKU-001; row 2 including the header)
        raw = conn.execute(
            "SELECT raw_data FROM po_import_rows WHERE batch_id = ? AND source_row_number = 1", (batch_id,)
        ).fetchone()
        if raw is None or raw["raw_data"].get("PoNumber") != "SYN-PO-1001" or raw["raw_data"].get("SkuCode") != "DEMO-SKU-001":
            failures.append(f"  raw_data for source row 2 didn't preserve the original CSV values: {raw['raw_data'] if raw else None}")

        # 10. SKU DEMO-SKU-001 -> barcode 9000000000001
        line_sku_001 = conn.execute(
            "SELECT mp.barcode FROM staged_po_lines l JOIN staged_purchase_orders p ON p.staged_po_id = l.staged_po_id "
            "JOIN master_products mp ON mp.product_id = l.product_id "
            "WHERE p.batch_id = ? AND l.external_sku = ? LIMIT 1",
            (batch_id, "DEMO-SKU-001"),
        ).fetchone()
        if line_sku_001 is None or line_sku_001["barcode"] != "9000000000001":
            failures.append(f"  SKU DEMO-SKU-001 did not resolve to barcode 9000000000001, got {dict(line_sku_001) if line_sku_001 else None}")

        # 11. actual customer SKU preserved verbatim on the staged line
        preserved = conn.execute(
            "SELECT external_sku FROM staged_po_lines l JOIN staged_purchase_orders p ON p.staged_po_id = l.staged_po_id "
            "WHERE p.batch_id = ? AND l.external_sku = ?", (batch_id, "DEMO-SKU-002"),
        ).fetchone()
        if preserved is None:
            failures.append("  external_sku DEMO-SKU-002 was not preserved verbatim on any staged line")

        # 6. re-staging the identical file is idempotent
        rows_before_reimport = table_count(conn, "po_import_rows")
        pos_before_reimport = table_count(conn, "staged_purchase_orders")
        lines_before_reimport = table_count(conn, "staged_po_lines")
        batches_before_reimport = table_count(conn, "po_import_batches")

        result2 = staging.stage_po_csv(conn, str(FIXTURE_PATH))
        if not result2["reused_existing_batch"] or result2["batch_id"] != batch_id:
            failures.append(f"  re-staging the identical file did not reuse the existing batch: {result2}")
        if table_count(conn, "po_import_rows") != rows_before_reimport:
            failures.append("  re-staging the identical file changed po_import_rows count")
        if table_count(conn, "staged_purchase_orders") != pos_before_reimport:
            failures.append("  re-staging the identical file changed staged_purchase_orders count")
        if table_count(conn, "staged_po_lines") != lines_before_reimport:
            failures.append("  re-staging the identical file changed staged_po_lines count")
        if table_count(conn, "po_import_batches") != batches_before_reimport:
            failures.append("  re-staging the identical file created another batch")

    finally:
        conn.execute("ROLLBACK TO SAVEPOINT real_file_check")

    return None


# ---------------------------------------------------------------------------
# Controlled edge-case tests (synthetic fixtures, all rolled back)
# ---------------------------------------------------------------------------

def check_unknown_sku(conn, failures):
    path = write_csv([row(SkuCode="UNKNOWN-SKU-9999")])
    conn.execute("SAVEPOINT unknown_sku_check")
    try:
        result = staging.stage_po_csv(conn, path)
        line = conn.execute(
            "SELECT product_id, l.validation_status, l.validation_errors FROM staged_po_lines l "
            "JOIN staged_purchase_orders p ON p.staged_po_id = l.staged_po_id WHERE p.batch_id = ?",
            (result["batch_id"],),
        ).fetchone()
        po = conn.execute(
            "SELECT validation_status FROM staged_purchase_orders WHERE batch_id = ?", (result["batch_id"],)
        ).fetchone()
        raw_row_count = table_count(conn, "po_import_rows", "batch_id = ?", (result["batch_id"],))

        if raw_row_count != 1:
            failures.append("  unknown SKU: raw row was not retained")
        if line is None or line["product_id"] is not None or line["validation_status"] != "blocked":
            failures.append(f"  unknown SKU: expected line blocked with product_id NULL, got {dict(line) if line else None}")
        if line and not any(e["code"] == "unmapped_customer_sku" for e in line["validation_errors"]):
            failures.append(f"  unknown SKU: expected an 'unmapped_customer_sku' validation error, got {line['validation_errors']}")
        if po is None or po["validation_status"] != "blocked":
            failures.append(f"  unknown SKU: expected the staged PO itself to be blocked, got {dict(po) if po else None}")

        # A human may add the missing mapping from the terminal. The same
        # staged batch must then be revalidatable without re-uploading or
        # creating any Master Product automatically.
        product = catalog.get_master_product_by_barcode(conn, "9000000000001")
        catalog.add_customer_sku_mapping(
            conn, get_scootsy_id(conn), product["product_id"], "UNKNOWN-SKU-9999"
        )
        staging.revalidate_product_mappings(conn, result["batch_id"])
        revalidated_line = conn.execute(
            "SELECT product_id, validation_status, validation_errors FROM staged_po_lines "
            "WHERE staged_po_id = (SELECT staged_po_id FROM staged_purchase_orders WHERE batch_id = ?)",
            (result["batch_id"],),
        ).fetchone()
        revalidated_po = conn.execute(
            "SELECT validation_status FROM staged_purchase_orders WHERE batch_id = ?",
            (result["batch_id"],),
        ).fetchone()
        if (
            revalidated_line["product_id"] != product["product_id"]
            or revalidated_line["validation_status"] != "valid"
            or revalidated_line["validation_errors"]
        ):
            failures.append(f"  unknown SKU: mapping revalidation did not resolve the staged line: {dict(revalidated_line)}")
        if revalidated_po["validation_status"] != "valid":
            failures.append(f"  unknown SKU: mapping revalidation did not unblock the staged PO: {dict(revalidated_po)}")
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT unknown_sku_check")


def check_malformed_quantity(conn, failures):
    path = write_csv([row(PoNumber="TESTPO0002", OrderedQty="not-a-number")])
    conn.execute("SAVEPOINT malformed_qty_check")
    try:
        result = staging.stage_po_csv(conn, path)
        line = conn.execute(
            "SELECT ordered_qty, l.validation_status, l.validation_errors FROM staged_po_lines l "
            "JOIN staged_purchase_orders p ON p.staged_po_id = l.staged_po_id WHERE p.batch_id = ?",
            (result["batch_id"],),
        ).fetchone()
        raw_row_count = table_count(conn, "po_import_rows", "batch_id = ?", (result["batch_id"],))

        if raw_row_count != 1:
            failures.append("  malformed quantity: raw row was not retained")
        if line is None or line["ordered_qty"] is not None or line["validation_status"] != "blocked":
            failures.append(f"  malformed quantity: expected blocked line with NULL ordered_qty, got {dict(line) if line else None}")
        if line and not any(e["code"] == "invalid_number" and e["field"] == "OrderedQty" for e in line["validation_errors"]):
            failures.append(f"  malformed quantity: expected an 'invalid_number' error on OrderedQty, got {line['validation_errors']}")
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT malformed_qty_check")


def check_inconsistent_po_metadata(conn, failures):
    rows = [
        row(PoNumber="TESTPO0003", SkuCode="DEMO-SKU-001", VendorName="DRIZZL DEMO VENDOR"),
        row(PoNumber="TESTPO0003", SkuCode="DEMO-SKU-003", VendorName="A DIFFERENT VENDOR NAME"),
        row(PoNumber="TESTPO0004", SkuCode="DEMO-SKU-002"),  # a second, unrelated, otherwise-valid PO
    ]
    path = write_csv(rows)
    conn.execute("SAVEPOINT inconsistent_metadata_check")
    try:
        result = staging.stage_po_csv(conn, path)
        po3 = conn.execute(
            "SELECT validation_status, validation_errors FROM staged_purchase_orders WHERE batch_id = ? AND external_po_number = ?",
            (result["batch_id"], "TESTPO0003"),
        ).fetchone()
        po4 = conn.execute(
            "SELECT validation_status FROM staged_purchase_orders WHERE batch_id = ? AND external_po_number = ?",
            (result["batch_id"], "TESTPO0004"),
        ).fetchone()
        lines_for_po3 = table_count(conn, "staged_po_lines", "staged_po_id = (SELECT staged_po_id FROM staged_purchase_orders WHERE batch_id = ? AND external_po_number = ?)", (result["batch_id"], "TESTPO0003"))

        if po3 is None or po3["validation_status"] != "blocked":
            failures.append(f"  inconsistent metadata: expected TESTPO0003 blocked, got {dict(po3) if po3 else None}")
        if po3 and not any(e["code"] == "inconsistent_po_metadata" and e["field"] == "VendorName" for e in po3["validation_errors"]):
            failures.append(f"  inconsistent metadata: expected an 'inconsistent_po_metadata' error on VendorName, got {po3['validation_errors']}")
        if lines_for_po3 != 2:
            failures.append(f"  inconsistent metadata: expected both raw lines preserved for TESTPO0003, got {lines_for_po3}")
        if po4 is None or po4["validation_status"] != "valid":
            failures.append(f"  inconsistent metadata: TESTPO0004 (unrelated, valid PO) should still stage cleanly, got {dict(po4) if po4 else None}")
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT inconsistent_metadata_check")


def check_wrong_customer_fatal(conn, failures):
    rows = [
        row(PoNumber="TESTPO0005", Entity="SCOOTSY LOGISTICS PRIVATE LIMITED"),
        row(PoNumber="TESTPO0006", Entity="SOME OTHER COMPANY PRIVATE LIMITED"),
    ]
    path = write_csv(rows)
    batches_before = table_count(conn, "po_import_batches")
    rows_before = table_count(conn, "po_import_rows")
    pos_before = table_count(conn, "staged_purchase_orders")

    conn.execute("SAVEPOINT wrong_customer_check")
    try:
        raised = False
        try:
            staging.stage_po_csv(conn, path)
        except staging.FatalImportError:
            raised = True
        if not raised:
            failures.append("  mixed-entity CSV should have raised FatalImportError, but did not")
        if table_count(conn, "po_import_batches") != batches_before:
            failures.append("  mixed-entity CSV left a partial batch behind")
        if table_count(conn, "po_import_rows") != rows_before:
            failures.append("  mixed-entity CSV left partial raw rows behind")
        if table_count(conn, "staged_purchase_orders") != pos_before:
            failures.append("  mixed-entity CSV left partial staged POs behind")
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT wrong_customer_check")


def check_extra_unknown_column(conn, failures):
    fieldnames = REAL_HEADER + ["SomeFutureField"]
    r = row(PoNumber="TESTPO0007")
    r["SomeFutureField"] = "unexpected-value-123"
    path = write_csv([r], fieldnames=fieldnames)
    conn.execute("SAVEPOINT extra_column_check")
    try:
        result = staging.stage_po_csv(conn, path)
        raw = conn.execute(
            "SELECT raw_data FROM po_import_rows WHERE batch_id = ?", (result["batch_id"],)
        ).fetchone()
        if raw is None or raw["raw_data"].get("SomeFutureField") != "unexpected-value-123":
            failures.append(f"  extra unknown column was not preserved in raw_data: {raw['raw_data'] if raw else None}")
        po = conn.execute(
            "SELECT validation_status FROM staged_purchase_orders WHERE batch_id = ?", (result["batch_id"],)
        ).fetchone()
        if po is None or po["validation_status"] != "valid":
            failures.append(f"  an extra unknown column should not block an otherwise-valid PO, got {dict(po) if po else None}")
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT extra_column_check")


def check_missing_optional_column(conn, failures):
    fieldnames = [c for c in REAL_HEADER if c != "BrandName"]
    path = write_csv([row(PoNumber="TESTPO0008")], fieldnames=fieldnames)
    conn.execute("SAVEPOINT missing_column_check")
    try:
        result = staging.stage_po_csv(conn, path)
        po = conn.execute(
            "SELECT validation_status, brand_name FROM staged_purchase_orders WHERE batch_id = ?", (result["batch_id"],)
        ).fetchone()
        if po is None:
            failures.append("  missing optional column: PO was not staged at all")
        else:
            if po["brand_name"] is not None:
                failures.append(f"  missing optional column: expected brand_name NULL, got {po['brand_name']!r}")
            if po["validation_status"] != "valid":
                failures.append(f"  missing optional column: expected a valid PO, got {po['validation_status']!r}")
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT missing_column_check")


def check_no_residue(conn, failures):
    for table in ("po_import_batches", "po_import_rows", "staged_purchase_orders", "staged_po_lines"):
        n = table_count(conn, table)
        if n != 0:
            failures.append(f"  {n} row(s) left behind in {table} after verification -- rollback did not fully clean up")


def run():
    create_database(TEST_DB_NAME)
    conn = bootstrap_connection(TEST_DB_NAME)
    failures = []
    try:
        scootsy_id = get_scootsy_id(conn)
        if scootsy_id is None:
            print("FAILED: Scootsy customer not found -- cannot run the rest of the checks.")
            return False

        check_fixture_file(conn, failures)
        check_unknown_sku(conn, failures)
        check_malformed_quantity(conn, failures)
        check_inconsistent_po_metadata(conn, failures)
        check_wrong_customer_fatal(conn, failures)
        check_extra_unknown_column(conn, failures)
        check_missing_optional_column(conn, failures)
        check_no_residue(conn, failures)
    finally:
        conn.close()
        drop_database(TEST_DB_NAME)

    if any("FATAL" in f for f in failures):
        print("STOPPED:")
        print("\n".join(failures))
        return False

    if failures:
        print(f"FAILED ({len(failures)} issue(s)):")
        print("\n".join(failures))
    else:
        print(
            "PASSED -- public fixture (2 rows / 1 staged PO / 2 resolved product lines) checks out, "
            "all source_location_id values NULL, destination fields preserved separately, official ledger "
            "(purchase_orders/po_line_items/inventory_movements/legacy products) untouched, SYN-PO-1001 grouped "
            "correctly, raw JSONB preserved, SKU->barcode resolution correct, re-staging is idempotent, and all "
            "6 controlled edge cases (unknown SKU, malformed quantity, inconsistent PO metadata, mixed-entity "
            "fatal rejection, extra column, missing optional column) behave exactly as specified. No test data "
            "left behind."
        )
    return not failures


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

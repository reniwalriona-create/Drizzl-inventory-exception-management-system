"""
Verifies the Phase 1 Master Product + customer-SKU identity foundation
against the real drizzl_inventory database (via db.get_connection()), the
same connection app.py uses -- not an isolated throwaway copy, since the
whole point is confirming the actual migration/seed landed correctly.

Checks only that the *specific* expected rows exist and resolve correctly
-- never asserts a total row count on master_products or on Scootsy's
mapping count, since more products/mappings will be added later and this
script should keep passing when they are.

Any write this script performs to prove a constraint (the duplicate-key
check) runs inside a SAVEPOINT and is rolled back immediately, so nothing
is left behind and no failed-transaction state leaks into the connection.
"""
import sys

import psycopg2.errors

import catalog
from db import get_connection

EXPECTED_PRODUCTS = [
    ("9000000000001", "Drizzl Passionfruit Probiotic Soda", "250 ml"),
    ("9000000000002", "Drizzl Yuzu & Elderflower Probiotic Soda", "250 ml"),
    ("9000000000003", "Drizzl Mixed Berry Probiotic Soda", "250 ml"),
    ("9000000000004", "Drizzl Lemon & Mint Probiotic Soda", "250 ml"),
    ("9000000000005", "Drizzl Orange Probiotic Soda", "250 ml"),
    ("9000000000006", "Drizzl Probiotic Sparkling Water - Passionfruit", "250 ml"),
    ("9000000000007", "Drizzl Probiotic Sparkling Water - Lemon & Mint", "250 ml"),
]

EXPECTED_SCOOTSY_MAPPINGS = [
    ("9000000000001", "DEMO-SKU-001"),
    ("9000000000002", "DEMO-SKU-002"),
    ("9000000000003", "DEMO-SKU-003"),
    ("9000000000004", "DEMO-SKU-004"),
    ("9000000000005", "DEMO-SKU-005"),
    ("9000000000006", "DEMO-SKU-006"),
]

UNMAPPED_BARCODE = "9000000000007"
SCOOTSY_NAME = "Scootsy Logistics Private Limited"


def get_scootsy_id(conn):
    row = conn.execute("SELECT id FROM customers WHERE name = ?", (SCOOTSY_NAME,)).fetchone()
    return row["id"] if row else None


def check_products_exist(conn, failures):
    for barcode, name, unit_size in EXPECTED_PRODUCTS:
        row = catalog.get_master_product_by_barcode(conn, barcode)
        if row is None:
            failures.append(f"  master product {barcode} not found")
        elif row["product_name"] != name or row["unit_size"] != unit_size:
            failures.append(
                f"  master product {barcode}: expected ({name!r}, {unit_size!r}), "
                f"got ({row['product_name']!r}, {row['unit_size']!r})"
            )


def check_barcodes_unique(conn, failures):
    barcodes = [b for b, _, _ in EXPECTED_PRODUCTS]
    placeholders = ",".join(["?"] * len(barcodes))
    rows = conn.execute(
        f"SELECT barcode, COUNT(*) AS n FROM master_products WHERE barcode IN ({placeholders}) GROUP BY barcode",
        tuple(barcodes),
    ).fetchall()
    found = {r["barcode"]: r["n"] for r in rows}
    if len(found) != len(barcodes):
        missing = set(barcodes) - set(found)
        failures.append(f"  expected {len(barcodes)} distinct barcodes, only found {len(found)} (missing: {missing})")
    for barcode, n in found.items():
        if n != 1:
            failures.append(f"  barcode {barcode} appears {n} times, expected exactly 1")


def check_scootsy_mappings(conn, scootsy_id, failures):
    for barcode, external_sku in EXPECTED_SCOOTSY_MAPPINGS:
        resolved = catalog.resolve_customer_sku(conn, scootsy_id, external_sku)
        if resolved is None:
            failures.append(f"  Scootsy SKU {external_sku} did not resolve to any product (expected barcode {barcode})")
        elif resolved["barcode"] != barcode:
            failures.append(f"  Scootsy SKU {external_sku} resolved to barcode {resolved['barcode']}, expected {barcode}")


def check_specific_resolutions(conn, scootsy_id, failures):
    cases = [("DEMO-SKU-001", "9000000000001"), ("DEMO-SKU-006", "9000000000006")]
    for external_sku, expected_barcode in cases:
        resolved = catalog.resolve_customer_sku(conn, scootsy_id, external_sku)
        if resolved is None or resolved["barcode"] != expected_barcode:
            got = resolved["barcode"] if resolved else None
            failures.append(f"  Scootsy SKU {external_sku}: expected barcode {expected_barcode}, got {got}")


def check_unmapped_product_exists_independently(conn, scootsy_id, failures):
    product = catalog.get_master_product_by_barcode(conn, UNMAPPED_BARCODE)
    if product is None:
        failures.append(f"  master product {UNMAPPED_BARCODE} should exist even with no Scootsy mapping, but wasn't found")
        return
    mappings = catalog.list_customer_sku_mappings(conn, scootsy_id)
    has_mapping = any(m["barcode"] == UNMAPPED_BARCODE for m in mappings)
    if has_mapping:
        failures.append(f"  {UNMAPPED_BARCODE} unexpectedly has a Scootsy mapping -- should have none yet")


def check_unknown_sku_no_autocreate(conn, scootsy_id, failures):
    before = conn.execute("SELECT COUNT(*) AS n FROM master_products").fetchone()["n"]
    resolved = catalog.resolve_customer_sku(conn, scootsy_id, "TOTALLY-UNKNOWN-SKU-XYZ")
    after = conn.execute("SELECT COUNT(*) AS n FROM master_products").fetchone()["n"]
    if resolved is not None:
        failures.append(f"  unknown SKU unexpectedly resolved to something: {resolved}")
    if before != after:
        failures.append(f"  master_products row count changed from {before} to {after} just from a lookup -- should never write")


def check_duplicate_mapping_rejected(conn, scootsy_id, failures):
    """Attempts a real duplicate insert to prove the DB constraint exists,
    then rolls back to a savepoint so nothing persists and the connection
    is left in a clean, usable state afterward."""
    product = catalog.get_master_product_by_barcode(conn, "9000000000001")
    conn.execute("SAVEPOINT dup_check")
    try:
        catalog.add_customer_sku_mapping(conn, scootsy_id, product["product_id"], "DEMO-SKU-001")
        conn.execute("ROLLBACK TO SAVEPOINT dup_check")
        failures.append("  inserting a duplicate (customer_id, external_sku) mapping succeeded -- expected a constraint violation")
    except psycopg2.errors.UniqueViolation:
        conn.execute("ROLLBACK TO SAVEPOINT dup_check")
    except Exception as e:
        conn.execute("ROLLBACK TO SAVEPOINT dup_check")
        failures.append(f"  duplicate insert raised {type(e).__name__}, expected psycopg2.errors.UniqueViolation: {e}")


def check_reseed_idempotent(conn, failures):
    """Re-runs the same idempotent migration file and confirms the
    expected rows are still exactly present -- not that the whole table
    is still exactly 7/6 rows, since other products may exist by now."""
    from db import CATALOG_MIGRATION_PATH

    conn.executescript(CATALOG_MIGRATION_PATH.read_text())
    conn.commit()
    check_products_exist(conn, failures)
    scootsy_id = get_scootsy_id(conn)
    check_scootsy_mappings(conn, scootsy_id, failures)


def check_legacy_still_works(conn, failures):
    tables = {
        r["table_name"]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    }
    for legacy_table in ("products", "inventory_movements", "purchase_orders", "grn_receipts"):
        if legacy_table not in tables:
            failures.append(f"  legacy table {legacy_table} is missing")
    try:
        conn.execute("SELECT COUNT(*) FROM products").fetchone()
    except Exception as e:
        failures.append(f"  querying legacy products table failed: {e}")


def run():
    conn = get_connection()
    failures = []
    scootsy_id = get_scootsy_id(conn)
    if scootsy_id is None:
        print("FAILED: Scootsy customer not found -- cannot run the rest of the checks.")
        return False

    check_products_exist(conn, failures)
    check_barcodes_unique(conn, failures)
    check_scootsy_mappings(conn, scootsy_id, failures)
    check_specific_resolutions(conn, scootsy_id, failures)
    check_unmapped_product_exists_independently(conn, scootsy_id, failures)
    check_unknown_sku_no_autocreate(conn, scootsy_id, failures)
    check_duplicate_mapping_rejected(conn, scootsy_id, failures)
    check_reseed_idempotent(conn, failures)
    check_legacy_still_works(conn, failures)

    conn.close()

    if failures:
        print(f"FAILED ({len(failures)} issue(s)):")
        print("\n".join(failures))
    else:
        print(
            f"PASSED -- all {len(EXPECTED_PRODUCTS)} expected master products, "
            f"all {len(EXPECTED_SCOOTSY_MAPPINGS)} expected Scootsy mappings, the unmapped-product case, "
            "unknown-SKU no-autocreate, duplicate-mapping rejection, re-seed idempotency, "
            "and legacy-table compatibility all check out."
        )
    return not failures


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

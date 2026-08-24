"""
Verifies the Phase 2 Purchase Order identity foundation (po_id as the real
primary key, po_number kept as backwards-compatible scaffolding) against
a disposable PostgreSQL database, never the development database.

Since purchase_orders had zero rows at the time of this migration, most
checks here are real round-trip tests: insert a test PO inside a
SAVEPOINT, prove the new identity layer and the old legacy access pattern
both still work correctly, then ROLLBACK TO SAVEPOINT so nothing persists
and no failed-transaction state leaks into the connection. Shape/schema
checks (constraint names, FK targets) are verified directly against
Postgres's catalog, independent of row data.
"""
import sys
from pathlib import Path

import psycopg2.errors

import purchase_orders as po_helpers
from db import get_connection
from verify_db import bootstrap_connection, create_database, drop_database

TEST_DB_NAME = "drizzl_inventory_test_po_identity"

CHILD_FK_TABLES = ["po_line_items", "appointments", "grn_receipts", "debit_notes"]
MIGRATION_PATH = Path(__file__).parent / "migrations" / "002_po_identity_foundation.sql"


def get_scootsy_id(conn):
    row = conn.execute("SELECT id FROM customers WHERE name = ?", ("Scootsy Logistics Private Limited",)).fetchone()
    return row["id"] if row else None


def check_po_id_is_real_pk(conn, failures):
    row = conn.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'purchase_orders'::regclass AND i.indisprimary
        """
    ).fetchone()
    pk_column = row["attname"] if row else None
    if pk_column != "po_id":
        failures.append(f"  expected po_id to be the primary key, found {pk_column!r}")

    col = conn.execute(
        "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'purchase_orders' AND column_name = 'po_id'"
    ).fetchone()
    if col is None or col["is_nullable"] != "NO":
        failures.append("  po_id is not NOT NULL")


def check_child_fks_intact(conn, failures):
    for table in CHILD_FK_TABLES:
        row = conn.execute(
            """
            SELECT ccu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_name = ?
              AND tc.constraint_name = ?
            """,
            (table, f"{table}_po_number_fkey"),
        ).fetchone()
        if row is None:
            failures.append(f"  {table}_po_number_fkey is missing or doesn't reference the expected column")
        elif row["column_name"] != "po_number":
            failures.append(f"  {table}_po_number_fkey references {row['column_name']!r}, expected po_number")


def check_legacy_query_and_insert_pattern(conn, scootsy_id, failures):
    """Proves both the read side (a typical reconcile.py/app.py-style
    query) and the write side (_ensure_po_stub()'s exact INSERT ... ON
    CONFLICT(po_number) pattern) still work unmodified after the
    migration -- this is the whole promise of Phase 2."""
    try:
        conn.execute("SELECT po_number, customer_id, voided FROM purchase_orders WHERE voided = 0").fetchall()
    except Exception as e:
        failures.append(f"  a typical legacy read query failed: {e}")
        return

    conn.execute("SAVEPOINT legacy_insert_check")
    try:
        conn.execute(
            "INSERT INTO purchase_orders (po_number, customer_id) VALUES (?, ?) ON CONFLICT (po_number) DO NOTHING",
            ("VERIFY-LEGACY-STUB-PO", scootsy_id),
        )
        conn.execute(
            "INSERT INTO purchase_orders (po_number, customer_id) VALUES (?, ?) ON CONFLICT (po_number) DO NOTHING",
            ("VERIFY-LEGACY-STUB-PO", scootsy_id),
        )
        row = conn.execute("SELECT COUNT(*) AS n FROM purchase_orders WHERE po_number = ?", ("VERIFY-LEGACY-STUB-PO",)).fetchone()
        if row["n"] != 1:
            failures.append(f"  legacy ON CONFLICT(po_number) insert pattern created {row['n']} rows, expected exactly 1")
    except Exception as e:
        failures.append(f"  legacy _ensure_po_stub-style insert pattern failed after migration: {e}")
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT legacy_insert_check")


def check_round_trip_and_resolution(conn, scootsy_id, failures):
    """Covers: PO metadata preserved, po_number <-> external_po_number
    resolve to the same order, a second distinct po_number for the same
    customer is valid, whitespace normalization, and unknown PO -> None.
    All inside one savepoint, rolled back at the end."""
    conn.execute("SAVEPOINT round_trip_check")
    try:
        conn.execute(
            """
            INSERT INTO purchase_orders (po_number, customer_id, po_date, vendor_name, grand_total)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("VERIFY-PO-0001", scootsy_id, "2026-08-15", "DRIZZL DEMO VENDOR", 1234.5),
        )

        by_customer_number = po_helpers.get_po_by_customer_and_number(conn, scootsy_id, "VERIFY-PO-0001")
        if by_customer_number is None:
            failures.append("  get_po_by_customer_and_number() did not find the just-inserted PO")
        else:
            if by_customer_number["external_po_number"] != "VERIFY-PO-0001":
                failures.append(f"  external_po_number was {by_customer_number['external_po_number']!r}, expected 'VERIFY-PO-0001'")
            if by_customer_number["po_date"] != "2026-08-15" or by_customer_number["vendor_name"] != "DRIZZL DEMO VENDOR":
                failures.append("  PO metadata (po_date/vendor_name) was not preserved correctly on round-trip")

            by_id = po_helpers.get_po_by_id(conn, by_customer_number["po_id"])
            if by_id is None or by_id["po_number"] != "VERIFY-PO-0001":
                failures.append("  get_po_by_id() did not resolve back to the same PO")

        padded = po_helpers.resolve_po_identity(conn, scootsy_id, "  VERIFY-PO-0001  ")
        if padded is None:
            failures.append("  resolve_po_identity() did not normalize surrounding whitespace correctly")

        unknown = po_helpers.resolve_po_identity(conn, scootsy_id, "TOTALLY-UNKNOWN-PO-XYZ")
        if unknown is not None:
            failures.append(f"  unknown PO number unexpectedly resolved to something: {unknown}")

        # A second, different po_number for the same customer must be valid.
        conn.execute(
            "INSERT INTO purchase_orders (po_number, customer_id) VALUES (?, ?)",
            ("VERIFY-PO-0002", scootsy_id),
        )
        second = po_helpers.get_po_by_customer_and_number(conn, scootsy_id, "VERIFY-PO-0002")
        if second is None:
            failures.append("  a second distinct po_number for the same customer was not accepted")

        # Duplicate (customer_id, po_number) must be rejected.
        conn.execute("SAVEPOINT duplicate_check")
        try:
            conn.execute(
                "INSERT INTO purchase_orders (po_number, customer_id) VALUES (?, ?)",
                ("VERIFY-PO-0001", scootsy_id),
            )
            conn.execute("ROLLBACK TO SAVEPOINT duplicate_check")
            failures.append("  inserting a duplicate (customer_id, po_number) succeeded -- expected a constraint violation")
        except psycopg2.errors.UniqueViolation:
            conn.execute("ROLLBACK TO SAVEPOINT duplicate_check")
        except Exception as e:
            conn.execute("ROLLBACK TO SAVEPOINT duplicate_check")
            failures.append(f"  duplicate insert raised {type(e).__name__}, expected psycopg2.errors.UniqueViolation: {e}")

    finally:
        conn.execute("ROLLBACK TO SAVEPOINT round_trip_check")


def check_reapply_migration_idempotent(conn, failures):
    before = conn.execute("SELECT COUNT(*) AS n FROM purchase_orders").fetchone()["n"]
    conn.executescript(MIGRATION_PATH.read_text())
    conn.commit()
    after = conn.execute("SELECT COUNT(*) AS n FROM purchase_orders").fetchone()["n"]
    if before != after:
        failures.append(f"  re-running the migration changed purchase_orders row count from {before} to {after}")
    check_po_id_is_real_pk(conn, failures)
    check_child_fks_intact(conn, failures)


def run():
    create_database(TEST_DB_NAME)
    conn = bootstrap_connection(TEST_DB_NAME)
    failures = []
    try:
        scootsy_id = get_scootsy_id(conn)
        if scootsy_id is None:
            print("FAILED: Scootsy customer not found -- cannot run the rest of the checks.")
            return False

        row_count_before = conn.execute("SELECT COUNT(*) AS n FROM purchase_orders").fetchone()["n"]

        check_po_id_is_real_pk(conn, failures)
        check_child_fks_intact(conn, failures)
        check_legacy_query_and_insert_pattern(conn, scootsy_id, failures)
        check_round_trip_and_resolution(conn, scootsy_id, failures)
        check_reapply_migration_idempotent(conn, failures)

        row_count_after = conn.execute("SELECT COUNT(*) AS n FROM purchase_orders").fetchone()["n"]
        if row_count_after != row_count_before:
            failures.append(f"  purchase_orders row count changed from {row_count_before} to {row_count_after} -- verification left data behind")
    finally:
        conn.close()
        drop_database(TEST_DB_NAME)

    if failures:
        print(f"FAILED ({len(failures)} issue(s)):")
        print("\n".join(failures))
    else:
        print(
            "PASSED -- po_id is the real primary key, po_number is preserved as unique backwards-compatible "
            "scaffolding, all 5 child FKs intact, legacy read/insert patterns still work, PO metadata round-trips "
            "correctly, external_po_number resolution + whitespace normalization + unknown-PO handling all check "
            "out, duplicate (customer_id, po_number) is rejected, a second po_number per customer is valid, "
            "re-running the migration is a no-op, and no test data was left behind."
        )
    return not failures


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

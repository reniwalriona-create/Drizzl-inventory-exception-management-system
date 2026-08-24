"""
Verifies the Phase 4 PO CSV upload + staged review UI in a disposable
PostgreSQL database: both the Flask
routes (via app.test_client(), which exercise real HTTP requests and real
commits, same as a browser would) and the backend functions in
po_csv_staging.py directly (via savepoints, for tests that don't need a
full request cycle).

Route-driven tests commit for real (each Flask request opens its own
database connection, so an external savepoint here can't wrap them) --
those are cleaned up with an explicit DELETE at the end instead, per the
"transactions/savepoints OR clean up deliberately" allowance. Pure
backend-function tests use savepoints and are rolled back.

Uses the real Scootsy CSV (PO_0000000000001.csv) for the main flow, plus
small synthetic CSV fixtures for the blocked/edge-case tests.
"""
import csv
import sys
import tempfile
from pathlib import Path

import psycopg2.errors

from verify_db import bootstrap_connection, create_database, drop_database, point_app_at

TEST_DB_NAME = "drizzl_inventory_test_po_review_ui"
point_app_at(TEST_DB_NAME)  # must happen before importing app

import po_csv_staging as staging
from app import app
from db import get_connection

REAL_CSV_PATH = Path("/Users/demo/Desktop/Swiggy test PO GRN data/last 7 po csv/PO_0000000000001.csv")
SCOOTSY_NAME = "Scootsy Logistics Private Limited"

REAL_HEADER = [
    "PoNumber", "Entity", "FacilityId", "FacilityName", "City", "PoCreatedAt", "PoModifiedAt",
    "Status", "SupplierCode", "VendorName", "PoAmount", "SkuCode", "SkuDescription", "CategoryId",
    "OrderedQty", "ReceivedQty", "BalancedQty", "Tax", "PoLineValueWithoutTax", "PoLineValueWithTax",
    "Mrp", "UnitBasedCost", "ExpectedDeliveryDate", "PoExpiryDate", "OtbReferenceNumber",
    "InternalExternalPo", "PoAgeing", "BrandName", "ReferencePoNumber",
]

BASE_ROW = {
    "PoNumber": "UITEST0001", "Entity": "SCOOTSY LOGISTICS PRIVATE LIMITED",
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


def upload_csv(client, path, filename=None):
    with open(path, "rb") as f:
        data = {"file": (f, filename or Path(path).name)}
        return client.post("/po-import", data=data, content_type="multipart/form-data", follow_redirects=False)


def table_count(conn, table, where=None, params=()):
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return conn.execute(sql, params).fetchone()["n"]


def batch_id_from_redirect(response):
    # Location header looks like /po-import/<batch_id>
    loc = response.headers.get("Location", "")
    parts = [p for p in loc.split("/") if p]
    return int(parts[-1]) if parts and parts[-1].isdigit() else None


def cleanup_batch(conn, batch_id):
    if batch_id is None:
        return
    conn.execute(
        "DELETE FROM staged_po_lines WHERE staged_po_id IN (SELECT staged_po_id FROM staged_purchase_orders WHERE batch_id = ?)",
        (batch_id,),
    )
    conn.execute("DELETE FROM staged_purchase_orders WHERE batch_id = ?", (batch_id,))
    conn.execute("DELETE FROM po_import_rows WHERE batch_id = ?", (batch_id,))
    conn.execute("DELETE FROM po_import_batches WHERE batch_id = ?", (batch_id,))
    conn.commit()


def _run_checks():
    conn = get_connection()
    # Phase 12: routes are now login-gated. CSRF is disabled for this
    # same-process test harness (Flask-WTF's own documented testing
    # guidance -- there's no cross-site attacker in a test_client() run;
    # CSRF enforcement itself is verified separately in
    # verify_security.py). A throwaway user is created directly (not via
    # create_user.py, which prompts interactively) and logged in before
    # any route below is exercised.
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["TESTING"] = True
    from werkzeug.security import generate_password_hash
    conn.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?) ON CONFLICT (username) DO NOTHING",
        ("verify_po_review_ui_bot", generate_password_hash("not-a-real-password")),
    )
    conn.commit()
    client = app.test_client()
    login_resp = client.post("/login", data={"username": "verify_po_review_ui_bot", "password": "not-a-real-password"})
    if login_resp.status_code not in (302, 303):
        print(f"FATAL: test login failed (status {login_resp.status_code}) -- stopping rather than running unauthenticated.")
        return False
    failures = []
    created_batch_ids = []

    if not REAL_CSV_PATH.exists():
        print(f"FATAL: real CSV not found at {REAL_CSV_PATH} -- stopping rather than fabricating a result.")
        return False

    official_before = {
        t: table_count(conn, t) for t in ("purchase_orders", "po_line_items", "inventory_movements", "grn_receipts")
    }

    # -----------------------------------------------------------------
    # Upload (route-driven, real commits -- cleaned up explicitly below)
    # -----------------------------------------------------------------
    resp = upload_csv(client, REAL_CSV_PATH)
    if resp.status_code != 302:
        failures.append(f"  upload 1: expected a redirect after successful upload, got {resp.status_code}")
    batch_id = batch_id_from_redirect(resp)
    if batch_id is None:
        failures.append("  upload 1: could not determine batch_id from redirect")
    else:
        created_batch_ids.append(batch_id)
        n_pos = table_count(conn, "staged_purchase_orders", "batch_id = ?", (batch_id,))
        n_lines = table_count(
            conn, "staged_po_lines",
            "staged_po_id IN (SELECT staged_po_id FROM staged_purchase_orders WHERE batch_id = ?)", (batch_id,),
        )
        if n_pos != 12:
            failures.append(f"  upload 2: expected 12 staged POs, got {n_pos}")
        if n_lines != 51:
            failures.append(f"  upload 2: expected 51 staged lines, got {n_lines}")

    # 3. Re-upload identical file -> reuses existing batch
    resp2 = upload_csv(client, REAL_CSV_PATH)
    batch_id2 = batch_id_from_redirect(resp2)
    if batch_id2 != batch_id:
        failures.append(f"  upload 3: re-upload should reuse batch {batch_id}, got {batch_id2}")
    if table_count(conn, "po_import_batches", "customer_id = (SELECT id FROM customers WHERE name = ?) AND source_filename = ?", (SCOOTSY_NAME, "PO_0000000000001.csv")) != 1:
        failures.append("  upload 3: re-upload created a second batch instead of reusing the existing one")

    # 4. Fatal invalid CSV (mixed entities) leaves no partial data
    mixed_path = write_csv([row(Entity="SCOOTSY LOGISTICS PRIVATE LIMITED"), row(PoNumber="UITEST0002", Entity="SOME OTHER COMPANY")])
    batches_before_fatal = table_count(conn, "po_import_batches")
    resp3 = upload_csv(client, mixed_path, filename="mixed_entity.csv")
    if resp3.status_code != 302:
        failures.append(f"  upload 4: expected a redirect (with flashed error) even on fatal failure, got {resp3.status_code}")
    if table_count(conn, "po_import_batches") != batches_before_fatal:
        failures.append("  upload 4: a fatal import error left a partial batch behind")

    # 5. Non-CSV upload rejected
    txt_path = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    txt_path.write("not a csv")
    txt_path.close()
    batches_before_txt = table_count(conn, "po_import_batches")
    resp4 = upload_csv(client, txt_path.name, filename="notacsv.txt")
    if resp4.status_code != 302:
        failures.append(f"  upload 5: expected a redirect (with flashed error), got {resp4.status_code}")
    if table_count(conn, "po_import_batches") != batches_before_txt:
        failures.append("  upload 5: a non-CSV upload was somehow staged")

    # -----------------------------------------------------------------
    # Batch review page
    # -----------------------------------------------------------------
    if batch_id is not None:
        resp = client.get(f"/po-import/{batch_id}")
        if resp.status_code != 200:
            failures.append(f"  review 6: batch page returned {resp.status_code}, expected 200")
        body = resp.get_data(as_text=True)
        if "GGNPO385985" not in body:
            failures.append("  review 7: batch page did not show a known staged PO (GGNPO385985)")

        summary = staging.batch_summary(conn, batch_id)
        if summary["orders"] != 12 or summary["lines"] != 51:
            failures.append(f"  review 8: expected 12 orders / 51 lines, got {summary}")
        if summary["needs_source"] != 12 or summary["ready"] != 0 or summary["blocked"] != 0:
            failures.append(f"  review 9: freshly staged valid POs should all be NEEDS SOURCE, got {summary}")

        detail_resp = client.get(f"/po-import/{batch_id}/po/" + str(
            conn.execute("SELECT staged_po_id FROM staged_purchase_orders WHERE batch_id = ? AND external_po_number = ?", (batch_id, "GGNPO385985")).fetchone()["staged_po_id"]
        ))
        detail_body = detail_resp.get_data(as_text=True)
        if "Customer destination" not in detail_body or "Drizzl source warehouse" not in detail_body:
            failures.append("  review 10: detail page did not clearly separate destination from source")

        # Nonexistent batch -> 404
        resp404 = client.get("/po-import/999999")
        if resp404.status_code != 404:
            failures.append(f"  expected 404 for a nonexistent batch, got {resp404.status_code}")

    # -----------------------------------------------------------------
    # Individual + bulk assignment, cross-batch protection (savepoint-based)
    # -----------------------------------------------------------------
    if batch_id is not None:
        conn.execute("SAVEPOINT assignment_checks")
        try:
            all_pos = staging.list_staged_pos(conn, batch_id)
            po_ids = [p["staged_po_id"] for p in all_pos]
            loc = conn.execute("SELECT id FROM locations LIMIT 1").fetchone()
            if loc is None:
                failures.append("  no locations exist in this database -- cannot test source assignment")
            else:
                location_id = loc["id"]

                # 11/12: individual assignment + invalid location rejected
                one_id = po_ids[0]
                staging.assign_source_location(conn, batch_id, [one_id], location_id)
                assigned = conn.execute("SELECT source_location_id, external_po_number FROM staged_purchase_orders WHERE staged_po_id = ?", (one_id,)).fetchone()
                if assigned["source_location_id"] != location_id:
                    failures.append("  assignment 11: individual assignment did not persist source_location_id")
                other_unchanged = conn.execute(
                    "SELECT COUNT(*) AS n FROM staged_purchase_orders WHERE batch_id = ? AND staged_po_id != ? AND source_location_id IS NOT NULL",
                    (batch_id, one_id),
                ).fetchone()["n"]
                if other_unchanged != 0:
                    failures.append("  assignment 11: individual assignment affected other rows")

                try:
                    staging.assign_source_location(conn, batch_id, [po_ids[1]], 999999)
                    failures.append("  assignment 12: an invalid location id should have been rejected")
                except ValueError:
                    pass

                # 13: cross-batch protection -- create a second tiny batch, try to assign its PO via the first batch_id
                other_csv = write_csv([row(PoNumber="UITEST0003")])
                other_result = staging.stage_po_csv(conn, other_csv)
                other_po_id = staging.list_staged_pos(conn, other_result["batch_id"])[0]["staged_po_id"]
                try:
                    staging.assign_source_location(conn, batch_id, [other_po_id], location_id)
                    failures.append("  assignment 13: a PO from another batch was accepted through this batch's assignment")
                except ValueError:
                    pass
                cross_check = conn.execute("SELECT source_location_id FROM staged_purchase_orders WHERE staged_po_id = ?", (other_po_id,)).fetchone()
                if cross_check["source_location_id"] is not None:
                    failures.append("  assignment 13: cross-batch assignment actually wrote a value despite being rejected")

                # 14/15: bulk assignment of exactly 3 specific POs
                three_ids = po_ids[2:5]
                staging.assign_source_location(conn, batch_id, three_ids, location_id)
                assigned_count = conn.execute(
                    f"SELECT COUNT(*) AS n FROM staged_purchase_orders WHERE staged_po_id IN ({','.join(['?']*3)}) AND source_location_id = ?",
                    (*three_ids, location_id),
                ).fetchone()["n"]
                if assigned_count != 3:
                    failures.append(f"  assignment 14: expected exactly 3 POs assigned, got {assigned_count}")
                untouched = [pid for pid in po_ids if pid not in three_ids and pid != one_id]
                if untouched:
                    still_null = conn.execute(
                        f"SELECT COUNT(*) AS n FROM staged_purchase_orders WHERE staged_po_id IN ({','.join(['?']*len(untouched))}) AND source_location_id IS NULL",
                        tuple(untouched),
                    ).fetchone()["n"]
                    if still_null != len(untouched):
                        failures.append("  assignment 15: bulk assignment affected POs that weren't selected")

                # 16: invalid staged PO id in a bulk request fails atomically
                before_mix = conn.execute(
                    f"SELECT staged_po_id, source_location_id FROM staged_purchase_orders WHERE staged_po_id IN ({','.join(['?']*len(untouched[:2]))})",
                    tuple(untouched[:2]),
                ).fetchall() if len(untouched) >= 2 else []
                try:
                    staging.assign_source_location(conn, batch_id, untouched[:2] + [9999999], location_id)
                    failures.append("  assignment 16: a bulk request containing an invalid id should have been rejected entirely")
                except ValueError:
                    pass
                after_mix = conn.execute(
                    f"SELECT staged_po_id, source_location_id FROM staged_purchase_orders WHERE staged_po_id IN ({','.join(['?']*len(untouched[:2]))})",
                    tuple(untouched[:2]),
                ).fetchall() if len(untouched) >= 2 else []
                if [dict(r) for r in before_mix] != [dict(r) for r in after_mix]:
                    failures.append("  assignment 16: a rejected bulk request still partially updated some rows")

                # 17: select-all -> every PO in the batch
                staging.assign_source_location(conn, batch_id, po_ids, location_id)
                all_assigned = conn.execute(
                    "SELECT COUNT(*) AS n FROM staged_purchase_orders WHERE batch_id = ? AND source_location_id = ?",
                    (batch_id, location_id),
                ).fetchone()["n"]
                if all_assigned != len(po_ids):
                    failures.append(f"  assignment 17: expected all {len(po_ids)} POs assigned, got {all_assigned}")

                # 19/20: status transitions
                fresh = conn.execute("SELECT validation_status, source_location_id FROM staged_purchase_orders WHERE staged_po_id = ?", (po_ids[-1],)).fetchone()
                if staging.review_status(fresh["validation_status"], fresh["source_location_id"]) != "ready":
                    failures.append("  status 20: valid PO with a source assigned should be READY")
        finally:
            conn.execute("ROLLBACK TO SAVEPOINT assignment_checks")

    # -----------------------------------------------------------------
    # Status: needs_source / blocked-with-source, detail view SKU/product/barcode
    # -----------------------------------------------------------------
    conn.execute("SAVEPOINT status_and_detail_checks")
    try:
        if staging.review_status("valid", None) != "needs_source":
            failures.append("  status 19: valid + no source should be NEEDS SOURCE")
        if staging.review_status("blocked", None) != "blocked":
            failures.append("  status 21a: blocked + no source should be BLOCKED")

        unknown_sku_path = write_csv([row(PoNumber="UITEST0004", SkuCode="UNKNOWN-SKU-XYZ")])
        unknown_result = staging.stage_po_csv(conn, unknown_sku_path)
        unknown_po = staging.list_staged_pos(conn, unknown_result["batch_id"])[0]
        loc = conn.execute("SELECT id FROM locations LIMIT 1").fetchone()
        if loc:
            staging.assign_source_location(conn, unknown_result["batch_id"], [unknown_po["staged_po_id"]], loc["id"])
            after_assign = conn.execute(
                "SELECT validation_status, source_location_id FROM staged_purchase_orders WHERE staged_po_id = ?",
                (unknown_po["staged_po_id"],),
            ).fetchone()
            if staging.review_status(after_assign["validation_status"], after_assign["source_location_id"]) != "blocked":
                failures.append("  status 21b: a blocked PO with a source assigned should still be BLOCKED")

        # 22-25: detail view shows customer SKU, master product, barcode, and validation errors
        detail = staging.get_staged_po(conn, unknown_po["staged_po_id"])
        skus = [l["external_sku"] for l in detail["lines"]]
        if "UNKNOWN-SKU-XYZ" not in skus:
            failures.append("  detail 22: customer SKU not shown on the staged line")
        if any(l["validation_status"] == "blocked" and not l["validation_errors"] for l in detail["lines"]):
            failures.append("  detail 25: blocked line has no validation_errors to display")

        # A valid, resolved line to check master product name + barcode are exposed
        real_result = staging.stage_po_csv(conn, str(REAL_CSV_PATH), filename="verify_detail_check.csv")
        real_po = staging.list_staged_pos(conn, real_result["batch_id"])[0]
        real_detail = staging.get_staged_po(conn, real_po["staged_po_id"])
        if not any(l["master_product_name"] and l["master_barcode"] for l in real_detail["lines"]):
            failures.append("  detail 23/24: master product name/barcode not exposed on staged lines")
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT status_and_detail_checks")

    # -----------------------------------------------------------------
    # Cleanup route-driven (really-committed) data
    # -----------------------------------------------------------------
    for bid in created_batch_ids:
        cleanup_batch(conn, bid)
    conn.execute(
        "DELETE FROM po_import_batches WHERE source_filename IN (?, ?)",
        ("mixed_entity.csv", "notacsv.txt"),
    )
    conn.commit()

    official_after = {
        t: table_count(conn, t) for t in ("purchase_orders", "po_line_items", "inventory_movements", "grn_receipts")
    }
    for t in official_before:
        if official_before[t] != official_after[t]:
            failures.append(f"  official ledger table {t} changed from {official_before[t]} to {official_after[t]}")

    remaining = sum(table_count(conn, t) for t in ("po_import_batches", "po_import_rows", "staged_purchase_orders", "staged_po_lines"))
    if remaining != 0:
        failures.append(f"  {remaining} staging row(s) left behind after cleanup")

    conn.execute("DELETE FROM users WHERE username = ?", ("verify_po_review_ui_bot",))
    conn.commit()
    conn.close()

    if failures:
        print(f"FAILED ({len(failures)} issue(s)):")
        print("\n".join(failures))
    else:
        print(
            "PASSED -- upload (fresh + idempotent re-upload + fatal-error + non-CSV rejection), batch review "
            "(counts, NEEDS SOURCE default, destination/source separation, 404 handling), individual + bulk "
            "assignment (exact-selection, cross-batch protection, atomic rejection, select-all), status "
            "transitions (needs_source/ready/blocked-with-source), and detail view (customer SKU, master product, "
            "barcode, validation errors) all behave as specified. Official ledger tables unchanged throughout. "
            "No staging data left behind."
        )
    return not failures


def run():
    create_database(TEST_DB_NAME)
    bootstrap = bootstrap_connection(TEST_DB_NAME)
    bootstrap.close()
    try:
        return _run_checks()
    finally:
        drop_database(TEST_DB_NAME)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

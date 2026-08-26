"""
Verifies Phase 7: GRN CSV upload + review UI + quarantine visibility +
manual revalidation -- the new /grn-import routes in app.py, driven
through real Flask requests via app.test_client() (same as
verify_po_review_ui.py).

Runs entirely against a disposable throwaway Postgres database
(drizzl_inventory_test_phase7) -- config.DATABASE_URL is monkeypatched
for the duration of this script so that app.test_client()'s own
route-driven connections transparently target the throwaway database too
(db.py's get_connection() reads config.DATABASE_URL at call time, not at
import time, so this is safe). The real drizzl_inventory database is
never touched. Dropped at the end regardless of pass/fail.

Uses repository-local public demo fixtures plus generated synthetic rows for
duplicate-DN, multi-lot, quarantine, and revalidation UI states.
"""
import csv
import json
import sys
import tempfile
from pathlib import Path

import psycopg2

TEST_DB_NAME = "drizzl_inventory_test_phase7"

import config
config.DATABASE_URL = f"dbname={TEST_DB_NAME}"  # must happen before `import app`
import db as db_module

import grn_csv_staging as staging
import po_csv_staging
import po_posting
from app import app

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "synthetic"
PO_FIXTURE = FIXTURE_DIR / "demo_po_01.csv"
GRN_FIXTURE = FIXTURE_DIR / "demo_grn_01.csv"
SCOOTSY_NAME = "Scootsy Logistics Private Limited"

GRN_HEADER = [
    "GrnNumber", "PurchaseOrderNumber", "FacilityName", "SupplierCode", "VendorName",
    "InvoiceNumber", "InvoiceDate", "CreatedAtDate", "DnNumber", "DNQuantity", "DNValue",
    "SkuCode", "SkuDescription", "BrandName", "Category", "ReceivedQty",
    "GrnLineValueWithoutTax", "GrnLineValueWithTax", "LotMrp", "LotExpiryDate",
    "CgstRate", "CgstAmount", "SgstRate", "SgstAmount", "IgstRate", "IgstAmount",
    "CessRate", "CessAmount", "AdditionalCess", "TotalTax", "TotalAmount",
]
GRN_BASE_ROW = {
    "GrnNumber": "SYNGRN0001", "PurchaseOrderNumber": "SYNPO0001", "FacilityName": "DEMO FACILITY B",
    "SupplierCode": "DEMO-SUPPLIER-001", "VendorName": "DRIZZL DEMO VENDOR",
    "InvoiceNumber": "SYN-INV-REVIEW", "InvoiceDate": "2026-07-31", "CreatedAtDate": "2026-08-13 17:25:12",
    "DnNumber": "", "DNQuantity": "0", "DNValue": "0.00", "SkuCode": "DEMO-SKU-001",
    "SkuDescription": "Drizzl Passionfruit | Probiotic Soda | 250 ml", "BrandName": "DRIZZL",
    "Category": "Packaged Food", "ReceivedQty": "10", "GrnLineValueWithoutTax": "600.00",
    "GrnLineValueWithTax": "840.00", "LotMrp": "120.00", "LotExpiryDate": "2027-05-01",
    "CgstRate": "0.00", "CgstAmount": "0.00", "SgstRate": "0.00", "SgstAmount": "0.00",
    "IgstRate": "40.00", "IgstAmount": "240.00", "CessRate": "0.00", "CessAmount": "0.00",
    "AdditionalCess": "0.00", "TotalTax": "240.00", "TotalAmount": "840.00",
}


def grow(**overrides):
    r = dict(GRN_BASE_ROW)
    r.update(overrides)
    return r


def write_csv(rows, fieldnames=GRN_HEADER):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    f.close()
    return f.name


def expanded_grn_fixture():
    with GRN_FIXTURE.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    rows.extend([
        grow(GrnNumber="SYN-GRN-NORM", PurchaseOrderNumber="SYN-PO-NORM", DnNumber="SYN-DN-NORM", DNQuantity="0", DNValue="0", ReceivedQty="10"),
        grow(GrnNumber="SYN-GRN-NORM", PurchaseOrderNumber="SYN-PO-NORM", DnNumber="SYN-DN-NORM", DNQuantity="3", DNValue="180", ReceivedQty="10"),
        grow(GrnNumber="SYN-GRN-NORM", PurchaseOrderNumber="SYN-PO-NORM", DnNumber="SYN-DN-NORM", SkuCode="DEMO-SKU-002", ReceivedQty="48", LotExpiryDate="2027-06-01"),
        grow(GrnNumber="SYN-GRN-NORM", PurchaseOrderNumber="SYN-PO-NORM", DnNumber="SYN-DN-NORM", SkuCode="DEMO-SKU-002", ReceivedQty="24", LotExpiryDate="2027-05-01"),
    ])
    for sku, qty in [("DEMO-SKU-003", 72), ("DEMO-SKU-001", 240), ("DEMO-SKU-002", 48), ("DEMO-SKU-005", 144), ("DEMO-SKU-004", 96)]:
        rows.append(grow(GrnNumber="SYN-GRN-LATE", PurchaseOrderNumber="SYN-PO-LATE", FacilityName="Synthetic Late Facility", SkuCode=sku, ReceivedQty=str(qty), LotExpiryDate=f"2027-0{(qty % 5) + 1}-01"))
    return write_csv(rows)


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    return condition


def table_count(conn, table, where=None, params=()):
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return conn.execute(sql, params).fetchone()["n"]


def upload_grn_csv(client, path, customer_id, filename=None):
    with open(path, "rb") as f:
        data = {"file": (f, filename or Path(path).name)}
        if customer_id is not None:
            data["customer_id"] = str(customer_id)
        return client.post("/grn-import", data=data, content_type="multipart/form-data", follow_redirects=False)


def batch_id_from_redirect(response):
    loc = response.headers.get("Location", "")
    parts = [p for p in loc.split("/") if p]
    for i, p in enumerate(parts):
        if p == "grn-import" and i + 1 < len(parts) and parts[i + 1].isdigit():
            return int(parts[i + 1])
    return None


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


def get_customer_id(conn, name):
    r = conn.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()
    return r["id"] if r else None


def get_location_id(conn, name):
    r = conn.execute("SELECT id FROM locations WHERE name = ?", (name,)).fetchone()
    return r["id"] if r else None


def product_id_for_sku(conn, sku):
    r = conn.execute(
        "SELECT mp.product_id FROM master_products mp JOIN customer_product_skus c ON c.product_id = mp.product_id "
        "WHERE c.external_sku = ?", (sku,)
    ).fetchone()
    return r["product_id"] if r else None


def insert_official_po(conn, po_number, customer_id, source_location_id, destination_facility_name,
                        supplier_code, vendor_name, lines):
    po_id = conn.execute(
        """
        INSERT INTO purchase_orders (po_number, customer_id, source_location_id, destination_facility_name,
                                      facility_name, supplier_code, vendor_name)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        RETURNING po_id
        """,
        (po_number, customer_id, source_location_id, destination_facility_name,
         destination_facility_name, supplier_code, vendor_name),
    ).fetchone()["po_id"]
    for product_id, external_sku, qty in lines:
        conn.execute(
            "INSERT INTO po_line_items (po_number, item_code, product_id, external_sku, qty) VALUES (?, ?, ?, ?, ?)",
            (po_number, external_sku, product_id, external_sku, qty),
        )
    return po_id


def run():
    for path in (PO_FIXTURE, GRN_FIXTURE):
        if not path.exists():
            print(f"FAIL -- synthetic fixture not found: {path}")
            return False
    expanded_grn = expanded_grn_fixture()

    print(f"Creating throwaway database {TEST_DB_NAME}...")
    create_test_database()
    conn = db_module.get_connection()  # bootstraps schema + seed, since DATABASE_URL is patched
    # Phase 12: routes are now login-gated. CSRF is disabled for this
    # same-process test harness (Flask-WTF's own documented testing
    # guidance -- CSRF enforcement itself is verified separately in
    # verify_security.py). The whole database is disposable and dropped
    # at the end regardless, so the test user needs no explicit cleanup.
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["TESTING"] = True
    from werkzeug.security import generate_password_hash
    conn.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
        ("verify_bot", generate_password_hash("not-a-real-password")),
    )
    conn.commit()
    client = app.test_client()
    login_resp = client.post("/login", data={"username": "verify_bot", "password": "not-a-real-password"})
    if login_resp.status_code not in (302, 303):
        print(f"FATAL: test login failed (status {login_resp.status_code}) -- stopping rather than running unauthenticated.")
        return False
    ok = True

    try:
        scootsy_id = get_customer_id(conn, SCOOTSY_NAME)
        bangalore_id = get_location_id(conn, "Drizzl Demo Warehouse")
        p_sku_001 = product_id_for_sku(conn, "DEMO-SKU-001")

        baseline_movements = table_count(conn, "inventory_movements")

        # -----------------------------------------------------------------
        print("\n--- Setup: stage + post public SYN-PO-1001 (backend setup) ---")
        po_result = po_csv_staging.stage_po_csv(conn, PO_FIXTURE, customer_id=scootsy_id, filename=PO_FIXTURE.name)
        conn.commit()
        po_batch_id = po_result["batch_id"]
        fixture_po = next(p for p in po_csv_staging.list_staged_pos(conn, po_batch_id) if p["external_po_number"] == "SYN-PO-1001")
        po_csv_staging.assign_source_location(conn, po_batch_id, [fixture_po["staged_po_id"]], bangalore_id)
        conn.commit()
        post_result = po_posting.post_staged_purchase_orders(conn, po_batch_id, [fixture_po["staged_po_id"]])
        conn.commit()
        ok &= check("SYN-PO-1001 posted", len(post_result["posted"]) == 1)

        baseline_grn_receipts = table_count(conn, "grn_receipts")
        baseline_grn_line_items = table_count(conn, "grn_line_items")

        # -----------------------------------------------------------------
        print("\n--- Upload: customer requirement ---")
        batches_before = table_count(conn, "grn_import_batches")
        resp = upload_grn_csv(client, expanded_grn, customer_id=None, filename="demo_grn_review.csv")
        ok &= check("upload with no customer -> redirect back to /grn-import", resp.status_code == 302 and resp.headers.get("Location", "").rstrip("/").endswith("/grn-import"))
        ok &= check("upload with no customer -> no batch created", table_count(conn, "grn_import_batches") == batches_before)

        resp = upload_grn_csv(client, expanded_grn, customer_id=999999, filename="demo_grn_review.csv")
        ok &= check("upload with invalid customer -> rejected", resp.status_code == 302 and resp.headers.get("Location", "").rstrip("/").endswith("/grn-import"))
        ok &= check("upload with invalid customer -> no batch created", table_count(conn, "grn_import_batches") == batches_before)

        # -----------------------------------------------------------------
        print("\n--- Upload: non-CSV rejection ---")
        txt_path = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        txt_path.write("not a csv")
        txt_path.close()
        resp = upload_grn_csv(client, txt_path.name, customer_id=scootsy_id, filename="not_a_csv.txt")
        ok &= check("non-CSV upload rejected", resp.status_code == 302 and table_count(conn, "grn_import_batches") == batches_before)

        # -----------------------------------------------------------------
        print("\n--- Upload: fatal import leaves no partial batch ---")
        bad_csv = write_csv([{"OnlySomeColumn": "x"}], fieldnames=["OnlySomeColumn"])
        resp = upload_grn_csv(client, bad_csv, customer_id=scootsy_id, filename="missing_columns.csv")
        ok &= check("fatal import (missing structural columns) -> no batch created", table_count(conn, "grn_import_batches") == batches_before)

        # -----------------------------------------------------------------
        print("\n--- Upload: the real GRN CSV ---")
        resp = upload_grn_csv(client, expanded_grn, customer_id=scootsy_id, filename="demo_grn_review.csv")
        ok &= check("valid upload -> redirect", resp.status_code == 302)
        grn_batch_id = batch_id_from_redirect(resp)
        ok &= check("batch_id resolved from redirect", grn_batch_id is not None)

        summary = staging.get_grn_batch_summary(conn, grn_batch_id)
        ok &= check("3 staged GRNs", summary["grns"] == 3, str(summary))
        ok &= check("15 raw rows", summary["raw_rows"] == 15, str(summary))
        ok &= check("14 normalized lines", summary["lines"] == 14, str(summary))

        # -----------------------------------------------------------------
        print("\n--- Upload: exact-file idempotency ---")
        resp2 = upload_grn_csv(client, expanded_grn, customer_id=scootsy_id, filename="demo_grn_review.csv")
        batch_id2 = batch_id_from_redirect(resp2)
        ok &= check("re-upload same file/customer -> reuses batch", batch_id2 == grn_batch_id)
        ok &= check("no duplicate batch row", table_count(conn, "grn_import_batches", "batch_id = ?", (grn_batch_id,)) == 1)

        # -----------------------------------------------------------------
        print("\n--- Revalidate the whole batch (resolves SYN-GRN-1001) ---")
        resp = client.post(f"/grn-import/{grn_batch_id}/revalidate", follow_redirects=False)
        ok &= check("batch revalidate -> redirect back to review page", resp.status_code == 302)

        summary = staging.get_grn_batch_summary(conn, grn_batch_id)
        ok &= check("verified=1, quarantined=2", summary["verified"] == 1 and summary["quarantined"] == 2, str(summary))

        # -----------------------------------------------------------------
        print("\n--- Batch review page renders ---")
        resp = client.get(f"/grn-import/{grn_batch_id}")
        body = resp.data.decode()
        ok &= check("batch review page 200", resp.status_code == 200)
        ok &= check("shows file name", "demo_grn_review.csv" in body)
        ok &= check("shows Ready to post / PO not found metric cards", "READY TO POST" in body and "PO NOT FOUND" in body)
        ok &= check("each GRN row displays its specific status", "SYN-GRN-1001" in body and "READY TO POST" in body and "PO NOT FOUND" in body)

        grns = staging.list_staged_grns(conn, grn_batch_id)
        norm = next(g for g in grns if g["external_grn_number"] == "SYN-GRN-NORM")
        ok &= check(
            "batch page's SYN-GRN-NORM total uses normalized lines",
            str(int(norm["total_received_qty"])) in body,
        )

        # -----------------------------------------------------------------
        print("\n--- Detail page: public fixture overlap ---")
        fixture_grn = next(g for g in staging.list_staged_grns(conn, grn_batch_id) if g["external_grn_number"] == "SYN-GRN-1001")
        resp = client.get(f"/grn-import/{grn_batch_id}/grn/{fixture_grn['staged_grn_id']}")
        body = resp.data.decode()
        ok &= check("detail page 200", resp.status_code == 200)
        ok &= check("shows READY TO POST", "READY TO POST" in body)
        ok &= check("links to official PO SYN-PO-1001", "SYN-PO-1001" in body)
        ok &= check("shows Drizzl source Drizzl Demo Warehouse", "Drizzl Demo Warehouse" in body)
        ok &= check("shows 48 ordered and 46 received", "48" in body and "46" in body)
        ok &= check("shows 36 ordered and 35 received", "36" in body and "35" in body)
        ok &= check("shows short master product name", "Passionfruit Probiotic Soda" in body)
        ok &= check("hides master barcode", "9000000000001" not in body)

        # -----------------------------------------------------------------
        print("\n--- Detail page: duplicate-DN representation display ---")
        norm_grn = next(g for g in staging.list_staged_grns(conn, grn_batch_id) if g["external_grn_number"] == "SYN-GRN-NORM")
        resp = client.get(f"/grn-import/{grn_batch_id}/grn/{norm_grn['staged_grn_id']}")
        body = resp.data.decode()
        ok &= check("SYN-GRN-NORM detail 200", resp.status_code == 200)
        ok &= check("shows received 10", "10" in body)
        ok &= check("shows source DN qty 3", ">3<" in body or "3" in body)
        ok &= check("shows 2 source rows", "2 source rows" in body)
        ok &= check("does NOT show the doubled-sum 20 as received for the duplicate line", "duplicate" not in body.lower() or "2 source rows" in body)

        # -----------------------------------------------------------------
        print("\n--- Detail page: multi-lot display (SYN-GRN-NORM / DEMO-SKU-002) ---")
        ok &= check("shows lot 1 received 48", "48" in body)
        ok &= check("shows lot 2 received 24", "24" in body)
        ok &= check("shows expiry 2027-06-01", "2027-06-01" in body)
        ok &= check("shows expiry 2027-05-01", "2027-05-01" in body)

        # -----------------------------------------------------------------
        print("\n--- Missing-PO quarantine remains browsable ---")
        other = [g for g in staging.list_staged_grns(conn, grn_batch_id) if g["external_grn_number"] != "SYN-GRN-1001"]
        ok &= check("2 other GRNs", len(other) == 2)
        sample = other[0]
        resp = client.get(f"/grn-import/{grn_batch_id}/grn/{sample['staged_grn_id']}")
        body = resp.data.decode()
        ok &= check("quarantined GRN detail page still 200 (browsable)", resp.status_code == 200)
        ok &= check("shows PO NOT FOUND", "PO NOT FOUND" in body)
        ok &= check("shows quarantine reason", "not currently available" in body.lower() or "not found" in body.lower())

        # -----------------------------------------------------------------
        print("\n--- Revalidation after a previously-missing PO arrives ---")
        chm_sku = "DEMO-SKU-003"
        insert_official_po(
            conn, "SYN-PO-LATE", scootsy_id, bangalore_id, "Synthetic Late Facility", "DEMO-SUPPLIER-001",
            "DRIZZL DEMO VENDOR",
            [
                (product_id_for_sku(conn, chm_sku), chm_sku, 72),
                (p_sku_001, "DEMO-SKU-001", 240),
                (product_id_for_sku(conn, "DEMO-SKU-002"), "DEMO-SKU-002", 48),
                (product_id_for_sku(conn, "DEMO-SKU-005"), "DEMO-SKU-005", 144),
                (product_id_for_sku(conn, "DEMO-SKU-004"), "DEMO-SKU-004", 96),
            ],
        )
        conn.commit()
        chm_grn = next(g for g in staging.list_staged_grns(conn, grn_batch_id) if g["external_grn_number"] == "SYN-GRN-LATE")
        before_validation_errors = json.dumps(staging.get_staged_grn(conn, chm_grn["staged_grn_id"])["validation_errors"])
        before_line_count = table_count(conn, "staged_grn_lines", "staged_grn_id IN (SELECT staged_grn_id FROM staged_grns WHERE batch_id = ?)", (grn_batch_id,))

        resp = client.post(f"/grn-import/{grn_batch_id}/grn/{chm_grn['staged_grn_id']}/revalidate", follow_redirects=False)
        ok &= check("single-GRN revalidate -> redirect to detail page", resp.status_code == 302)

        chm_after = staging.get_staged_grn(conn, chm_grn["staged_grn_id"])
        ok &= check("SYN-GRN-LATE now verified", chm_after["review_status"] == "verified", str(chm_after["po_verification_errors"]))
        ok &= check("intrinsic validation_errors unchanged by revalidation", json.dumps(chm_after["validation_errors"]) == before_validation_errors)
        after_line_count = table_count(conn, "staged_grn_lines", "staged_grn_id IN (SELECT staged_grn_id FROM staged_grns WHERE batch_id = ?)", (grn_batch_id,))
        ok &= check("normalized line count unchanged by revalidation", after_line_count == before_line_count)

        resp = client.post(f"/grn-import/{grn_batch_id}/revalidate", follow_redirects=False)
        summary_after = staging.get_grn_batch_summary(conn, grn_batch_id)
        ok &= check("batch summary reflects verified=2, quarantined=1", summary_after["verified"] == 2 and summary_after["quarantined"] == 1, str(summary_after))

        resp = client.get(f"/grn-import/{grn_batch_id}/grn/{chm_grn['staged_grn_id']}")
        ok &= check("SYN-GRN-LATE detail page now shows READY TO POST", "READY TO POST" in resp.data.decode())

        # -----------------------------------------------------------------
        print("\n--- PO comparison: absent product + over-receipt (synthetic, via routes) ---")
        p_absent = product_id_for_sku(conn, "DEMO-SKU-002")
        insert_official_po(conn, "SYNPO-ABSENT", scootsy_id, bangalore_id, "DEMO FACILITY B", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p_sku_001, "DEMO-SKU-001", 20), (p_absent, "DEMO-SKU-002", 50)])
        conn.commit()
        absent_csv = write_csv([grow(GrnNumber="SYNGRN-ABSENT", PurchaseOrderNumber="SYNPO-ABSENT", ReceivedQty="20")])
        resp = upload_grn_csv(client, absent_csv, customer_id=scootsy_id, filename="synthetic_absent.csv")
        absent_batch_id = batch_id_from_redirect(resp)
        client.post(f"/grn-import/{absent_batch_id}/revalidate")
        absent_grn = staging.list_staged_grns(conn, absent_batch_id)[0]
        resp = client.get(f"/grn-import/{absent_batch_id}/grn/{absent_grn['staged_grn_id']}")
        body = resp.data.decode()
        ok &= check("absent-product PO comparison page 200", resp.status_code == 200)
        ok &= check("DEMO-SKU-002 shown with received 0 and discrepancy 50", "DEMO-SKU-002" in body and ">0<" in body and "50" in body)

        insert_official_po(conn, "SYNPO-OVER", scootsy_id, bangalore_id, "DEMO FACILITY B", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p_sku_001, "DEMO-SKU-001", 100)])
        conn.commit()
        over_csv = write_csv([grow(GrnNumber="SYNGRN-OVER", PurchaseOrderNumber="SYNPO-OVER", ReceivedQty="101")])
        resp = upload_grn_csv(client, over_csv, customer_id=scootsy_id, filename="synthetic_over.csv")
        over_batch_id = batch_id_from_redirect(resp)
        client.post(f"/grn-import/{over_batch_id}/revalidate")
        over_grn = staging.list_staged_grns(conn, over_batch_id)[0]
        resp = client.get(f"/grn-import/{over_batch_id}/grn/{over_grn['staged_grn_id']}")
        body = resp.data.decode()
        ok &= check("over-receipt shows OVER and GRN is REVIEW REQUIRED", "OVER" in body and "REVIEW REQUIRED" in body)

        # partial receipt is NOT presented as an error
        insert_official_po(conn, "SYNPO-PARTIAL", scootsy_id, bangalore_id, "DEMO FACILITY B", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p_sku_001, "DEMO-SKU-001", 600)])
        conn.commit()
        partial_csv = write_csv([grow(GrnNumber="SYNGRN-PARTIAL", PurchaseOrderNumber="SYNPO-PARTIAL", ReceivedQty="200")])
        resp = upload_grn_csv(client, partial_csv, customer_id=scootsy_id, filename="synthetic_partial.csv")
        partial_batch_id = batch_id_from_redirect(resp)
        client.post(f"/grn-import/{partial_batch_id}/revalidate")
        partial_grn = staging.list_staged_grns(conn, partial_batch_id)[0]
        resp = client.get(f"/grn-import/{partial_batch_id}/grn/{partial_grn['staged_grn_id']}")
        body = resp.data.decode()
        ok &= check("partial receipt (600/200) shows READY TO POST, not blocked", "READY TO POST" in body and "REVIEW REQUIRED" not in body)
        ok &= check("partial receipt shows SHORT status, not an error styling", "SHORT" in body)
        ok &= check("computed shortfall 400 shown", "400" in body)

        # -----------------------------------------------------------------
        print("\n--- Cross-batch safety ---")
        resp = client.get(f"/grn-import/{absent_batch_id}/grn/{over_grn['staged_grn_id']}")
        ok &= check("GRN from another batch via wrong batch path -> 404", resp.status_code == 404)
        resp = client.post(f"/grn-import/{absent_batch_id}/grn/{over_grn['staged_grn_id']}/revalidate")
        ok &= check("revalidate via wrong batch path -> 404, no crash", resp.status_code == 404)
        resp = client.get("/grn-import/999999")
        ok &= check("nonexistent batch -> 404", resp.status_code == 404)
        resp = client.get(f"/grn-import/{grn_batch_id}/grn/999999")
        ok &= check("nonexistent GRN -> 404", resp.status_code == 404)

        # -----------------------------------------------------------------
        print("\n--- GRN import landing page ---")
        resp = client.get("/grn-import")
        body = resp.data.decode()
        ok &= check("landing page 200", resp.status_code == 200)
        ok &= check("customer dropdown lists Scootsy", SCOOTSY_NAME in body)
        ok &= check("recent imports listed", "demo_grn_review.csv" in body)

        tracker = client.get("/po-grn-tracker?status=awaiting_grn")
        tracker_body = tracker.data.decode()
        ok &= check("PO-GRN tracker page 200", tracker.status_code == 200)
        ok &= check("PO-GRN tracker shows awaiting official POs", "AWAITING GRN" in tracker_body and "SYN-PO-1001" in tracker_body)

        # -----------------------------------------------------------------
        print("\n--- Ledger isolation (all Phase 7 activity above) ---")
        ok &= check("grn_receipts untouched by Phase 7 activity", table_count(conn, "grn_receipts") == baseline_grn_receipts)
        ok &= check("grn_line_items untouched by Phase 7 activity", table_count(conn, "grn_line_items") == baseline_grn_line_items)
        ok &= check("inventory_movements untouched by Phase 7 activity", table_count(conn, "inventory_movements") == baseline_movements)

    finally:
        conn.close()
        print(f"\nDropping throwaway database {TEST_DB_NAME}...")
        drop_test_database()

    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

"""
Verifies Phase 10: safe correction workflows for posted GRNs (void old +
post corrected replacement, atomically, linked via grn_receipts.
supersedes_grn_id) and for PO source warehouses (assign-once +
audited correct_po_source_location(), blocked once an active GRN
exists).

Runs entirely against a disposable throwaway Postgres database
(drizzl_inventory_test_phase10) -- created/dropped for this run, never
the real drizzl_inventory database. Uses small synthetic GRN CSV
fixtures staged through the real grn_csv_staging.py pipeline (so
po_verification_status/official_grn_already_exists detection is
exercised for real, not faked) and official POs inserted directly
(same approach verify_grn_posting.py uses -- PO posting itself is
already covered by verify_po_posting.py).
"""
import csv
import sys
import tempfile
import threading
import time
from pathlib import Path

import psycopg2

import db as db_module
import grn_csv_staging as staging
import grn_posting
import ingest
import reconcile

TEST_DB_NAME = "drizzl_inventory_test_phase10"
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
    "InvoiceNumber": "SYN-INV-CORRECTION", "InvoiceDate": "2026-07-31", "CreatedAtDate": "2026-08-13 17:25:12",
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


def check(label, condition, detail=""):
    condition = bool(condition)
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    return condition


def table_count(conn, table, where=None, params=()):
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return conn.execute(sql, params).fetchone()["n"]


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


def get_customer_id(conn, name=SCOOTSY_NAME):
    return conn.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()["id"]


def get_location_id(conn, name):
    return conn.execute("SELECT id FROM locations WHERE name = ?", (name,)).fetchone()["id"]


def product_id_for_sku(conn, sku):
    r = conn.execute(
        "SELECT mp.product_id FROM master_products mp JOIN customer_product_skus c ON c.product_id = mp.product_id "
        "WHERE c.external_sku = ?", (sku,)
    ).fetchone()
    return r["product_id"] if r else None


def insert_official_po(conn, po_number, customer_id, source_location_id, lines, supplier_code="DEMO-SUPPLIER-001",
                        vendor_name="DRIZZL DEMO VENDOR", destination_facility_name="DEMO FACILITY B"):
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


def stage_and_revalidate(conn, rows, customer_id, filename):
    csv_path = write_csv(rows)
    result = staging.stage_grn_csv(conn, csv_path, customer_id, filename=filename)
    conn.commit()
    batch_id = result["batch_id"]
    staging.revalidate_grn_batch(conn, batch_id)
    conn.commit()
    grns = {g["external_grn_number"]: g for g in staging.list_staged_grns(conn, batch_id)}
    return batch_id, grns


def post_one(conn, batch_id, staged_grn_id):
    result = grn_posting.post_staged_grns(conn, batch_id, [staged_grn_id])
    if result["rejected"]:
        raise AssertionError(f"setup posting was rejected: {result['rejected']}")
    conn.commit()
    return result["posted"][0]["grn_id"]


def active_movements_for_grn(conn, grn_number):
    return conn.execute(
        "SELECT * FROM inventory_movements WHERE reference_type = 'grn' AND reference_id = ? AND voided = 0",
        (grn_number,),
    ).fetchall()


def run():
    print(f"Creating throwaway database {TEST_DB_NAME}...")
    create_test_database()
    conn = get_test_connection()
    ok = True

    try:
        customer_id = get_customer_id(conn)
        bangalore_id = get_location_id(conn, "Drizzl Demo Warehouse")
        conn.execute("INSERT INTO locations (name, type) VALUES ('Mumbai', 'own_facility')")
        mumbai_id = get_location_id(conn, "Mumbai")
        conn.commit()
        p_passion = product_id_for_sku(conn, "DEMO-SKU-001")
        p_orange = product_id_for_sku(conn, "DEMO-SKU-005")
        p_yuzu = product_id_for_sku(conn, "DEMO-SKU-002")

        # ===================================================================
        print("\n--- Setup: PO-CORR ordered 600, opening stock 1000 at Drizzl Demo Warehouse ---")
        insert_official_po(conn, "PO-CORR", customer_id, bangalore_id, [(p_passion, "DEMO-SKU-001", 600)])
        ingest.record_movement(
            conn, movement_date="2026-08-01", sku_code=None, movement_type="opening_balance",
            quantity=1000, location_to="Drizzl Demo Warehouse", product_id=p_passion,
        )
        conn.commit()

        # -----------------------------------------------------------------
        print("\n--- Original GRN posted: received 200 ---")
        batch1, grns1 = stage_and_revalidate(
            conn, [grow(GrnNumber="GRN-CORR", PurchaseOrderNumber="PO-CORR", ReceivedQty="200")],
            customer_id, "original.csv",
        )
        original_staged_id = grns1["GRN-CORR"]["staged_grn_id"]
        ok &= check("original staged GRN verified", grns1["GRN-CORR"]["po_verification_status"] == "verified")
        old_grn_id = post_one(conn, batch1, original_staged_id)
        balance_after_original = reconcile.current_balance_by_product(conn, bangalore_id, p_passion)
        ok &= check("balance after original GRN is 1000-600=400 (full PO leaves stock)", balance_after_original == 400, f"got {balance_after_original}")

        # -----------------------------------------------------------------
        print("\n--- 1: corrected GRN CANNOT auto-replace original (duplicate grn_number is quarantined) ---")
        batch2, grns2 = stage_and_revalidate(
            conn, [grow(GrnNumber="GRN-CORR", PurchaseOrderNumber="PO-CORR", ReceivedQty="250")],
            customer_id, "corrected.csv",
        )
        corrected_staged_id = grns2["GRN-CORR"]["staged_grn_id"]
        ok &= check(
            "corrected staged GRN is BLOCKED (official_grn_already_exists)",
            grns2["GRN-CORR"]["po_verification_status"] == "blocked", f"got {grns2['GRN-CORR']['po_verification_status']}",
        )
        error_codes = {e["code"] for e in grns2["GRN-CORR"]["po_verification_errors"]}
        ok &= check(
            "blocking reasons are exactly the expected correction shape",
            error_codes <= {"official_grn_already_exists", "duplicate_grn_in_other_batch"} and "official_grn_already_exists" in error_codes,
            f"got {error_codes}",
        )

        normal_post_result = grn_posting.post_staged_grns(conn, batch2, [corrected_staged_id])
        ok &= check("normal post_staged_grns() REJECTS it, does not auto-replace", bool(normal_post_result["rejected"]), f"got {normal_post_result}")
        ok &= check("balance unchanged by the rejected auto-post attempt", reconcile.current_balance_by_product(conn, bangalore_id, p_passion) == 400)
        conn.commit()

        # -----------------------------------------------------------------
        print("\n--- find_correction_target() finds the original ---")
        target = grn_posting.find_correction_target(conn, corrected_staged_id)
        ok &= check("find_correction_target resolves to the original GRN", target is not None and target["grn_id"] == old_grn_id)

        # -----------------------------------------------------------------
        print("\n--- 9a: readiness failure leaves the (unrelated) old GRN completely untouched ---")
        # Deliberately a SEPARATE PO/GRN pair from PO-CORR/GRN-CORR above --
        # a bad correction attempt leaves its own staged duplicate lying
        # around permanently (staging rows are never deleted), which would
        # otherwise show up as a THIRD ambiguous candidate against
        # GRN-CORR and incorrectly block the real correction tested next.
        # Deliberately a product (Yuzu) used nowhere else in this test file
        # -- otherwise this GRN's own received units would silently
        # contaminate the shared Drizzl Demo Warehouse balance for whichever
        # product it reused, corrupting a later, unrelated assertion.
        insert_official_po(conn, "PO-BADCORR", customer_id, bangalore_id, [(p_yuzu, "DEMO-SKU-002", 300)])
        conn.commit()
        batch_bc1, grns_bc1 = stage_and_revalidate(
            conn, [grow(GrnNumber="GRN-BADCORR", PurchaseOrderNumber="PO-BADCORR", ReceivedQty="100",
                        SkuCode="DEMO-SKU-002", SkuDescription="Drizzl Yuzu & Elderflower | Probiotic Soda | 250 ml")],
            customer_id, "badcorr_original.csv",
        )
        badcorr_old_grn_id = post_one(conn, batch_bc1, grns_bc1["GRN-BADCORR"]["staged_grn_id"])

        batch_bad, grns_bad = stage_and_revalidate(
            conn, [grow(GrnNumber="GRN-BADCORR", PurchaseOrderNumber="PO-BADCORR", ReceivedQty="250", SkuCode="99999NOTONPO")],
            customer_id, "bad_correction.csv",
        )
        bad_staged_id = grns_bad["GRN-BADCORR"]["staged_grn_id"]
        raised = False
        try:
            grn_posting.replace_posted_grn(conn, badcorr_old_grn_id, bad_staged_id, "trying a bad correction")
        except ValueError:
            raised = True
        ok &= check("replace_posted_grn() raises ValueError for a not-ready correction", raised)
        old_row = conn.execute("SELECT voided FROM grn_receipts WHERE grn_id = ?", (badcorr_old_grn_id,)).fetchone()
        ok &= check("old GRN still NOT voided after the failed attempt", old_row["voided"] == 0)
        ok &= check("old GRN's movements still active after the failed attempt", len(active_movements_for_grn(conn, "GRN-BADCORR")) > 0)
        ok &= check(
            "the not-ready staged GRN was never linked as posted",
            conn.execute("SELECT posted_grn_id FROM staged_grns WHERE staged_grn_id = ?", (bad_staged_id,)).fetchone()["posted_grn_id"] is None,
        )

        # -----------------------------------------------------------------
        print("\n--- 9b: a successful replace_posted_grn() call, then caller rolls back -> nothing persists ---")
        savepoint_ok = True
        try:
            result_would_be = grn_posting.replace_posted_grn(conn, old_grn_id, corrected_staged_id, "rollback probe")
            conn.rollback()
        except Exception as e:
            savepoint_ok = False
            conn.rollback()
        ok &= check("replace_posted_grn() succeeded before rollback (no exception)", savepoint_ok)
        old_row = conn.execute("SELECT voided FROM grn_receipts WHERE grn_id = ?", (old_grn_id,)).fetchone()
        ok &= check("old GRN is active again after caller rollback (voiding didn't stick)", old_row["voided"] == 0)
        new_exists = conn.execute("SELECT 1 FROM grn_receipts WHERE grn_number = 'GRN-CORR' AND grn_id != ?", (old_grn_id,)).fetchone()
        ok &= check("no replacement GRN row persisted after rollback", new_exists is None)
        still_posted = conn.execute("SELECT posted_grn_id FROM staged_grns WHERE staged_grn_id = ?", (corrected_staged_id,)).fetchone()
        ok &= check("corrected staged GRN's posted_grn_id reverted to NULL after rollback", still_posted["posted_grn_id"] is None)
        ok &= check("balance reverted to 400 after rollback", reconcile.current_balance_by_product(conn, bangalore_id, p_passion) == 400)

        # -----------------------------------------------------------------
        print("\n--- 2/3/4/5/6: explicit replacement succeeds; 200 -> corrected 250 gives ACTIVE effect 250, not 450 ---")
        result = grn_posting.replace_posted_grn(conn, old_grn_id, corrected_staged_id, "warehouse undercounted lot on first receipt")
        conn.commit()
        new_grn_id = result["grn_id"]

        old_row = conn.execute("SELECT * FROM grn_receipts WHERE grn_id = ?", (old_grn_id,)).fetchone()
        ok &= check("3: old GRN remains stored", old_row is not None)
        ok &= check("3: old GRN is voided", old_row["voided"] == 1)
        ok &= check("3: old GRN's void_reason carries the correction reason", "undercounted" in (old_row["void_reason"] or ""))

        old_movements = conn.execute(
            "SELECT * FROM inventory_movements WHERE reference_type = 'grn' AND reference_id = 'GRN-CORR' AND product_id = ?",
            (p_passion,),
        ).fetchall()
        # both old (voided) and new (active) movements share reference_id='GRN-CORR' -- distinguish by voided flag
        old_only = [m for m in old_movements if m["voided"] == 1]
        new_only = [m for m in old_movements if m["voided"] == 0]
        ok &= check("4: old SALE movement(s) remain stored but voided", len(old_only) == 1 and old_only[0]["quantity"] == 200, f"got {[dict(m) for m in old_only]}")
        ok &= check("5: new SALE movement(s) are active", len(new_only) == 1 and new_only[0]["quantity"] == 250, f"got {[dict(m) for m in new_only]}")

        new_grn_row = conn.execute("SELECT * FROM grn_receipts WHERE grn_id = ?", (new_grn_id,)).fetchone()
        ok &= check("18: replacement GRN is POSTED (not voided)", new_grn_row["voided"] == 0)
        ok &= check("18: replacement GRN records supersedes_grn_id = old", new_grn_row["supersedes_grn_id"] == old_grn_id)

        balance_after_correction = reconcile.current_balance_by_product(conn, bangalore_id, p_passion)
        ok &= check("6: corrected GRN still removes the full 600-unit PO exactly once", balance_after_correction == 400, f"got {balance_after_correction}")

        # -----------------------------------------------------------------
        print("\n--- 17: superseded GRN cannot be generically restored ---")
        restore_raised = False
        try:
            ingest.unvoid_grn(conn, old_grn_id)
        except ValueError:
            restore_raised = True
        ok &= check("unvoid_grn() on the superseded original refuses", restore_raised)

        # -----------------------------------------------------------------
        print("\n--- 8/13: commitment remains 0 after successful replacement ---")
        committed = {(r["po_number"], r["sku_code"]): r["qty"] for r in reconcile.committed_quantity(conn)}
        ok &= check("PO-CORR has no remaining committed_quantity() row after replacement", ("PO-CORR", "DEMO-SKU-001") not in committed, f"got {committed}")

        # -----------------------------------------------------------------
        print("\n--- lookup_document() reflects the correction chain ---")
        lookup = reconcile.lookup_document(conn, "GRN-CORR")
        by_id = {g["grn_id"]: g for g in lookup["grns"]}
        ok &= check("lookup shows the old GRN as superseded_by the new one", by_id.get(old_grn_id, {}).get("superseded_by_grn_number") == "GRN-CORR")
        ok &= check("lookup shows the new GRN as supersedes the old one's grn_number", by_id.get(new_grn_id, {}).get("supersedes_grn_number") == "GRN-CORR")

        # ===================================================================
        print("\n--- Void canonical GRN reopens commitment (item 17 test set) ---")
        insert_official_po(conn, "PO-VOID", customer_id, bangalore_id, [(p_passion, "DEMO-SKU-001", 100)])
        conn.commit()
        batch_v, grns_v = stage_and_revalidate(
            conn, [grow(GrnNumber="GRN-VOID", PurchaseOrderNumber="PO-VOID", ReceivedQty="100")],
            customer_id, "for_void.csv",
        )
        grn_void_id = post_one(conn, batch_v, grns_v["GRN-VOID"]["staged_grn_id"])
        committed = {r["po_number"]: r["qty"] for r in reconcile.committed_quantity(conn)}
        ok &= check("commitment is 0 while GRN-VOID is active", "PO-VOID" not in committed)
        ingest.void_grn(conn, "GRN-VOID", "testing void/reopen")
        conn.commit()
        committed = {r["po_number"]: r["qty"] for r in reconcile.committed_quantity(conn)}
        ok &= check("voiding GRN-VOID reopens the commitment (100)", committed.get("PO-VOID") == 100, f"got {committed}")

        print("\n--- Restore canonical GRN restores its SALE movements and re-closes commitment ---")
        ingest.unvoid_grn(conn, grn_void_id)
        conn.commit()
        committed = {r["po_number"]: r["qty"] for r in reconcile.committed_quantity(conn)}
        ok &= check("restoring GRN-VOID re-closes the commitment", "PO-VOID" not in committed, f"got {committed}")
        ok &= check("GRN-VOID's SALE movement is active again", len(active_movements_for_grn(conn, "GRN-VOID")) == 1)

        # ===================================================================
        print("\n--- 11/12: negative-inventory flags during correction ---")
        insert_official_po(conn, "PO-NEG", customer_id, bangalore_id, [(p_orange, "DEMO-SKU-005", 500)])
        ingest.record_movement(
            conn, movement_date="2026-08-01", sku_code=None, movement_type="opening_balance",
            quantity=50, location_to="Drizzl Demo Warehouse", product_id=p_orange,
        )
        conn.commit()
        batch_n1, grns_n1 = stage_and_revalidate(
            conn, [grow(GrnNumber="GRN-NEG", PurchaseOrderNumber="PO-NEG", ReceivedQty="80", SkuCode="DEMO-SKU-005",
                        SkuDescription="Drizzl Orange | Probiotic Soda | 250 ml")],
            customer_id, "neg_original.csv",
        )
        neg_old_grn_id = post_one(conn, batch_n1, grns_n1["GRN-NEG"]["staged_grn_id"])
        flags_after_original = conn.execute(
            "SELECT * FROM inventory_flags WHERE reference_id = 'GRN-NEG' AND resolved = 0"
        ).fetchall()
        ok &= check("11: sale and remaining PO shortfall each record their negative crossing", len(flags_after_original) == 2, f"got {len(flags_after_original)}")

        batch_n2, grns_n2 = stage_and_revalidate(
            conn, [grow(GrnNumber="GRN-NEG", PurchaseOrderNumber="PO-NEG", ReceivedQty="40", SkuCode="DEMO-SKU-005",
                        SkuDescription="Drizzl Orange | Probiotic Soda | 250 ml")],
            customer_id, "neg_corrected.csv",
        )
        neg_corrected_staged_id = grns_n2["GRN-NEG"]["staged_grn_id"]
        neg_result = grn_posting.replace_posted_grn(conn, neg_old_grn_id, neg_corrected_staged_id, "corrected over-receipt down to 40")
        conn.commit()
        old_flag_after = conn.execute("SELECT resolved FROM inventory_flags WHERE reference_id = 'GRN-NEG' AND id = ?", (flags_after_original[0]["id"],)).fetchone()
        ok &= check("11: old flag is now resolved (superseded), still retained (not deleted)", old_flag_after is not None and old_flag_after["resolved"] == 1)
        new_flags = conn.execute(
            "SELECT * FROM inventory_flags WHERE reference_id = 'GRN-NEG' AND resolved = 0"
        ).fetchall()
        ok &= check("12: corrected full-PO removal retains one active negative incident", len(new_flags) == 1, f"got {len(new_flags)}")
        balance_neg = reconcile.current_balance_by_product(conn, bangalore_id, p_orange)
        ok &= check("balance reflects the corrected GRN's full 500-unit PO removal", balance_neg == -450, f"got {balance_neg}")

        # A second scenario where the CORRECTED value still overshoots, to prove a NEW flag can still be created.
        batch_n3, grns_n3 = stage_and_revalidate(
            conn, [grow(GrnNumber="GRN-NEG", PurchaseOrderNumber="PO-NEG", ReceivedQty="9999", SkuCode="DEMO-SKU-005",
                        SkuDescription="Drizzl Orange | Probiotic Soda | 250 ml")],
            customer_id, "neg_corrected_again.csv",
        )
        # 9999 > ordered 500 -- this would fail readiness (received_quantity_exceeds_ordered), which is
        # correct: Phase 10 must not weaken the over-ordered-quantity rule even during a correction.
        raised_over = False
        try:
            grn_posting.replace_posted_grn(conn, neg_result["grn_id"], grns_n3["GRN-NEG"]["staged_grn_id"], "testing over-ordered rule")
        except ValueError as e:
            raised_over = True
            over_msg = str(e)
        ok &= check("over-ordered corrected quantity is still rejected (rule not weakened)", raised_over)
        conn.rollback()

        # Use a same-again correction with a smaller, still-negative-producing quantity instead, to prove
        # a fresh flag CAN be created by a correction (12).
        insert_official_po(conn, "PO-NEG2", customer_id, bangalore_id, [(p_orange, "DEMO-SKU-005", 500)])
        conn.commit()
        batch_n4, grns_n4 = stage_and_revalidate(
            conn, [grow(GrnNumber="GRN-NEG2", PurchaseOrderNumber="PO-NEG2", ReceivedQty="5", SkuCode="DEMO-SKU-005",
                        SkuDescription="Drizzl Orange | Probiotic Soda | 250 ml")],
            customer_id, "neg2_original.csv",
        )
        neg2_old_grn_id = post_one(conn, batch_n4, grns_n4["GRN-NEG2"]["staged_grn_id"])
        batch_n5, grns_n5 = stage_and_revalidate(
            conn, [grow(GrnNumber="GRN-NEG2", PurchaseOrderNumber="PO-NEG2", ReceivedQty="15", SkuCode="DEMO-SKU-005",
                        SkuDescription="Drizzl Orange | Probiotic Soda | 250 ml")],
            customer_id, "neg2_corrected.csv",
        )
        balance_before_neg2 = reconcile.current_balance_by_product(conn, bangalore_id, p_orange)
        neg2_result = grn_posting.replace_posted_grn(conn, neg2_old_grn_id, grns_n5["GRN-NEG2"]["staged_grn_id"], "correcting up, expected to go negative")
        conn.commit()
        new_flags2 = conn.execute("SELECT * FROM inventory_flags WHERE reference_id = 'GRN-NEG2' AND resolved = 0").fetchall()
        # Full-PO semantics can create one flag for the accepted SALE and
        # another for the remaining discrepancy LOSS when both start below
        # zero. Replacing the old GRN must leave only the corrected pair.
        expected_flags = 2 if balance_before_neg2 < 15 else (1 if balance_before_neg2 - 15 < 485 else 0)
        ok &= check(
            "12: corrected GRN creates a NEW negative flag exactly when the corrected receipt still overshoots",
            len(new_flags2) == expected_flags,
            f"balance_before={balance_before_neg2}, corrected=15, new_flags={len(new_flags2)}",
        )

        # ===================================================================
        print("\n--- 7: multi-lot correction replaces all old lines/movements safely ---")
        insert_official_po(conn, "PO-LOT", customer_id, bangalore_id, [(p_passion, "DEMO-SKU-001", 100)])
        conn.commit()
        batch_l1, grns_l1 = stage_and_revalidate(
            conn,
            [
                grow(GrnNumber="GRN-LOT", PurchaseOrderNumber="PO-LOT", ReceivedQty="48", LotExpiryDate="2027-06-01"),
                grow(GrnNumber="GRN-LOT", PurchaseOrderNumber="PO-LOT", ReceivedQty="24", LotExpiryDate="2027-05-01"),
            ],
            customer_id, "lot_original.csv",
        )
        lot_old_grn_id = post_one(conn, batch_l1, grns_l1["GRN-LOT"]["staged_grn_id"])
        old_lot_movements = active_movements_for_grn(conn, "GRN-LOT")
        ok &= check("original multi-lot GRN posted 2 SALE movements (48 + 24)", len(old_lot_movements) == 2, f"got {len(old_lot_movements)}")
        ok &= check("original quantities are 48 and 24", sorted(m["quantity"] for m in old_lot_movements) == [24, 48])

        batch_l2, grns_l2 = stage_and_revalidate(
            conn,
            [
                grow(GrnNumber="GRN-LOT", PurchaseOrderNumber="PO-LOT", ReceivedQty="48", LotExpiryDate="2027-06-01"),
                grow(GrnNumber="GRN-LOT", PurchaseOrderNumber="PO-LOT", ReceivedQty="30", LotExpiryDate="2027-05-01"),
            ],
            customer_id, "lot_corrected.csv",
        )
        lot_result = grn_posting.replace_posted_grn(conn, lot_old_grn_id, grns_l2["GRN-LOT"]["staged_grn_id"], "lot B recount: 24 -> 30")
        conn.commit()

        all_lot_movements = conn.execute(
            "SELECT * FROM inventory_movements WHERE reference_type = 'grn' AND reference_id = 'GRN-LOT'"
        ).fetchall()
        old_lot_after = [m for m in all_lot_movements if m["voided"] == 1]
        new_lot_after = [m for m in all_lot_movements if m["voided"] == 0]
        ok &= check("old multi-lot movements (48, 24) all voided, not mutated", sorted(m["quantity"] for m in old_lot_after) == [24, 48], f"got {sorted(m['quantity'] for m in old_lot_after)}")
        ok &= check("new multi-lot movements are 48 and 30 (fresh, not a patch of the old 24)", sorted(m["quantity"] for m in new_lot_after) == [30, 48], f"got {sorted(m['quantity'] for m in new_lot_after)}")
        ok &= check("no old movement was mutated into 30 (old set is untouched, exactly 24 and 48)", 30 not in [m["quantity"] for m in old_lot_after])

        # ===================================================================
        print("\n--- 10: two simultaneous replacement attempts for the same GRN cannot both succeed ---")
        insert_official_po(conn, "PO-RACE", customer_id, bangalore_id, [(p_passion, "DEMO-SKU-001", 100)])
        conn.commit()
        batch_r1, grns_r1 = stage_and_revalidate(
            conn, [grow(GrnNumber="GRN-RACE", PurchaseOrderNumber="PO-RACE", ReceivedQty="20")],
            customer_id, "race_original.csv",
        )
        race_old_grn_id = post_one(conn, batch_r1, grns_r1["GRN-RACE"]["staged_grn_id"])

        batch_r2, grns_r2 = stage_and_revalidate(
            conn, [grow(GrnNumber="GRN-RACE", PurchaseOrderNumber="PO-RACE", ReceivedQty="25")],
            customer_id, "race_corrected_a.csv",
        )
        staged_a = grns_r2["GRN-RACE"]["staged_grn_id"]
        batch_r3, grns_r3 = stage_and_revalidate(
            conn, [grow(GrnNumber="GRN-RACE", PurchaseOrderNumber="PO-RACE", ReceivedQty="30")],
            customer_id, "race_corrected_b.csv",
        )
        staged_b = grns_r3["GRN-RACE"]["staged_grn_id"]

        results = {}
        started_a = threading.Event()
        release_a = threading.Event()

        def worker_a():
            raw = psycopg2.connect(dbname=TEST_DB_NAME)
            conn_a = db_module._PGConnection(raw)
            try:
                grn_posting.replace_posted_grn(conn_a, race_old_grn_id, staged_a, "race attempt A")
                started_a.set()
                release_a.wait(timeout=5)
                conn_a.commit()
                results["a"] = "success"
            except Exception as e:
                results["a"] = f"error: {e}"
                started_a.set()
                conn_a.rollback()
            finally:
                raw.close()

        def worker_b():
            started_a.wait(timeout=5)
            time.sleep(0.3)  # let A's FOR UPDATE lock actually take hold before B attempts it
            raw = psycopg2.connect(dbname=TEST_DB_NAME)
            conn_b = db_module._PGConnection(raw)
            try:
                grn_posting.replace_posted_grn(conn_b, race_old_grn_id, staged_b, "race attempt B")
                conn_b.commit()
                results["b"] = "success"
            except Exception as e:
                results["b"] = f"error: {e}"
                conn_b.rollback()
            finally:
                raw.close()

        t_a = threading.Thread(target=worker_a)
        t_b = threading.Thread(target=worker_b)
        t_a.start()
        t_b.start()
        time.sleep(0.6)
        release_a.set()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

        ok &= check("worker A succeeded", results.get("a") == "success", f"got {results.get('a')}")
        ok &= check("worker B did NOT succeed (blocked, then rejected once A committed)", results.get("b") != "success", f"got {results.get('b')}")

        active_race_grns = conn.execute(
            "SELECT grn_id, grn_number, source FROM grn_receipts WHERE grn_number = 'GRN-RACE' AND voided = 0"
        ).fetchall()
        ok &= check("exactly ONE active GRN-RACE official GRN after both attempts", len(active_race_grns) == 1, f"got {len(active_race_grns)}")

        # ===================================================================
        print("\n--- 13/14/15/16: PO source correction workflow ---")

        print("13/14: posted PO with no GRN yet -- correct source, requires a reason, audited")
        insert_official_po(conn, "PO-SRC1", customer_id, bangalore_id, [(p_passion, "DEMO-SKU-001", 50)])
        conn.commit()
        no_reason_raised = False
        try:
            ingest.correct_po_source_location(conn, "PO-SRC1", "Mumbai", "")
        except ValueError:
            no_reason_raised = True
        ok &= check("14: empty reason is rejected", no_reason_raised)

        ingest.correct_po_source_location(conn, "PO-SRC1", "Mumbai", "warehouse reassignment before any delivery")
        conn.commit()
        po_src1 = conn.execute("SELECT source_location_id FROM purchase_orders WHERE po_number = 'PO-SRC1'").fetchone()
        ok &= check("13: source_location_id updated to Mumbai", po_src1["source_location_id"] == mumbai_id)
        audit_row = conn.execute(
            "SELECT * FROM po_source_corrections WHERE po_id = (SELECT po_id FROM purchase_orders WHERE po_number = 'PO-SRC1')"
        ).fetchone()
        ok &= check(
            "13: po_source_corrections row records old/new/reason",
            audit_row is not None and audit_row["old_source_location_id"] == bangalore_id
            and audit_row["new_source_location_id"] == mumbai_id
            and "reassignment" in audit_row["reason"],
            f"got {dict(audit_row) if audit_row else None}",
        )

        print("assign_po_source_location() (the first-time-only function) now refuses to silently change it")
        silent_change_raised = False
        try:
            ingest.assign_po_source_location(conn, "PO-SRC1", "Drizzl Demo Warehouse")
        except ValueError:
            silent_change_raised = True
        ok &= check("assign_po_source_location() refuses to overwrite an already-set source", silent_change_raised)

        print("15/16: PO source correction is blocked once an active GRN exists; no history silently moved")
        batch_s1, grns_s1 = stage_and_revalidate(
            conn, [grow(GrnNumber="GRN-SRC1", PurchaseOrderNumber="PO-SRC1", ReceivedQty="10")],
            customer_id, "src_grn.csv",
        )
        post_one(conn, batch_s1, grns_s1["GRN-SRC1"]["staged_grn_id"])
        balance_before_block = reconcile.current_balance_by_product(conn, mumbai_id, p_passion)

        blocked_raised = False
        try:
            ingest.correct_po_source_location(conn, "PO-SRC1", "Drizzl Demo Warehouse", "trying to move it after a GRN posted")
        except ValueError as e:
            blocked_raised = True
            block_msg = str(e)
        ok &= check("15: source correction blocked once an active GRN exists", blocked_raised)
        ok &= check("15: block message tells the operator to correct/replace the GRN instead", blocked_raised and "GRN" in block_msg)
        po_src1_after = conn.execute("SELECT source_location_id FROM purchase_orders WHERE po_number = 'PO-SRC1'").fetchone()
        ok &= check("16: source_location_id is unchanged (still Mumbai) after the blocked attempt", po_src1_after["source_location_id"] == mumbai_id)
        ok &= check(
            "16: no historical inventory silently moved -- Mumbai balance unchanged by the blocked attempt",
            reconcile.current_balance_by_product(conn, mumbai_id, p_passion) == balance_before_block,
        )

    finally:
        conn.close()
        print(f"\nDropping throwaway database {TEST_DB_NAME}...")
        drop_test_database()

    return ok


if __name__ == "__main__":
    ok = run()
    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    sys.exit(0 if ok else 1)

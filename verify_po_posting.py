"""
Verifies Phase 5: posting a READY staged PO into the official ledger
(po_posting.py), plus the compatibility-preserving enrichment of
reconcile.py's commitment layer.

Unlike the Phase 3/4 verify scripts, this one does NOT run against the
real drizzl_inventory database, not even under a SAVEPOINT -- posting
writes real official business records (purchase_orders/po_line_items),
and the user asked that this class of write happen only in a disposable
database. This script creates/drops its own throwaway Postgres database
(drizzl_inventory_test_phase5) for the whole run, built from the same
schema_postgres.sql a fresh install would use.

Uses the real Scootsy PO export (PO_0000000000001.csv) for the primary
posting checks (12 staged POs / 51 lines / 51 resolved products, per
PROJECT_HANDOFF.md), plus small synthetic CSV fixtures and direct SQL
fixtures for the conflict/edge-case tests that need data the real file
doesn't contain (an unmapped SKU, a second customer, a legacy PDF-style
line with no product_id).
"""
import csv
import sys
import tempfile
from pathlib import Path

import psycopg2

import db as db_module
import po_csv_staging as staging
import po_posting
import reconcile

REAL_CSV_PATH = Path("/Users/demo/Desktop/Swiggy test PO GRN data/last 7 po csv/PO_0000000000001.csv")
SCOOTSY_NAME = "Scootsy Logistics Private Limited"
TEST_DB_NAME = "drizzl_inventory_test_phase5"

REAL_HEADER = [
    "PoNumber", "Entity", "FacilityId", "FacilityName", "City", "PoCreatedAt", "PoModifiedAt",
    "Status", "SupplierCode", "VendorName", "PoAmount", "SkuCode", "SkuDescription", "CategoryId",
    "OrderedQty", "ReceivedQty", "BalancedQty", "Tax", "PoLineValueWithoutTax", "PoLineValueWithTax",
    "Mrp", "UnitBasedCost", "ExpectedDeliveryDate", "PoExpiryDate", "OtbReferenceNumber",
    "InternalExternalPo", "PoAgeing", "BrandName", "ReferencePoNumber",
]

BASE_ROW = {
    "PoNumber": "SYNPO0001", "Entity": "",
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


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    return condition


def table_count(conn, table, where=None, params=()):
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return conn.execute(sql, params).fetchone()["n"]


# ---------------------------------------------------------------------------
# Throwaway database lifecycle
# ---------------------------------------------------------------------------

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


def get_customer_id(conn, name):
    r = conn.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()
    return r["id"] if r else None


def get_location_id(conn, name):
    r = conn.execute("SELECT id FROM locations WHERE name = ?", (name,)).fetchone()
    return r["id"] if r else None


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run():
    if not REAL_CSV_PATH.exists():
        print(f"FAIL -- real sample CSV not found at {REAL_CSV_PATH}")
        return False

    print(f"Creating throwaway database {TEST_DB_NAME}...")
    create_test_database()
    conn = get_test_connection()
    ok = True

    try:
        scootsy_id = get_customer_id(conn, SCOOTSY_NAME)
        bangalore_id = get_location_id(conn, "Drizzl Demo Warehouse")
        ok &= check("Scootsy seeded", scootsy_id is not None)
        ok &= check("Drizzl Demo Warehouse seeded", bangalore_id is not None)

        baseline_movements = table_count(conn, "inventory_movements")
        baseline_grns = table_count(conn, "grn_receipts")

        # -----------------------------------------------------------------
        print("\n--- Staging the real Scootsy CSV ---")
        result = staging.stage_po_csv(conn, str(REAL_CSV_PATH), customer_id=scootsy_id, filename="PO_0000000000001.csv")
        conn.commit()
        batch_real = result["batch_id"]
        summary = staging.batch_summary(conn, batch_real)
        ok &= check("12 staged POs", summary["orders"] == 12, f"got {summary['orders']}")
        ok &= check("51 staged lines", summary["lines"] == 51, f"got {summary['lines']}")
        ok &= check("0 blocked (all SKUs resolve)", summary["blocked"] == 0, f"got {summary['blocked']}")

        pos = sorted(staging.list_staged_pos(conn, batch_real), key=lambda p: p["staged_po_id"])
        resolved = sum(
            1 for p in pos for l in staging.get_staged_po(conn, p["staged_po_id"])["lines"]
            if l["product_id"] is not None
        )
        ok &= check("51/51 product mappings resolved", resolved == 51, f"got {resolved}")

        p0, p1, p2, p3, p4, p5, p6, p7, p8, p9 = pos[:10]

        # -----------------------------------------------------------------
        print("\n--- Test 1: basic post, product identity, quantity, no legacy products created ---")
        staging.assign_source_location(conn, batch_real, [p0["staged_po_id"]], bangalore_id)
        conn.commit()
        staged_p0 = staging.get_staged_po(conn, p0["staged_po_id"])
        lines_before = staged_p0["lines"]

        po_count_before = table_count(conn, "purchase_orders")
        line_count_before = table_count(conn, "po_line_items")
        legacy_products_before = table_count(conn, "products")

        r1 = po_posting.post_staged_purchase_orders(conn, batch_real, [p0["staged_po_id"]])
        ok &= check("Test1: posted list has 1 entry", len(r1["posted"]) == 1, str(r1))
        ok &= check("Test1: nothing rejected", not r1["rejected"], str(r1["rejected"]))
        conn.commit()

        po_id_0 = r1["posted"][0]["po_id"] if r1["posted"] else None
        official_po = conn.execute("SELECT * FROM purchase_orders WHERE po_id = ?", (po_id_0,)).fetchone() if po_id_0 else None
        ok &= check("Test1: official PO created", official_po is not None)
        if official_po:
            ok &= check("Test1: po_number matches", official_po["po_number"] == staged_p0["external_po_number"])
            ok &= check("Test1: customer_id matches", official_po["customer_id"] == scootsy_id)
            ok &= check("Test1: source_location_id matches assigned source exactly", official_po["source_location_id"] == bangalore_id)
            ok &= check(
                "Test1: destination_* kept separate from source",
                official_po["destination_facility_id"] == staged_p0["destination_facility_id"]
                and official_po["destination_facility_name"] == staged_p0["destination_facility_name"]
                and official_po["destination_city"] == staged_p0["destination_city"]
                and official_po["source_location_id"] != None,
            )
            ok &= check(
                "Test1: facility_name mirrors destination_facility_name (legacy compat)",
                official_po["facility_name"] == staged_p0["destination_facility_name"],
            )

        official_lines = conn.execute("SELECT * FROM po_line_items WHERE po_number = ?", (staged_p0["external_po_number"],)).fetchall()
        ok &= check("Test1: one official line per staged line", len(official_lines) == len(lines_before), f"{len(official_lines)} vs {len(lines_before)}")
        official_by_id = {l["id"]: l for l in official_lines}
        refreshed_lines = staging.get_staged_po(conn, p0["staged_po_id"])["lines"]
        all_qty_ok, all_identity_ok = True, True
        for sl in refreshed_lines:
            ol = official_by_id.get(sl["posted_line_item_id"])
            if ol is None:
                all_qty_ok = all_identity_ok = False
                continue
            if float(ol["qty"]) != float(sl["ordered_qty"]):
                all_qty_ok = False
            if ol["product_id"] != sl["product_id"] or ol["external_sku"] != sl["external_sku"] or ol["item_code"] != sl["external_sku"]:
                all_identity_ok = False
        ok &= check("Test1: qty = ordered_qty (never received/balanced)", all_qty_ok)
        ok &= check("Test1: product_id/external_sku/item_code preserved exactly", all_identity_ok)

        legacy_products_after = table_count(conn, "products")
        ok &= check("Test1: no legacy products row created as a side effect", legacy_products_after == legacy_products_before, f"{legacy_products_before} -> {legacy_products_after}")

        staged_p0_after = conn.execute("SELECT posted_po_id, posted_at FROM staged_purchase_orders WHERE staged_po_id = ?", (p0["staged_po_id"],)).fetchone()
        ok &= check("Test1: staged_purchase_orders.posted_po_id set", staged_p0_after["posted_po_id"] == po_id_0)
        ok &= check("Test1: staged_purchase_orders.posted_at set", staged_p0_after["posted_at"] is not None)

        # -----------------------------------------------------------------
        print("\n--- Test 2: idempotency (posting the same staged PO twice) ---")
        po_count_after_1 = table_count(conn, "purchase_orders")
        line_count_after_1 = table_count(conn, "po_line_items")
        r2 = po_posting.post_staged_purchase_orders(conn, batch_real, [p0["staged_po_id"]])
        conn.commit()
        ok &= check("Test2: second call reports already_posted, not posted", len(r2["already_posted"]) == 1 and not r2["posted"], str(r2))
        ok &= check("Test2: already_posted references same po_id", r2["already_posted"][0]["po_id"] == po_id_0)
        ok &= check("Test2: no duplicate purchase_orders row", table_count(conn, "purchase_orders") == po_count_after_1)
        ok &= check("Test2: no duplicate po_line_items rows", table_count(conn, "po_line_items") == line_count_after_1)

        # -----------------------------------------------------------------
        print("\n--- Test 3: multi-PO atomicity (3 POs posted together) ---")
        for p in (p1, p2, p3):
            staging.assign_source_location(conn, batch_real, [p["staged_po_id"]], bangalore_id)
        conn.commit()
        expected_new_lines = sum(len(staging.get_staged_po(conn, p["staged_po_id"])["lines"]) for p in (p1, p2, p3))
        po_before = table_count(conn, "purchase_orders")
        line_before = table_count(conn, "po_line_items")
        r3 = po_posting.post_staged_purchase_orders(conn, batch_real, [p1["staged_po_id"], p2["staged_po_id"], p3["staged_po_id"]])
        ok &= check("Test3: all 3 posted", len(r3["posted"]) == 3, str(r3))
        conn.commit()
        ok &= check("Test3: purchase_orders grew by exactly 3", table_count(conn, "purchase_orders") - po_before == 3)
        ok &= check("Test3: po_line_items grew by exactly the 3 POs' line count", table_count(conn, "po_line_items") - line_before == expected_new_lines)

        # -----------------------------------------------------------------
        print("\n--- Test 4: not-ready protection ---")
        # 4a: valid data, no source assigned
        po_before = table_count(conn, "purchase_orders")
        r4a = po_posting.post_staged_purchase_orders(conn, batch_real, [p4["staged_po_id"]])
        ok &= check("Test4a: valid+no-source is rejected", bool(r4a["rejected"]) and not r4a["posted"], str(r4a))
        ok &= check("Test4a: nothing written", table_count(conn, "purchase_orders") == po_before)

        # Synthetic blocked staged PO (unmapped SKU), source assigned anyway (allowed, per Phase 4 rule)
        blocked_csv = write_csv([row(PoNumber="SYN-BLOCKED-0001", SkuCode="999999", SkuDescription="Unknown SKU")])
        rblk = staging.stage_po_csv(conn, blocked_csv, customer_id=scootsy_id, filename="synthetic_blocked.csv")
        conn.commit()
        batch_blocked = rblk["batch_id"]
        blocked_po = staging.list_staged_pos(conn, batch_blocked)[0]
        ok &= check("Synthetic fixture is BLOCKED", blocked_po["review_status"] == "blocked", blocked_po["review_status"])
        staging.assign_source_location(conn, batch_blocked, [blocked_po["staged_po_id"]], bangalore_id)
        conn.commit()
        blocked_po = staging.get_staged_po(conn, blocked_po["staged_po_id"])
        ok &= check("Source assignment does not un-block it", blocked_po["review_status"] == "blocked", blocked_po["review_status"])

        # 4b: blocked + source assigned, alone
        po_before = table_count(conn, "purchase_orders")
        r4b = po_posting.post_staged_purchase_orders(conn, batch_blocked, [blocked_po["staged_po_id"]])
        ok &= check("Test4b: blocked+source is still rejected", bool(r4b["rejected"]) and not r4b["posted"], str(r4b))
        ok &= check("Test4b: nothing written", table_count(conn, "purchase_orders") == po_before)

        # 4c: mix 2 ready + 1 blocked (different batches) -- everything must be rejected, atomically, per PO
        staging.assign_source_location(conn, batch_real, [p6["staged_po_id"], p7["staged_po_id"]], bangalore_id)
        conn.commit()
        po_before = table_count(conn, "purchase_orders")
        try:
            po_posting.post_staged_purchase_orders(conn, batch_real, [p6["staged_po_id"], p7["staged_po_id"], blocked_po["staged_po_id"]])
            ok &= check("Test4c: mixed-batch call raises PostingError (cross-batch id)", False, "did not raise")
        except po_posting.PostingError:
            ok &= check("Test4c: mixed-batch call raises PostingError (cross-batch id)", True)
        ok &= check("Test4c: nothing written", table_count(conn, "purchase_orders") == po_before)
        p6_after = conn.execute("SELECT posted_po_id FROM staged_purchase_orders WHERE staged_po_id = ?", (p6["staged_po_id"],)).fetchone()
        ok &= check("Test4c: otherwise-ready PO in the mix stayed unposted", p6_after["posted_po_id"] is None)

        # -----------------------------------------------------------------
        print("\n--- Test 5: cross-batch protection ---")
        po_before = table_count(conn, "purchase_orders")
        try:
            po_posting.post_staged_purchase_orders(conn, batch_blocked, [p9["staged_po_id"]])
            ok &= check("Test5: posting a foreign staged_po_id under the wrong batch_id raises PostingError", False, "did not raise")
        except po_posting.PostingError:
            ok &= check("Test5: posting a foreign staged_po_id under the wrong batch_id raises PostingError", True)
        ok &= check("Test5: nothing written", table_count(conn, "purchase_orders") == po_before)

        # -----------------------------------------------------------------
        print("\n--- Test 6: different-batch duplicate conflict ---")
        staging.assign_source_location(conn, batch_real, [p5["staged_po_id"]], bangalore_id)
        conn.commit()
        p5_number = p5["external_po_number"]
        r6a = po_posting.post_staged_purchase_orders(conn, batch_real, [p5["staged_po_id"]])
        ok &= check("Test6: original P5 posts cleanly", len(r6a["posted"]) == 1, str(r6a))
        conn.commit()
        p5_po_id = r6a["posted"][0]["po_id"]
        original_row = conn.execute("SELECT * FROM purchase_orders WHERE po_id = ?", (p5_po_id,)).fetchone()

        dup_csv = write_csv([row(PoNumber=p5_number, SkuCode="DEMO-SKU-002", SkuDescription="Drizzl Yuzu | Probiotic Soda", OrderedQty="99")])
        rdup = staging.stage_po_csv(conn, dup_csv, customer_id=scootsy_id, filename="synthetic_duplicate.csv")
        conn.commit()
        batch_dup = rdup["batch_id"]
        dup_po = staging.list_staged_pos(conn, batch_dup)[0]
        staging.assign_source_location(conn, batch_dup, [dup_po["staged_po_id"]], bangalore_id)
        conn.commit()

        po_before = table_count(conn, "purchase_orders")
        r6b = po_posting.post_staged_purchase_orders(conn, batch_dup, [dup_po["staged_po_id"]])
        ok &= check(
            "Test6: changed same-number PO is quarantined for review, not rejected with unrelated new orders",
            not r6b["rejected"] and len(r6b["skipped_existing"]) == 1
            and r6b["skipped_existing"][0]["status"] == "review_required",
            str(r6b),
        )
        ok &= check("Test6: nothing new written", table_count(conn, "purchase_orders") == po_before)
        original_row_after = conn.execute("SELECT * FROM purchase_orders WHERE po_id = ?", (p5_po_id,)).fetchone()
        ok &= check("Test6: original official PO left completely unmodified", dict(original_row) == dict(original_row_after))
        dup_after = conn.execute("SELECT posted_po_id FROM staged_purchase_orders WHERE staged_po_id = ?", (dup_po["staged_po_id"],)).fetchone()
        ok &= check("Test6: the duplicate staged record was not linked to the existing official PO", dup_after["posted_po_id"] is None)

        decision_po_id = po_posting.record_duplicate_decision(
            conn, dup_po["staged_po_id"], "keep_existing", "Quantity differs; preserve the approved official order."
        )
        conn.commit()
        reviewed = staging.get_staged_po(conn, dup_po["staged_po_id"])
        ok &= check("Test6: review decision links to the existing official PO", decision_po_id == p5_po_id)
        ok &= check("Test6: review decision persists as reviewed_duplicate", reviewed["review_status"] == "reviewed_duplicate", reviewed["review_status"])

        # -----------------------------------------------------------------
        print("\n--- Test 7: cross-customer PO-number collision (temporary global UNIQUE(po_number)) ---")
        conn.execute("INSERT INTO customers (name) VALUES (?)", ("Test Retailer Co",))
        conn.commit()
        other_customer_id = get_customer_id(conn, "Test Retailer Co")
        other_batch = conn.execute(
            "INSERT INTO po_import_batches (customer_id, source_filename, file_sha256) VALUES (?, ?, ?) RETURNING batch_id",
            (other_customer_id, "synthetic_other_customer.csv", "deadbeef" * 8),
        ).fetchone()["batch_id"]
        product_id_any = conn.execute("SELECT product_id FROM master_products LIMIT 1").fetchone()["product_id"]
        raw_row_id = conn.execute(
            "INSERT INTO po_import_rows (batch_id, source_row_number, raw_data) VALUES (?, ?, ?) RETURNING row_id",
            (other_batch, 1, "{}"),
        ).fetchone()["row_id"]
        other_staged_po_id = conn.execute(
            """
            INSERT INTO staged_purchase_orders
                (batch_id, customer_id, external_po_number, source_location_id, validation_status)
            VALUES (?, ?, ?, ?, 'valid')
            RETURNING staged_po_id
            """,
            (other_batch, other_customer_id, p0["external_po_number"], bangalore_id),
        ).fetchone()["staged_po_id"]
        conn.execute(
            """
            INSERT INTO staged_po_lines
                (staged_po_id, raw_row_id, source_row_number, external_sku, product_id, ordered_qty, validation_status)
            VALUES (?, ?, ?, ?, ?, ?, 'valid')
            """,
            (other_staged_po_id, raw_row_id, 1, "OTHER-SKU-1", product_id_any, 5),
        )
        conn.commit()

        po_before = table_count(conn, "purchase_orders")
        r7 = po_posting.post_staged_purchase_orders(conn, other_batch, [other_staged_po_id])
        ok &= check("Test7: cross-customer same-po_number is rejected, not a raw IntegrityError", bool(r7["rejected"]), str(r7))
        ok &= check("Test7: nothing written", table_count(conn, "purchase_orders") == po_before)

        # -----------------------------------------------------------------
        print("\n--- Test 8: posted staged PO locks its source ---")
        conn.execute("INSERT INTO locations (name, type) VALUES (?, ?)", ("Mumbai Test", "own_facility"))
        conn.commit()
        mumbai_id = get_location_id(conn, "Mumbai Test")
        staging.assign_source_location(conn, batch_real, [p8["staged_po_id"]], bangalore_id)
        conn.commit()
        r8 = po_posting.post_staged_purchase_orders(conn, batch_real, [p8["staged_po_id"]])
        ok &= check("Test8: P8 posts cleanly", len(r8["posted"]) == 1, str(r8))
        conn.commit()
        p8_po_id = r8["posted"][0]["po_id"]

        try:
            staging.assign_source_location(conn, batch_real, [p8["staged_po_id"]], mumbai_id)
            ok &= check("Test8: reassigning source on a posted staged PO is rejected", False, "did not raise")
        except ValueError:
            ok &= check("Test8: reassigning source on a posted staged PO is rejected", True)
        conn.rollback()
        staged_source_after = conn.execute("SELECT source_location_id FROM staged_purchase_orders WHERE staged_po_id = ?", (p8["staged_po_id"],)).fetchone()
        official_source_after = conn.execute("SELECT source_location_id FROM purchase_orders WHERE po_id = ?", (p8_po_id,)).fetchone()
        ok &= check("Test8: staged source unchanged", staged_source_after["source_location_id"] == bangalore_id)
        ok &= check("Test8: official source unchanged", official_source_after["source_location_id"] == bangalore_id)

        # -----------------------------------------------------------------
        print("\n--- Test 9: commitments reflect canonical identity ---")
        staging.assign_source_location(conn, batch_real, [p9["staged_po_id"]], bangalore_id)
        conn.commit()
        r9 = po_posting.post_staged_purchase_orders(conn, batch_real, [p9["staged_po_id"]])
        ok &= check("Test9: P9 posts cleanly", len(r9["posted"]) == 1, str(r9))
        conn.commit()
        staged_p9 = staging.get_staged_po(conn, p9["staged_po_id"])
        committed_rows = [r for r in reconcile.committed_quantity(conn) if r["po_number"] == staged_p9["external_po_number"]]
        ok &= check("Test9: committed_quantity() has a row per line", len(committed_rows) == len(staged_p9["lines"]), f"{len(committed_rows)} vs {len(staged_p9['lines'])}")
        line_by_sku = {l["external_sku"]: l for l in staged_p9["lines"]}
        identity_ok, desc_ok, loc_ok, qty_ok = True, True, True, True
        for cr in committed_rows:
            line = line_by_sku.get(cr["sku_code"])
            if line is None:
                identity_ok = False
                continue
            if cr["sku_code"] != line["external_sku"]:
                identity_ok = False
            if cr["product_id"] != line["product_id"] or cr["barcode"] != line["master_barcode"] or cr["product_name"] != line["master_product_name"] or cr["external_sku"] != line["external_sku"]:
                identity_ok = False
            if cr["sku_desc"] != line["master_product_name"]:
                desc_ok = False
            if cr["source_location"] != "Drizzl Demo Warehouse":
                loc_ok = False
            if float(cr["qty"]) != float(line["ordered_qty"]):
                qty_ok = False
        ok &= check("Test9: sku_code/external_sku stayed document-SKU-keyed (join-key compatibility preserved)", identity_ok)
        ok &= check("Test9: product_id/barcode/product_name additive fields correct", identity_ok)
        ok &= check("Test9: sku_desc prefers Master Product name", desc_ok)
        ok &= check("Test9: source_location resolved from the assigned Drizzl warehouse", loc_ok)
        ok &= check("Test9: committed qty = ordered_qty", qty_ok)

        # Canonical commitments must produce exactly one product_id-based
        # synthetic stock row. They must not also leak through the older
        # customer-SKU commitment map (the dashboard duplicate fixed after
        # Phase 12).
        all_committed = reconcile.committed_quantity(conn)
        expected_committed_by_product = {}
        for cr in all_committed:
            if cr["source_location"] != "Drizzl Demo Warehouse" or cr["product_id"] is None:
                continue
            expected_committed_by_product[cr["product_id"]] = expected_committed_by_product.get(cr["product_id"], 0) + float(cr["qty"])

        stock_rows = reconcile.stock_by_location(conn, location="Drizzl Demo Warehouse")
        synthetic_rows = {r["product_id"]: r for r in stock_rows if r["qty_on_hand"] == 0 and r["product_id"] is not None}
        stock_merge_ok = all(
            synthetic_rows.get(product_id) is not None
            and float(synthetic_rows[product_id]["qty_committed"]) == expected_qty
            for product_id, expected_qty in expected_committed_by_product.items()
        )
        ok &= check("Test9: one canonical synthetic commitment row per Master Product", stock_merge_ok, str(synthetic_rows))
        legacy_commitment_map = reconcile.committed_by_location_sku(conn)
        canonical_skus_absent = all(
            ("Drizzl Demo Warehouse", cr["sku_code"]) not in legacy_commitment_map
            for cr in all_committed if cr["product_id"] is not None
        )
        ok &= check("Test9: canonical commitments do not create duplicate customer-SKU rows", canonical_skus_absent, str(legacy_commitment_map))

        # -----------------------------------------------------------------
        print("\n--- Test 10: posting never touches inventory_movements/grn_receipts ---")
        ok &= check("Test10: inventory_movements untouched by all posting above", table_count(conn, "inventory_movements") == baseline_movements)
        ok &= check("Test10: grn_receipts untouched by all posting above", table_count(conn, "grn_receipts") == baseline_grns)

        # -----------------------------------------------------------------
        print("\n--- Test 11: legacy PDF-style line (no product_id) still resolves via item_code/item_desc ---")
        conn.execute(
            "INSERT INTO purchase_orders (po_number, customer_id, source_location_id) VALUES (?, ?, ?)",
            ("LEGACY-PO-0001", scootsy_id, bangalore_id),
        )
        conn.execute(
            "INSERT INTO products (sku_code, sku_desc) VALUES (?, ?)",
            ("LEGACYSKU1", "Drizzl Test Legacy | Something"),
        )
        conn.execute(
            "INSERT INTO po_line_items (po_number, item_code, item_desc, qty) VALUES (?, ?, ?, ?)",
            ("LEGACY-PO-0001", "LEGACYSKU1", "Drizzl Test Legacy | Something", 7),
        )
        conn.commit()
        legacy_row = next((r for r in reconcile.committed_quantity(conn) if r["po_number"] == "LEGACY-PO-0001"), None)
        ok &= check("Test11: legacy line still appears in committed_quantity()", legacy_row is not None)
        if legacy_row:
            ok &= check("Test11: legacy sku_code = item_code", legacy_row["sku_code"] == "LEGACYSKU1")
            ok &= check("Test11: legacy sku_desc falls back to item_desc", legacy_row["sku_desc"] == "Drizzl Test Legacy | Something")
            ok &= check("Test11: legacy line has no canonical identity", legacy_row["product_id"] is None and legacy_row["barcode"] is None and legacy_row["external_sku"] is None)
        flavor_rows_legacy = reconcile.po_quantity_by_flavor(conn)
        legacy_flavor = next((f for f in flavor_rows_legacy if f["flavor"] == "Test Legacy"), None)
        ok &= check("Test11: legacy flavor grouping still works (products.sku_desc fallback)", legacy_flavor is not None, str(flavor_rows_legacy))

        # -----------------------------------------------------------------
        print("\n--- Test 12: po_quantity_by_flavor() no longer shows 'Unknown' for posted canonical POs ---")
        p0_line = staging.get_staged_po(conn, p0["staged_po_id"])["lines"][0]
        expected_fragment = (p0_line["master_product_name"] or "").replace("Drizzl", "").strip()
        flavor_rows = reconcile.po_quantity_by_flavor(conn)
        canonical_flavor = next((f for f in flavor_rows if f["flavor"] == expected_fragment), None)
        ok &= check(f"Test12: flavor {expected_fragment!r} present (not 'Unknown')", canonical_flavor is not None, str(flavor_rows))
        unknown_entry = next((f for f in flavor_rows if f["flavor"] == "Unknown"), None)
        ok &= check("Test12: no 'Unknown' flavor bucket from posted canonical lines", unknown_entry is None or unknown_entry["total_qty"] == 0, str(unknown_entry))

    finally:
        conn.close()
        print(f"\nDropping throwaway database {TEST_DB_NAME}...")
        drop_test_database()

    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

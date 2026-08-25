"""
Verifies Phase 8: official GRN posting, canonical SALE inventory
movements, and full PO commitment release -- grn_posting.py, plus the
dual-identity changes to reconcile.py (stock_by_location(),
current_balance_by_product(), committed_quantity()'s header-level
canonical release branch) and ingest.py (record_movement()/
record_inventory_flag()'s canonical mode).

Runs entirely against a disposable throwaway Postgres database
(drizzl_inventory_test_phase8) -- created/dropped for this run, never the
real drizzl_inventory database. Uses the real Scootsy PO/GRN samples for
the primary real-overlap checks (matching verify_po_posting.py and
verify_grn_csv_staging.py's precedent), plus small synthetic PO/GRN CSV
fixtures for the controlled scenarios the real file doesn't cover
(negative inventory, absent product, over-receipt, multi-GRN atomicity,
active-GRN-for-PO conflict).
"""
import csv
import sys
import tempfile
from pathlib import Path

import psycopg2

import db as db_module
import discrepancy_csv_staging
import grn_csv_staging as staging
import grn_posting
import ingest
import po_csv_staging
import po_posting
import reconcile

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "synthetic"
PO_FIXTURE = FIXTURE_DIR / "demo_po_01.csv"
GRN_FIXTURE = FIXTURE_DIR / "demo_grn_01.csv"
SCOOTSY_NAME = "Scootsy Logistics Private Limited"
TEST_DB_NAME = "drizzl_inventory_test_phase8"

GRN_HEADER = [
    "GrnNumber", "PurchaseOrderNumber", "FacilityName", "SupplierCode", "VendorName",
    "InvoiceNumber", "InvoiceDate", "CreatedAtDate", "DnNumber", "DNQuantity", "DNValue",
    "SkuCode", "SkuDescription", "BrandName", "Category", "ReceivedQty",
    "GrnLineValueWithoutTax", "GrnLineValueWithTax", "LotMrp", "LotExpiryDate",
    "CgstRate", "CgstAmount", "SgstRate", "SgstAmount", "IgstRate", "IgstAmount",
    "CessRate", "CessAmount", "AdditionalCess", "TotalTax", "TotalAmount",
]
DISCREPANCY_HEADER = [
    "PrNumber", "PoNumber", "GrnNumber", "SkuCode", "AcceptedQty",
    "TotalRejectedQty", "RejectedReasons",
]
GRN_BASE_ROW = {
    "GrnNumber": "SYNGRN0001", "PurchaseOrderNumber": "SYNPO0001", "FacilityName": "DEMO FACILITY B",
    "SupplierCode": "DEMO-SUPPLIER-001", "VendorName": "DRIZZL DEMO VENDOR",
    "InvoiceNumber": "SYN-INV-POSTING", "InvoiceDate": "2026-07-31", "CreatedAtDate": "2026-08-13 17:25:12",
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


def normalization_rows():
    return [
        grow(GrnNumber="SYN-GRN-NORM", PurchaseOrderNumber="SYN-PO-NORM", FacilityName="Synthetic Normalization Facility", DnNumber="SYN-DN-NORM", DNQuantity="0", DNValue="0", ReceivedQty="10"),
        grow(GrnNumber="SYN-GRN-NORM", PurchaseOrderNumber="SYN-PO-NORM", FacilityName="Synthetic Normalization Facility", DnNumber="SYN-DN-NORM", DNQuantity="3", DNValue="180", ReceivedQty="10"),
        grow(GrnNumber="SYN-GRN-NORM", PurchaseOrderNumber="SYN-PO-NORM", FacilityName="Synthetic Normalization Facility", DnNumber="SYN-DN-NORM", SkuCode="DEMO-SKU-002", ReceivedQty="48", LotExpiryDate="2027-06-01"),
        grow(GrnNumber="SYN-GRN-NORM", PurchaseOrderNumber="SYN-PO-NORM", FacilityName="Synthetic Normalization Facility", DnNumber="SYN-DN-NORM", SkuCode="DEMO-SKU-002", ReceivedQty="24", LotExpiryDate="2027-05-01"),
    ]


def check(label, condition, detail=""):
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


def stage_and_verify(conn, rows, customer_id, filename):
    """Stages a synthetic GRN CSV, revalidates its batch, returns
    (batch_id, {external_grn_number: staged_grn dict})."""
    csv_path = write_csv(rows)
    result = staging.stage_grn_csv(conn, csv_path, customer_id, filename=filename)
    conn.commit()
    batch_id = result["batch_id"]
    staging.revalidate_grn_batch(conn, batch_id)
    conn.commit()
    grns = {g["external_grn_number"]: g for g in staging.list_staged_grns(conn, batch_id)}
    return batch_id, grns


def opening_balance(conn, product_id, location_id, qty):
    ingest.record_movement(
        conn, movement_date="2026-08-01", sku_code=None, movement_type="opening_balance",
        quantity=qty, location_to=conn.execute("SELECT name FROM locations WHERE id = ?", (location_id,)).fetchone()["name"],
        product_id=product_id,
    )


def run():
    for path in (PO_FIXTURE, GRN_FIXTURE):
        if not path.exists():
            print(f"FAIL -- synthetic fixture not found: {path}")
            return False

    print(f"Creating throwaway database {TEST_DB_NAME}...")
    create_test_database()
    conn = get_test_connection()
    ok = True

    try:
        scootsy_id = get_customer_id(conn, SCOOTSY_NAME)
        bangalore_id = get_location_id(conn, "Drizzl Demo Warehouse")
        p_sku_001 = product_id_for_sku(conn, "DEMO-SKU-001")
        p_sku_005 = product_id_for_sku(conn, "DEMO-SKU-005")
        p_sku_002 = product_id_for_sku(conn, "DEMO-SKU-002")
        p_sku_006 = product_id_for_sku(conn, "DEMO-SKU-006")
        p_sku_003 = product_id_for_sku(conn, "DEMO-SKU-003")
        p_sku_004 = product_id_for_sku(conn, "DEMO-SKU-004")

        baseline_grn_receipts = table_count(conn, "grn_receipts")
        baseline_debit = table_count(conn, "debit_notes")

        # -----------------------------------------------------------------
        print("\n--- Setup: public SYN-PO-1001 staged+posted, opening stock, public GRN staged ---")
        po_result = po_csv_staging.stage_po_csv(conn, PO_FIXTURE, customer_id=scootsy_id, filename=PO_FIXTURE.name)
        conn.commit()
        po_batch_id = po_result["batch_id"]
        all_staged_pos = po_csv_staging.list_staged_pos(conn, po_batch_id)
        fixture_po = next(p for p in all_staged_pos if p["external_po_number"] == "SYN-PO-1001")
        po_csv_staging.assign_source_location(conn, po_batch_id, [fixture_po["staged_po_id"]], bangalore_id)
        conn.commit()
        post_result = po_posting.post_staged_purchase_orders(conn, po_batch_id, [fixture_po["staged_po_id"]])
        conn.commit()
        ok &= check("SYN-PO-1001 posted", len(post_result["posted"]) == 1)

        opening_balance(conn, p_sku_001, bangalore_id, 100)
        opening_balance(conn, p_sku_002, bangalore_id, 100)
        conn.commit()
        ok &= check("Passionfruit opening = 100", reconcile.current_balance_by_product(conn, bangalore_id, p_sku_001) == 100)
        ok &= check("Yuzu opening = 100", reconcile.current_balance_by_product(conn, bangalore_id, p_sku_002) == 100)

        grn_result = staging.stage_grn_csv(conn, GRN_FIXTURE, scootsy_id, filename=GRN_FIXTURE.name)
        conn.commit()
        real_batch_id = grn_result["batch_id"]
        staging.revalidate_grn_batch(conn, real_batch_id)
        conn.commit()
        fixture_grn = next(g for g in staging.list_staged_grns(conn, real_batch_id) if g["external_grn_number"] == "SYN-GRN-1001")
        ok &= check("SYN-GRN-1001 verified before posting", fixture_grn["review_status"] == "verified", fixture_grn["review_status"])

        movements_before = table_count(conn, "inventory_movements")
        commitment_before = sum(r["qty"] for r in reconcile.committed_quantity(conn) if r["po_number"] == "SYN-PO-1001")
        ok &= check("commitment before posting = 30", commitment_before == 30, str(commitment_before))

        # -----------------------------------------------------------------
        print("\n--- Post SYN-GRN-1001 ---")
        legacy_products_before = table_count(conn, "products")
        r1 = grn_posting.post_staged_grns(conn, real_batch_id, [fixture_grn["staged_grn_id"]])
        ok &= check("posted list has 1 entry", len(r1["posted"]) == 1, str(r1))
        ok &= check("nothing rejected", not r1["rejected"], str(r1["rejected"]))
        conn.commit()
        grn_id_1 = r1["posted"][0]["grn_id"] if r1["posted"] else None

        official_grn = conn.execute("SELECT * FROM grn_receipts WHERE grn_id = ?", (grn_id_1,)).fetchone() if grn_id_1 else None
        ok &= check("official GRN header created", official_grn is not None)
        if official_grn:
            ok &= check("po_id linked", official_grn["po_id"] == fixture_grn["official_po_id"])
            ok &= check("po_number preserved", official_grn["po_number"] == "SYN-PO-1001")
            ok &= check("source_location_id = Drizzl Demo Warehouse", official_grn["source_location_id"] == bangalore_id)
            ok &= check("source = 'csv'", official_grn["source"] == "csv")

        official_lines = conn.execute("SELECT * FROM grn_line_items WHERE grn_number = 'SYN-GRN-1001'").fetchall()
        ok &= check("2 official GRN lines", len(official_lines) == 2, str(len(official_lines)))
        by_sku = {l["external_sku"]: l for l in official_lines}
        ok &= check("DEMO-SKU-001 line: product_id + qty 18", by_sku.get("DEMO-SKU-001") and by_sku["DEMO-SKU-001"]["product_id"] == p_sku_001 and by_sku["DEMO-SKU-001"]["received_qty"] == 18)
        ok &= check("DEMO-SKU-002 line: product_id + qty 9", by_sku.get("DEMO-SKU-002") and by_sku["DEMO-SKU-002"]["product_id"] == p_sku_002 and by_sku["DEMO-SKU-002"]["received_qty"] == 9)
        ok &= check("item_code mirrors external_sku (DEMO-SKU-001)", by_sku.get("DEMO-SKU-001") and by_sku["DEMO-SKU-001"]["sku_code"] == "DEMO-SKU-001")

        legacy_products_after = table_count(conn, "products")
        ok &= check("no legacy products row created", legacy_products_after == legacy_products_before, f"{legacy_products_before} -> {legacy_products_after}")

        movements = conn.execute("SELECT * FROM inventory_movements WHERE reference_type = 'grn' AND reference_id = 'SYN-GRN-1001'").fetchall()
        ok &= check("2 canonical SALE movements", len(movements) == 2, str(len(movements)))
        mv_by_product = {m["product_id"]: m for m in movements}
        ok &= check("DEMO-SKU-001 movement qty=18, canonical barcode, source warehouse", mv_by_product.get(p_sku_001) and mv_by_product[p_sku_001]["quantity"] == 18 and mv_by_product[p_sku_001]["sku_code"] == "9000000000001" and mv_by_product[p_sku_001]["location_from_id"] == bangalore_id)
        ok &= check("DEMO-SKU-002 movement qty=9, canonical barcode", mv_by_product.get(p_sku_002) and mv_by_product[p_sku_002]["quantity"] == 9 and mv_by_product[p_sku_002]["sku_code"] == "9000000000002")
        ok &= check("movements link source_grn_line_item_id", all(m["source_grn_line_item_id"] is not None for m in movements))
        ok &= check("movement_type = sale", all(m["movement_type"] == "sale" for m in movements))
        ok &= check("no legacy products row created (movement path)", table_count(conn, "products") == legacy_products_before)

        commitment_after = sum(r["qty"] for r in reconcile.committed_quantity(conn) if r["po_number"] == "SYN-PO-1001")
        ok &= check("commitment after posting = 0", commitment_after == 0, str(commitment_after))

        ok &= check("Passionfruit balance = 80 after sale + shortfall", reconcile.current_balance_by_product(conn, bangalore_id, p_sku_001) == 80)
        ok &= check("Yuzu balance = 90 after sale + shortfall", reconcile.current_balance_by_product(conn, bangalore_id, p_sku_002) == 90)

        staged_grn_after = staging.get_staged_grn(conn, fixture_grn["staged_grn_id"])
        ok &= check("staged_grn.posted_grn_id set", staged_grn_after["posted_grn_id"] == grn_id_1)
        ok &= check("staged_grn.posted_at set", staged_grn_after["posted_at"] is not None)
        ok &= check("staged_grn_lines all linked", all(l["posted_grn_line_item_id"] is not None for l in staged_grn_after["lines"]))
        ok &= check("staged_grn review_status = posted", staged_grn_after["review_status"] == "posted")

        # -----------------------------------------------------------------
        print("\n--- Idempotency (posting the same staged GRN twice) ---")
        grn_count_after_1 = table_count(conn, "grn_receipts")
        line_count_after_1 = table_count(conn, "grn_line_items")
        movement_count_after_1 = table_count(conn, "inventory_movements")
        r2 = grn_posting.post_staged_grns(conn, real_batch_id, [fixture_grn["staged_grn_id"]])
        conn.commit()
        ok &= check("second call: already_posted, not posted", len(r2["already_posted"]) == 1 and not r2["posted"], str(r2))
        ok &= check("already_posted references same grn_id", r2["already_posted"][0]["grn_id"] == grn_id_1)
        ok &= check("no duplicate grn_receipts row", table_count(conn, "grn_receipts") == grn_count_after_1)
        ok &= check("no duplicate grn_line_items rows", table_count(conn, "grn_line_items") == line_count_after_1)
        ok &= check("no duplicate inventory_movements", table_count(conn, "inventory_movements") == movement_count_after_1)
        ok &= check("balances unchanged by idempotent re-post", reconcile.current_balance_by_product(conn, bangalore_id, p_sku_001) == 80)

        # -----------------------------------------------------------------
        print("\n--- Posted revalidation lock ---")
        before_po_verif = staged_grn_after["po_verification_status"]
        before_po_verif_errors = staged_grn_after["po_verification_errors"]
        result_single = staging.validate_staged_grn(conn, fixture_grn["staged_grn_id"])
        ok &= check("validate_staged_grn() no-ops on a posted record", result_single["po_verification_status"] == before_po_verif)
        conn.commit()
        staging.revalidate_grn_batch(conn, real_batch_id)
        conn.commit()
        staged_grn_recheck = staging.get_staged_grn(conn, fixture_grn["staged_grn_id"])
        ok &= check("official_po_id unchanged after batch revalidate", staged_grn_recheck["official_po_id"] == staged_grn_after["official_po_id"])
        ok &= check("po_verification_status unchanged after batch revalidate", staged_grn_recheck["po_verification_status"] == before_po_verif)
        ok &= check("still shows POSTED after revalidate attempts", staged_grn_recheck["review_status"] == "posted")

        # -----------------------------------------------------------------
        print("\n--- Duplicate-DN no-double-post + multi-lot posting ---")
        insert_official_po(
            conn, "SYN-PO-NORM", scootsy_id, bangalore_id, "Synthetic Normalization Facility", "DEMO-SUPPLIER-001",
            "DRIZZL DEMO VENDOR",
            [
                (p_sku_001, "DEMO-SKU-001", 10), (p_sku_002, "DEMO-SKU-002", 72),
            ],
        )
        conn.commit()
        opening_balance(conn, p_sku_001, bangalore_id, 1000)
        opening_balance(conn, p_sku_002, bangalore_id, 1000)
        conn.commit()

        norm_path = write_csv(normalization_rows())
        norm_result = staging.stage_grn_csv(conn, norm_path, scootsy_id, filename="synthetic_normalization.csv")
        norm_batch_id = norm_result["batch_id"]
        staging.revalidate_grn_batch(conn, norm_batch_id)
        conn.commit()
        norm_grn = next(g for g in staging.list_staged_grns(conn, norm_batch_id) if g["external_grn_number"] == "SYN-GRN-NORM")
        ok &= check("SYN-GRN-NORM now verified", norm_grn["review_status"] == "verified", str(norm_grn))

        r3 = grn_posting.post_staged_grns(conn, norm_batch_id, [norm_grn["staged_grn_id"]])
        ok &= check("SYN-GRN-NORM posted", len(r3["posted"]) == 1, str(r3))
        conn.commit()

        fc5_lines = conn.execute("SELECT * FROM grn_line_items WHERE grn_number = 'SYN-GRN-NORM'").fetchall()
        fc5_sku_001 = [l for l in fc5_lines if l["external_sku"] == "DEMO-SKU-001"]
        ok &= check("DEMO-SKU-001: exactly 1 official line (duplicate DN collapsed)", len(fc5_sku_001) == 1, str(len(fc5_sku_001)))
        ok &= check("DEMO-SKU-001: received = 10 (not 20)", fc5_sku_001 and fc5_sku_001[0]["received_qty"] == 10)
        fc5_sku_002 = [l for l in fc5_lines if l["external_sku"] == "DEMO-SKU-002"]
        ok &= check("DEMO-SKU-002: exactly 2 official lines (multi-lot preserved)", len(fc5_sku_002) == 2, str(len(fc5_sku_002)))
        ok &= check("DEMO-SKU-002: lot quantities are {48, 24}, not merged", sorted(l["received_qty"] for l in fc5_sku_002) == [24, 48])

        fc5_movements = conn.execute("SELECT * FROM inventory_movements WHERE reference_type = 'grn' AND reference_id = 'SYN-GRN-NORM'").fetchall()
        fc5_sku_001_mv = [m for m in fc5_movements if m["product_id"] == p_sku_001]
        ok &= check("DEMO-SKU-001: exactly 1 SALE movement of 10", len(fc5_sku_001_mv) == 1 and fc5_sku_001_mv[0]["quantity"] == 10)
        fc5_sku_002_mv = [m for m in fc5_movements if m["product_id"] == p_sku_002]
        ok &= check("DEMO-SKU-002: 2 SALE movements totalling 72", len(fc5_sku_002_mv) == 2 and sum(m["quantity"] for m in fc5_sku_002_mv) == 72)

        # -----------------------------------------------------------------
        print("\n--- Partial receipt: full commitment release despite shortfall ---")
        insert_official_po(conn, "PARTIALPO001", scootsy_id, bangalore_id, "TESTFAC", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p_sku_001, "DEMO-SKU-001", 600)])
        conn.commit()
        partial_csv_rows = [grow(GrnNumber="PARTIALGRN001", PurchaseOrderNumber="PARTIALPO001", FacilityName="TESTFAC", ReceivedQty="200")]
        _, partial_grns = stage_and_verify(conn, partial_csv_rows, scootsy_id, "synthetic_partial.csv")
        partial_grn = partial_grns["PARTIALGRN001"]
        ok &= check("partial GRN verified", partial_grn["review_status"] == "verified", str(partial_grn))
        commitment_before_partial = sum(r["qty"] for r in reconcile.committed_quantity(conn) if r["po_number"] == "PARTIALPO001")
        ok &= check("commitment before = 600", commitment_before_partial == 600)
        r4 = grn_posting.post_staged_grns(conn, partial_grn["batch_id"], [partial_grn["staged_grn_id"]])
        ok &= check("partial GRN posted", len(r4["posted"]) == 1, str(r4))
        conn.commit()
        commitment_after_partial = sum(r["qty"] for r in reconcile.committed_quantity(conn) if r["po_number"] == "PARTIALPO001")
        ok &= check("commitment after = 0 (full release despite 400 shortfall)", commitment_after_partial == 0, str(commitment_after_partial))
        partial_mv = conn.execute("SELECT quantity FROM inventory_movements WHERE reference_type='grn' AND reference_id='PARTIALGRN001'").fetchall()
        ok &= check("SALE = 200, not 600 and not 400", len(partial_mv) == 1 and partial_mv[0]["quantity"] == 200)
        partial_loss = conn.execute(
            "SELECT * FROM inventory_movements WHERE reference_type='grn_discrepancy' AND reference_id='PARTIALGRN001'"
        ).fetchall()
        ok &= check(
            "unresolved discrepancy LOSS = 400, so full PO quantity leaves stock",
            len(partial_loss) == 1 and partial_loss[0]["quantity"] == 400
            and partial_loss[0]["source_grn_id"] == r4["posted"][0]["grn_id"],
            str([dict(row) for row in partial_loss]),
        )

        print("\n--- Discrepancy CSV: classify only, never deduct twice ---")
        discrepancy_path = write_csv([{
            "PrNumber": "PR001", "PoNumber": "PARTIALPO001", "GrnNumber": "PARTIALGRN001",
            "SkuCode": "DEMO-SKU-001", "AcceptedQty": "200", "TotalRejectedQty": "400",
            "RejectedReasons": "Damaged",
        }], DISCREPANCY_HEADER)
        staged_discrepancy = discrepancy_csv_staging.stage_csv(
            conn, discrepancy_path, scootsy_id, filename="discrepancy.csv"
        )
        conn.commit()
        _, discrepancy_lines = discrepancy_csv_staging.get_batch(conn, staged_discrepancy["batch_id"])
        ok &= check("matching discrepancy row is ready", discrepancy_lines[0]["review_status"] == "ready")
        movement_before = conn.execute("SELECT quantity FROM inventory_movements WHERE id=?", (partial_loss[0]["id"],)).fetchone()["quantity"]
        classified = discrepancy_csv_staging.classify_ready(conn, staged_discrepancy["batch_id"])
        conn.commit()
        movement_after = conn.execute("SELECT quantity,notes FROM inventory_movements WHERE id=?", (partial_loss[0]["id"],)).fetchone()
        ok &= check("one discrepancy row classified", classified == 1)
        ok &= check("classification records cause without changing quantity", movement_after["quantity"] == movement_before and movement_after["notes"] == "Damaged", str(dict(movement_after)))
        duplicate = discrepancy_csv_staging.stage_csv(conn, discrepancy_path, scootsy_id, filename="again.csv")
        ok &= check("same discrepancy file is idempotent", duplicate["reused"] and duplicate["batch_id"] == staged_discrepancy["batch_id"])

        mismatch_path = write_csv([{
            "PrNumber": "PR002", "PoNumber": "PARTIALPO001", "GrnNumber": "PARTIALGRN001",
            "SkuCode": "DEMO-SKU-001", "AcceptedQty": "201", "TotalRejectedQty": "399",
            "RejectedReasons": "Short",
        }], DISCREPANCY_HEADER)
        mismatch = discrepancy_csv_staging.stage_csv(conn, mismatch_path, scootsy_id, filename="mismatch.csv")
        conn.commit()
        _, mismatch_lines = discrepancy_csv_staging.get_batch(conn, mismatch["batch_id"])
        ok &= check("wrong rejected quantity is blocked", mismatch_lines[0]["review_status"] == "blocked", mismatch_lines[0]["review_message"])

        # -----------------------------------------------------------------
        print("\n--- Product completely absent from GRN: full release for BOTH products ---")
        insert_official_po(conn, "ABSENTPO001", scootsy_id, bangalore_id, "TESTFAC", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p_sku_001, "DEMO-SKU-001", 100), (p_sku_002, "DEMO-SKU-002", 50)])
        conn.commit()
        absent_csv_rows = [grow(GrnNumber="ABSENTGRN001", PurchaseOrderNumber="ABSENTPO001", FacilityName="TESTFAC", ReceivedQty="100")]
        _, absent_grns = stage_and_verify(conn, absent_csv_rows, scootsy_id, "synthetic_absent.csv")
        absent_grn = absent_grns["ABSENTGRN001"]
        ok &= check("absent-product GRN verified", absent_grn["review_status"] == "verified", str(absent_grn))
        r5 = grn_posting.post_staged_grns(conn, absent_grn["batch_id"], [absent_grn["staged_grn_id"]])
        ok &= check("absent-product GRN posted", len(r5["posted"]) == 1, str(r5))
        conn.commit()
        commitment_absent = {r["sku_code"]: r["qty"] for r in reconcile.committed_quantity(conn) if r["po_number"] == "ABSENTPO001"}
        ok &= check("commitment fully closed for BOTH A and B", commitment_absent == {}, str(commitment_absent))
        absent_mv = conn.execute("SELECT product_id, quantity FROM inventory_movements WHERE reference_type='grn' AND reference_id='ABSENTGRN001'").fetchall()
        ok &= check("only 1 SALE movement created (A=100), none for B", len(absent_mv) == 1 and absent_mv[0]["product_id"] == p_sku_001 and absent_mv[0]["quantity"] == 100)
        absent_loss = conn.execute("SELECT product_id,quantity FROM inventory_movements WHERE reference_type='grn_discrepancy' AND reference_id='ABSENTGRN001'").fetchall()
        ok &= check("absent product still leaves stock as a 50-unit discrepancy loss", len(absent_loss) == 1 and absent_loss[0]["product_id"] == p_sku_002 and absent_loss[0]["quantity"] == 50, str(absent_loss))
        comparison_absent = staging.get_grn_po_comparison(conn, absent_grn["staged_grn_id"])
        b_row = next(r for r in comparison_absent if r["external_sku"] == "DEMO-SKU-002")
        ok &= check("PO/GRN comparison still shows B's shortfall = 50 for audit", b_row["computed_discrepancy_qty"] == 50)

        # -----------------------------------------------------------------
        print("\n--- Negative inventory: allowed, flagged, canonical product_id ---")
        neg_loc_id = conn.execute("INSERT INTO locations (name, type) VALUES ('Neg Test Loc', 'own_facility') RETURNING id").fetchone()["id"]
        conn.commit()
        insert_official_po(conn, "NEGPO001", scootsy_id, neg_loc_id, "TESTFAC", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p_sku_003, "DEMO-SKU-003", 200)])
        conn.commit()
        opening_balance(conn, p_sku_003, neg_loc_id, 100)
        conn.commit()
        neg_csv_rows = [grow(GrnNumber="NEGGRN001", PurchaseOrderNumber="NEGPO001", FacilityName="TESTFAC", SkuCode="DEMO-SKU-003", SkuDescription="Drizzl Mixed Berry | Probiotic Soda | 250 ml", ReceivedQty="200")]
        _, neg_grns = stage_and_verify(conn, neg_csv_rows, scootsy_id, "synthetic_neg.csv")
        neg_grn = neg_grns["NEGGRN001"]
        ok &= check("negative-scenario GRN verified", neg_grn["review_status"] == "verified", str(neg_grn))
        flags_before = table_count(conn, "inventory_flags")
        r6 = grn_posting.post_staged_grns(conn, neg_grn["batch_id"], [neg_grn["staged_grn_id"]])
        ok &= check("negative-inventory GRN still posts successfully", len(r6["posted"]) == 1, str(r6))
        conn.commit()
        ok &= check("resulting balance = -100", reconcile.current_balance_by_product(conn, neg_loc_id, p_sku_003) == -100)
        flag = conn.execute("SELECT * FROM inventory_flags WHERE source = 'grn' AND reference_id = 'NEGGRN001'").fetchone()
        ok &= check("inventory_flags row created with product_id set", flag is not None and flag["product_id"] == p_sku_003)
        ok &= check("flag sku_code = master barcode, not a customer SKU", flag is not None and flag["sku_code"] == "9000000000003")
        ok &= check("no negative-inventory override was required (no exception)", table_count(conn, "inventory_flags") == flags_before + 1)

        # -----------------------------------------------------------------
        print("\n--- Multi-GRN atomicity: 3 succeed together ---")
        insert_official_po(conn, "ATOMPO_A", scootsy_id, bangalore_id, "TESTFAC", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p_sku_004, "DEMO-SKU-004", 10)])
        insert_official_po(conn, "ATOMPO_B", scootsy_id, bangalore_id, "TESTFAC", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p_sku_004, "DEMO-SKU-004", 10)])
        insert_official_po(conn, "ATOMPO_C", scootsy_id, bangalore_id, "TESTFAC", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p_sku_004, "DEMO-SKU-004", 10)])
        conn.commit()
        atom_rows = [
            grow(GrnNumber="ATOMGRN_A", PurchaseOrderNumber="ATOMPO_A", FacilityName="TESTFAC", SkuCode="DEMO-SKU-004", SkuDescription="Drizzl Lemon & Mint | Probiotic Soda | 250 ml", ReceivedQty="10"),
            grow(GrnNumber="ATOMGRN_B", PurchaseOrderNumber="ATOMPO_B", FacilityName="TESTFAC", SkuCode="DEMO-SKU-004", SkuDescription="Drizzl Lemon & Mint | Probiotic Soda | 250 ml", ReceivedQty="10"),
            grow(GrnNumber="ATOMGRN_C", PurchaseOrderNumber="ATOMPO_C", FacilityName="TESTFAC", SkuCode="DEMO-SKU-004", SkuDescription="Drizzl Lemon & Mint | Probiotic Soda | 250 ml", ReceivedQty="10"),
        ]
        atom_batch_id, atom_grns = stage_and_verify(conn, atom_rows, scootsy_id, "synthetic_atomic.csv")
        ok &= check("all 3 atomic-test GRNs verified", all(g["review_status"] == "verified" for g in atom_grns.values()), str(atom_grns))
        ids3 = [atom_grns[n]["staged_grn_id"] for n in ("ATOMGRN_A", "ATOMGRN_B", "ATOMGRN_C")]
        grn_before = table_count(conn, "grn_receipts")
        r7 = grn_posting.post_staged_grns(conn, atom_batch_id, ids3)
        ok &= check("all 3 posted together", len(r7["posted"]) == 3, str(r7))
        conn.commit()
        ok &= check("grn_receipts grew by exactly 3", table_count(conn, "grn_receipts") - grn_before == 3)

        # -----------------------------------------------------------------
        print("\n--- Multi-GRN atomicity: controlled failure -> 0 of 3 post ---")
        insert_official_po(conn, "ATOMPO_D", scootsy_id, bangalore_id, "TESTFAC", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p_sku_004, "DEMO-SKU-004", 10)])
        insert_official_po(conn, "ATOMPO_E", scootsy_id, bangalore_id, "TESTFAC", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p_sku_004, "DEMO-SKU-004", 10)])
        conn.commit()
        fail_rows = [
            grow(GrnNumber="ATOMGRN_D", PurchaseOrderNumber="ATOMPO_D", FacilityName="TESTFAC", SkuCode="DEMO-SKU-004", SkuDescription="Drizzl Lemon & Mint | Probiotic Soda | 250 ml", ReceivedQty="10"),
            grow(GrnNumber="ATOMGRN_E", PurchaseOrderNumber="ATOMPO_E", FacilityName="TESTFAC", SkuCode="DEMO-SKU-004", SkuDescription="Drizzl Lemon & Mint | Probiotic Soda | 250 ml", ReceivedQty="10"),
            # F targets a nonexistent PO -> guaranteed quarantined/unverified
            grow(GrnNumber="ATOMGRN_F", PurchaseOrderNumber="ATOMPO_NOPE", FacilityName="TESTFAC", SkuCode="DEMO-SKU-004", SkuDescription="Drizzl Lemon & Mint | Probiotic Soda | 250 ml", ReceivedQty="10"),
        ]
        fail_batch_id, fail_grns = stage_and_verify(conn, fail_rows, scootsy_id, "synthetic_atomic_fail.csv")
        ok &= check("D, E verified; F quarantined", fail_grns["ATOMGRN_D"]["review_status"] == "verified" and fail_grns["ATOMGRN_E"]["review_status"] == "verified" and fail_grns["ATOMGRN_F"]["review_status"] == "quarantined")
        ids_fail = [fail_grns[n]["staged_grn_id"] for n in ("ATOMGRN_D", "ATOMGRN_E", "ATOMGRN_F")]
        grn_before_fail = table_count(conn, "grn_receipts")
        movement_before_fail = table_count(conn, "inventory_movements")
        r8 = grn_posting.post_staged_grns(conn, fail_batch_id, ids_fail)
        ok &= check("0 posted when 1 of 3 is not ready", len(r8["posted"]) == 0 and bool(r8["rejected"]), str(r8))
        ok &= check("no writes: grn_receipts unchanged", table_count(conn, "grn_receipts") == grn_before_fail)
        ok &= check("no writes: inventory_movements unchanged", table_count(conn, "inventory_movements") == movement_before_fail)
        d_after = staging.get_staged_grn(conn, fail_grns["ATOMGRN_D"]["staged_grn_id"])
        ok &= check("otherwise-ready D stayed unposted", d_after["posted_grn_id"] is None)

        # -----------------------------------------------------------------
        print("\n--- Cross-batch protection ---")
        try:
            grn_posting.post_staged_grns(conn, atom_batch_id, [fail_grns["ATOMGRN_D"]["staged_grn_id"]])
            ok &= check("posting a foreign staged_grn_id under the wrong batch_id raises PostingError", False, "did not raise")
        except grn_posting.PostingError:
            ok &= check("posting a foreign staged_grn_id under the wrong batch_id raises PostingError", True)

        # -----------------------------------------------------------------
        print("\n--- Active-GRN-for-PO conflict ---")
        insert_official_po(conn, "SHAREDPO001", scootsy_id, bangalore_id, "TESTFAC", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p_sku_004, "DEMO-SKU-004", 100)])
        conn.commit()
        shared_rows_1 = [grow(GrnNumber="SHAREDGRN_1", PurchaseOrderNumber="SHAREDPO001", FacilityName="TESTFAC", SkuCode="DEMO-SKU-004", SkuDescription="Drizzl Lemon & Mint | Probiotic Soda | 250 ml", ReceivedQty="40")]
        shared_batch_1, shared_grns_1 = stage_and_verify(conn, shared_rows_1, scootsy_id, "synthetic_shared_1.csv")
        r9 = grn_posting.post_staged_grns(conn, shared_batch_1, [shared_grns_1["SHAREDGRN_1"]["staged_grn_id"]])
        ok &= check("first GRN against SHAREDPO001 posts", len(r9["posted"]) == 1, str(r9))
        conn.commit()

        shared_rows_2 = [grow(GrnNumber="SHAREDGRN_2", PurchaseOrderNumber="SHAREDPO001", FacilityName="TESTFAC", SkuCode="DEMO-SKU-004", SkuDescription="Drizzl Lemon & Mint | Probiotic Soda | 250 ml", ReceivedQty="30")]
        shared_batch_2, shared_grns_2 = stage_and_verify(conn, shared_rows_2, scootsy_id, "synthetic_shared_2.csv")
        po_before_conflict = table_count(conn, "grn_receipts")
        r10 = grn_posting.post_staged_grns(conn, shared_batch_2, [shared_grns_2["SHAREDGRN_2"]["staged_grn_id"]])
        ok &= check("second GRN against same PO is rejected (active_grn_for_po_already_exists)", bool(r10["rejected"]), str(r10))
        ok &= check("nothing written for the conflicting second GRN", table_count(conn, "grn_receipts") == po_before_conflict)

        # -----------------------------------------------------------------
        print("\n--- Canonical stock aggregation across differing identity sources ---")
        # A second canonical movement for the SAME product_id, created via a
        # different code path (manual opening_balance, not a GRN sale) --
        # proves the ledger pools by product_id, not by how the movement
        # was created or which document/SKU originally drove it. Compute the
        # expected total dynamically rather than a hand-derived constant --
        # this test DB has accumulated several unrelated DEMO-SKU-001 movements
        # from the public fixture and controlled normalization scenarios above.
        balance_before_agg_test = reconcile.current_balance_by_product(conn, bangalore_id, p_sku_001)
        opening_balance(conn, p_sku_001, bangalore_id, 50)
        conn.commit()
        ok &= check(
            "current_balance_by_product sums across different movement sources",
            reconcile.current_balance_by_product(conn, bangalore_id, p_sku_001) == balance_before_agg_test + 50,
        )
        expected_total = balance_before_agg_test + 50
        stock_rows = reconcile.stock_by_location(conn, location="Drizzl Demo Warehouse")
        passionfruit_rows = [r for r in stock_rows if r["product_id"] == p_sku_001]
        ok &= check("stock_by_location shows ONE row for this product_id, not split", len(passionfruit_rows) == 1, str(passionfruit_rows))
        ok &= check("that row's qty_on_hand matches the aggregated total", passionfruit_rows and passionfruit_rows[0]["qty_on_hand"] == expected_total, str(passionfruit_rows))
        ok &= check("that row exposes barcode/product_name, not a customer SKU", passionfruit_rows and passionfruit_rows[0]["sku_code"] == "9000000000001" and passionfruit_rows[0]["sku_desc"] == "Drizzl Passionfruit Probiotic Soda")

        # -----------------------------------------------------------------
        print("\n--- Zero legacy products rows created by any canonical GRN posting above ---")
        legacy_products_before_legacy_test = table_count(conn, "products")
        ok &= check("legacy products table unchanged by every canonical posting/movement above", legacy_products_before_legacy_test == legacy_products_before, f"{legacy_products_before} -> {legacy_products_before_legacy_test}")

        print("\n--- Legacy movement fallback (product_id IS NULL) ---")
        ingest.record_movement(
            conn, movement_date="2026-08-01", sku_code="LEGACYSKU1", movement_type="opening_balance",
            quantity=77, location_to="Drizzl Demo Warehouse", sku_desc="Drizzl Legacy Test | Something",
        )
        conn.commit()
        ok &= check("legacy current_balance() still works", reconcile.current_balance(conn, "Drizzl Demo Warehouse", "LEGACYSKU1") == 77)
        legacy_rows = [r for r in reconcile.stock_by_location(conn, location="Drizzl Demo Warehouse") if r["sku_code"] == "LEGACYSKU1"]
        ok &= check("legacy row appears in stock_by_location, product_id NULL", legacy_rows and legacy_rows[0]["product_id"] is None and legacy_rows[0]["qty_on_hand"] == 77)
        ok &= check("legacy row did not merge into any canonical product_id row", not any(r["sku_code"] == "LEGACYSKU1" and r["product_id"] is not None for r in stock_rows))
        ok &= check(
            "this legacy call DID create a legacy products row (expected -- it's the legacy path)",
            table_count(conn, "products") == legacy_products_before_legacy_test + 1,
        )

        # -----------------------------------------------------------------
        print("\n--- Ledger isolation: zero PR/DN side effects ---")
        ok &= check("debit_notes untouched by Phase 8 activity", table_count(conn, "debit_notes") == baseline_debit)

    finally:
        conn.close()
        print(f"\nDropping throwaway database {TEST_DB_NAME}...")
        drop_test_database()

    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

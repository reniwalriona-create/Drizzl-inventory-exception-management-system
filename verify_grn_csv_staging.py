"""
Verifies Phase 6: GRN CSV staging, raw-row preservation, safe
normalization (the inventory-critical duplicate-DN-row and multi-lot
handling), and canonical PO verification -- grn_csv_staging.py.

Runs entirely against a disposable throwaway Postgres database
(drizzl_inventory_test_phase6), created/dropped for this run -- never the
real drizzl_inventory database, since this needs a realistic posted
official PO to verify GRNs against.

Uses repository-local public demo fixtures. The demo GRN is expanded in a
temporary file with controlled duplicate-DN, multi-lot, and missing-PO rows;
the public discrepancy CSV remains an independent read-only oracle.
"""
import csv
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import psycopg2

import catalog
import db as db_module
import grn_csv_staging as staging
import po_csv_staging
import po_posting

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "synthetic"
PO_FIXTURE = FIXTURE_DIR / "demo_po_01.csv"
GRN_FIXTURE = FIXTURE_DIR / "demo_grn_01.csv"
PR_FIXTURE = FIXTURE_DIR / "demo_discrepancy_01.csv"
SCOOTSY_NAME = "Scootsy Logistics Private Limited"
TEST_DB_NAME = "drizzl_inventory_test_phase6"

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
    "InvoiceNumber": "SYN-INV-STAGING", "InvoiceDate": "2026-07-31", "CreatedAtDate": "2026-08-13 17:25:12",
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


# ---------------------------------------------------------------------------
# Throwaway database lifecycle (mirrors verify_po_posting.py)
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


def insert_official_po(conn, po_number, customer_id, source_location_id, destination_facility_name,
                        supplier_code, vendor_name, lines):
    """Direct-SQL fixture for a canonical official PO -- lines is a list of
    (product_id, external_sku, qty)."""
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


def product_id_for_sku(conn, sku):
    r = conn.execute("SELECT product_id FROM master_products WHERE barcode = (SELECT barcode FROM master_products mp JOIN customer_product_skus c ON c.product_id = mp.product_id WHERE c.external_sku = ?)", (sku,)).fetchone()
    return r["product_id"] if r else None


# ---------------------------------------------------------------------------
# PR oracle (read-only, never staged)
# ---------------------------------------------------------------------------

def load_pr_rows():
    with PR_FIXTURE.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run():
    for path in (PO_FIXTURE, GRN_FIXTURE, PR_FIXTURE):
        if not path.exists():
            print(f"FAIL -- synthetic fixture not found: {path}")
            return False
    expanded_grn = expanded_grn_fixture()

    print(f"Creating throwaway database {TEST_DB_NAME}...")
    create_test_database()
    conn = get_test_connection()
    ok = True

    try:
        scootsy_id = get_customer_id(conn, SCOOTSY_NAME)
        bangalore_id = get_location_id(conn, "Drizzl Demo Warehouse")
        ok &= check("Scootsy seeded", scootsy_id is not None)
        ok &= check("Drizzl Demo Warehouse seeded", bangalore_id is not None)

        baseline_grn_receipts = table_count(conn, "grn_receipts")
        baseline_movements = table_count(conn, "inventory_movements")

        # -----------------------------------------------------------------
        print("\n--- Setup: stage + post public SYN-PO-1001 ---")
        po_result = po_csv_staging.stage_po_csv(conn, PO_FIXTURE, customer_id=scootsy_id, filename=PO_FIXTURE.name)
        conn.commit()
        po_batch_id = po_result["batch_id"]
        staged_pos = po_csv_staging.list_staged_pos(conn, po_batch_id)
        fixture_po = next(p for p in staged_pos if p["external_po_number"] == "SYN-PO-1001")
        po_csv_staging.assign_source_location(conn, po_batch_id, [fixture_po["staged_po_id"]], bangalore_id)
        conn.commit()
        post_result = po_posting.post_staged_purchase_orders(conn, po_batch_id, [fixture_po["staged_po_id"]])
        ok &= check("SYN-PO-1001 posts cleanly", len(post_result["posted"]) == 1, str(post_result))
        conn.commit()
        fixture_po_id = post_result["posted"][0]["po_id"]

        # -----------------------------------------------------------------
        print("\n--- Staging public demo GRN plus controlled normalization rows ---")
        grn_result = staging.stage_grn_csv(conn, expanded_grn, customer_id=scootsy_id, filename="demo_grn_staging.csv")
        conn.commit()
        grn_batch_id = grn_result["batch_id"]

        raw_row_count = table_count(conn, "grn_import_rows", "batch_id = ?", (grn_batch_id,))
        staged_grn_count = table_count(conn, "staged_grns", "batch_id = ?", (grn_batch_id,))
        line_count = conn.execute(
            "SELECT COUNT(*) AS n FROM staged_grn_lines l JOIN staged_grns g ON g.staged_grn_id = l.staged_grn_id WHERE g.batch_id = ?",
            (grn_batch_id,),
        ).fetchone()["n"]
        resolved_count = conn.execute(
            "SELECT COUNT(*) AS n FROM staged_grn_lines l JOIN staged_grns g ON g.staged_grn_id = l.staged_grn_id "
            "WHERE g.batch_id = ? AND l.product_id IS NOT NULL",
            (grn_batch_id,),
        ).fetchone()["n"]

        ok &= check("11 raw rows", raw_row_count == 11, f"got {raw_row_count}")
        ok &= check("3 staged GRNs", staged_grn_count == 3, f"got {staged_grn_count}")
        ok &= check("10 normalized staged GRN lines", line_count == 10, f"got {line_count}")
        ok &= check("10/10 lines have product_id resolved", resolved_count == 10, f"got {resolved_count}")
        ok &= check("no legacy products rows created", table_count(conn, "products") == 0)

        def staged_grn_by_number(num):
            row = conn.execute(
                "SELECT * FROM staged_grns WHERE batch_id = ? AND external_grn_number = ?", (grn_batch_id, num)
            ).fetchone()
            return dict(row) if row else None

        def lines_for(num):
            g = staged_grn_by_number(num)
            return staging.get_staged_grn_lines(conn, g["staged_grn_id"])

        # -----------------------------------------------------------------
        print("\n--- Controlled duplicate-DN representation (Case B) ---")
        for grn_num, sku, expected_received, expected_dn in [
            ("SYN-GRN-NORM", "DEMO-SKU-001", 10, 3),
        ]:
            g = staged_grn_by_number(grn_num)
            matching = [l for l in staging.get_staged_grn_lines(conn, g["staged_grn_id"]) if l["external_sku"] == sku]
            ok &= check(f"{grn_num}/{sku}: exactly 1 normalized line", len(matching) == 1, f"got {len(matching)}")
            if matching:
                line = matching[0]
                ok &= check(f"{grn_num}/{sku}: received = {expected_received}", int(line["received_qty"]) == expected_received, str(line["received_qty"]))
                ok &= check(f"{grn_num}/{sku}: dn_quantity = {expected_dn}", int(line["dn_quantity"]) == expected_dn, str(line["dn_quantity"]))
                source_rows = conn.execute(
                    "SELECT raw_row_id FROM staged_grn_line_source_rows WHERE staged_grn_line_id = ?", (line["staged_grn_line_id"],)
                ).fetchall()
                ok &= check(f"{grn_num}/{sku}: 2 raw rows linked", len(source_rows) == 2, f"got {len(source_rows)}")

        # -----------------------------------------------------------------
        print("\n--- Controlled multi-lot case (SYN-GRN-NORM / DEMO-SKU-002) ---")
        g = staged_grn_by_number("SYN-GRN-NORM")
        lot_lines = sorted(
            [l for l in staging.get_staged_grn_lines(conn, g["staged_grn_id"]) if l["external_sku"] == "DEMO-SKU-002"],
            key=lambda l: -l["received_qty"],
        )
        ok &= check("SYN-GRN-NORM/DEMO-SKU-002: 2 normalized lines (not collapsed)", len(lot_lines) == 2, f"got {len(lot_lines)}")
        if len(lot_lines) == 2:
            ok &= check("lot 1 received = 48", int(lot_lines[0]["received_qty"]) == 48)
            ok &= check("lot 1 expiry = 2027-06-01", str(lot_lines[0]["lot_expiry_date"]) == "2027-06-01")
            ok &= check("lot 2 received = 24", int(lot_lines[1]["received_qty"]) == 24)
            ok &= check("lot 2 expiry = 2027-05-01", str(lot_lines[1]["lot_expiry_date"]) == "2027-05-01")
            ok &= check("aggregate received = 72", int(lot_lines[0]["received_qty"] + lot_lines[1]["received_qty"]) == 72)

        # -----------------------------------------------------------------
        print("\n--- PR cross-check (independent oracle, never staged) ---")
        pr_rows = load_pr_rows()
        pr_expected = [
            ("SYN-GRN-1001", "DEMO-SKU-001", 18, 2),
            ("SYN-GRN-1001", "DEMO-SKU-002", 9, 1),
        ]
        ok &= check("public discrepancy oracle has 2 relevant rows", len(pr_rows) == 2, f"got {len(pr_rows)}")
        pr_by_key = {(r["GrnNumber"], r["SkuCode"]): r for r in pr_rows}
        for grn_num, sku, accepted, rejected in pr_expected:
            pr = pr_by_key.get((grn_num, sku))
            if pr is None:
                ok &= check(f"PR row present for {grn_num}/{sku}", False)
                continue
            g = staged_grn_by_number(grn_num)
            matching = [l for l in staging.get_staged_grn_lines(conn, g["staged_grn_id"]) if l["external_sku"] == sku]
            total_received = sum(int(l["received_qty"]) for l in matching)
            total_dn = sum(int(l["dn_quantity"] or 0) for l in matching)
            ok &= check(
                f"{grn_num}/{sku}: normalized received ({total_received}) == PR AcceptedQty ({pr['AcceptedQty']})",
                total_received == int(pr["AcceptedQty"]),
            )
            ok &= check(
                f"{grn_num}/{sku}: normalized dn_qty ({total_dn}) == PR TotalRejectedQty ({pr['TotalRejectedQty']})",
                total_dn == int(pr["TotalRejectedQty"]),
            )

        # -----------------------------------------------------------------
        print("\n--- PO verification: public fixture overlap ---")
        results = staging.revalidate_grn_batch(conn, grn_batch_id)
        conn.commit()
        fixture_grn = staged_grn_by_number("SYN-GRN-1001")
        ok &= check("SYN-GRN-1001 resolves SYN-PO-1001's po_id", fixture_grn["official_po_id"] == fixture_po_id)
        ok &= check("SYN-GRN-1001 po_verification_status = verified", fixture_grn["po_verification_status"] == "verified", str(fixture_grn["po_verification_errors"]))

        comparison = staging.get_grn_po_comparison(conn, fixture_grn["staged_grn_id"])
        comp_by_sku = {r["external_sku"]: r for r in comparison}
        ok &= check("DEMO-SKU-001: ordered 20, received 18, discrepancy 2", comp_by_sku["DEMO-SKU-001"]["ordered_qty"] == 20 and comp_by_sku["DEMO-SKU-001"]["received_qty"] == 18 and comp_by_sku["DEMO-SKU-001"]["computed_discrepancy_qty"] == 2)
        ok &= check("DEMO-SKU-002: ordered 10, received 9, discrepancy 1", comp_by_sku["DEMO-SKU-002"]["ordered_qty"] == 10 and comp_by_sku["DEMO-SKU-002"]["received_qty"] == 9 and comp_by_sku["DEMO-SKU-002"]["computed_discrepancy_qty"] == 1)

        # -----------------------------------------------------------------
        print("\n--- Expected quarantine: two GRNs with missing official POs ---")
        other_grns = [r for r in staging.list_staged_grns(conn, grn_batch_id) if r["external_grn_number"] != "SYN-GRN-1001"]
        ok &= check("2 other staged GRNs", len(other_grns) == 2, f"got {len(other_grns)}")
        all_blocked_missing_po = all(
            r["po_verification_status"] == "blocked" and
            any(e["code"] == "official_po_not_found" for e in r["po_verification_errors"])
            for r in other_grns
        )
        ok &= check("both quarantined with official_po_not_found", all_blocked_missing_po)
        ok &= check("batch itself was not rejected wholesale", staged_grn_count == 3)

        # -----------------------------------------------------------------
        print("\n--- Revalidation after a previously-missing PO arrives ---")
        chm_sku = "DEMO-SKU-003"
        chm_product_id = product_id_for_sku(conn, chm_sku)
        insert_official_po(
            conn, "SYN-PO-LATE", scootsy_id, bangalore_id, "Synthetic Late Facility", "DEMO-SUPPLIER-001",
            "DRIZZL DEMO VENDOR",
            [
                (chm_product_id, chm_sku, 72),
                (product_id_for_sku(conn, "DEMO-SKU-001"), "DEMO-SKU-001", 240),
                (product_id_for_sku(conn, "DEMO-SKU-002"), "DEMO-SKU-002", 48),
                (product_id_for_sku(conn, "DEMO-SKU-005"), "DEMO-SKU-005", 144),
                (product_id_for_sku(conn, "DEMO-SKU-004"), "DEMO-SKU-004", 96),
            ],
        )
        conn.commit()
        chm_grn_before = staged_grn_by_number("SYN-GRN-LATE")
        ok &= check("SYN-GRN-LATE was blocked/missing-PO before", chm_grn_before["po_verification_status"] == "blocked")
        staging.revalidate_grn_batch(conn, grn_batch_id)
        conn.commit()
        chm_grn_after = staged_grn_by_number("SYN-GRN-LATE")
        ok &= check("SYN-GRN-LATE missing-PO error is gone", not any(e["code"] == "official_po_not_found" for e in chm_grn_after["po_verification_errors"]))
        ok &= check("SYN-GRN-LATE now verified", chm_grn_after["po_verification_status"] == "verified", str(chm_grn_after["po_verification_errors"]))
        ok &= check("SYN-GRN-LATE official_po_id populated", chm_grn_after["official_po_id"] is not None)
        ok &= check("Raw/normalization data untouched by revalidation (still 10 total lines)",
                     conn.execute("SELECT COUNT(*) AS n FROM staged_grn_lines l JOIN staged_grns g ON g.staged_grn_id=l.staged_grn_id WHERE g.batch_id=?", (grn_batch_id,)).fetchone()["n"] == 10)

        # -----------------------------------------------------------------
        print("\n--- Destination mismatch (controlled) ---")
        mismatch_csv = write_csv([grow(GrnNumber="SYNGRN-DEST", PurchaseOrderNumber="SYNPO-DEST", FacilityName="WRONG FACILITY")])
        p1 = product_id_for_sku(conn, "DEMO-SKU-001")
        insert_official_po(conn, "SYNPO-DEST", scootsy_id, bangalore_id, "RIGHT FACILITY", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p1, "DEMO-SKU-001", 10)])
        conn.commit()
        r = staging.stage_grn_csv(conn, mismatch_csv, customer_id=scootsy_id, filename="synthetic_dest.csv")
        conn.commit()
        dest_grn = staging.list_staged_grns(conn, r["batch_id"])[0]
        result = staging.validate_staged_grn(conn, dest_grn["staged_grn_id"])
        conn.commit()
        ok &= check("destination mismatch blocks verification", result["po_verification_status"] == "blocked" and any(e["code"] == "destination_facility_mismatch" for e in result["po_verification_errors"]), str(result))

        # -----------------------------------------------------------------
        print("\n--- Product/SKU mismatch scenarios (controlled) ---")
        # Unknown customer SKU
        unknown_csv = write_csv([grow(GrnNumber="SYNGRN-UNK", PurchaseOrderNumber="SYNPO0001", SkuCode="999999", SkuDescription="Unknown")])
        r = staging.stage_grn_csv(conn, unknown_csv, customer_id=scootsy_id, filename="synthetic_unknown.csv")
        conn.commit()
        unk_grn = staging.list_staged_grns(conn, r["batch_id"])[0]
        unk_lines = staging.get_staged_grn_lines(conn, unk_grn["staged_grn_id"])
        ok &= check("unknown SKU: raw row retained, product_id NULL, line blocked", unk_lines[0]["product_id"] is None and unk_lines[0]["validation_status"] == "blocked")
        ok &= check("unknown SKU: parent GRN blocked", unk_grn["validation_status"] == "blocked")

        # Known Master Product but SKU not present on official PO
        p2 = product_id_for_sku(conn, "DEMO-SKU-003")
        insert_official_po(conn, "SYNPO-NOSKU", scootsy_id, bangalore_id, "DEMO FACILITY B", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p2, "999888", 10)])
        conn.commit()
        nosku_csv = write_csv([grow(GrnNumber="SYNGRN-NOSKU", PurchaseOrderNumber="SYNPO-NOSKU", SkuCode="DEMO-SKU-003", SkuDescription="Drizzl Mixed Berry")])
        r = staging.stage_grn_csv(conn, nosku_csv, customer_id=scootsy_id, filename="synthetic_nosku.csv")
        conn.commit()
        nosku_grn = staging.list_staged_grns(conn, r["batch_id"])[0]
        result = staging.validate_staged_grn(conn, nosku_grn["staged_grn_id"])
        conn.commit()
        ok &= check("known product, SKU not on PO -> grn_sku_not_on_po, blocked", result["po_verification_status"] == "blocked" and any(e["code"] == "grn_sku_not_on_po" for e in result["po_verification_errors"]), str(result))

        # Product not represented on official PO at all
        p3 = product_id_for_sku(conn, "DEMO-SKU-004")
        insert_official_po(conn, "SYNPO-NOPROD", scootsy_id, bangalore_id, "DEMO FACILITY B", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p1, "DEMO-SKU-001", 10)])
        conn.commit()
        noprod_csv = write_csv([grow(GrnNumber="SYNGRN-NOPROD", PurchaseOrderNumber="SYNPO-NOPROD", SkuCode="DEMO-SKU-004", SkuDescription="Drizzl Lemon & Mint")])
        r = staging.stage_grn_csv(conn, noprod_csv, customer_id=scootsy_id, filename="synthetic_noprod.csv")
        conn.commit()
        noprod_grn = staging.list_staged_grns(conn, r["batch_id"])[0]
        result = staging.validate_staged_grn(conn, noprod_grn["staged_grn_id"])
        conn.commit()
        ok &= check("product not on PO -> grn_product_not_on_po, blocked", result["po_verification_status"] == "blocked" and any(e["code"] == "grn_product_not_on_po" for e in result["po_verification_errors"]), str(result))

        # Legacy PO line with no product_id
        conn.execute("INSERT INTO purchase_orders (po_number, customer_id, source_location_id, destination_facility_name, facility_name, supplier_code, vendor_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("SYNPO-LEGACY", scootsy_id, bangalore_id, "DEMO FACILITY B", "DEMO FACILITY B", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR"))
        conn.execute("INSERT INTO po_line_items (po_number, item_code, qty) VALUES (?, ?, ?)", ("SYNPO-LEGACY", "DEMO-SKU-001", 10))
        conn.commit()
        legacy_csv = write_csv([grow(GrnNumber="SYNGRN-LEGACY", PurchaseOrderNumber="SYNPO-LEGACY")])
        r = staging.stage_grn_csv(conn, legacy_csv, customer_id=scootsy_id, filename="synthetic_legacy.csv")
        conn.commit()
        legacy_grn = staging.list_staged_grns(conn, r["batch_id"])[0]
        result = staging.validate_staged_grn(conn, legacy_grn["staged_grn_id"])
        conn.commit()
        ok &= check("legacy PO line missing product_id -> blocked, not guessed", result["po_verification_status"] == "blocked" and any(e["code"] == "legacy_po_line_missing_product_identity" for e in result["po_verification_errors"]), str(result))

        # -----------------------------------------------------------------
        print("\n--- Quantity behavior (controlled) ---")
        # Exact receipt
        insert_official_po(conn, "SYNPO-EXACT", scootsy_id, bangalore_id, "DEMO FACILITY B", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p1, "DEMO-SKU-001", 100)])
        conn.commit()
        exact_csv = write_csv([grow(GrnNumber="SYNGRN-EXACT", PurchaseOrderNumber="SYNPO-EXACT", ReceivedQty="100")])
        r = staging.stage_grn_csv(conn, exact_csv, customer_id=scootsy_id, filename="synthetic_exact.csv")
        conn.commit()
        exact_grn = staging.list_staged_grns(conn, r["batch_id"])[0]
        result = staging.validate_staged_grn(conn, exact_grn["staged_grn_id"])
        conn.commit()
        ok &= check("exact receipt (100/100): verified", result["po_verification_status"] == "verified", str(result))

        # Partial receipt (600 ordered, 200 received) -- still valid, no ledger effect
        insert_official_po(conn, "SYNPO-PARTIAL", scootsy_id, bangalore_id, "DEMO FACILITY B", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p1, "DEMO-SKU-001", 600)])
        conn.commit()
        partial_csv = write_csv([grow(GrnNumber="SYNGRN-PARTIAL", PurchaseOrderNumber="SYNPO-PARTIAL", ReceivedQty="200")])
        r = staging.stage_grn_csv(conn, partial_csv, customer_id=scootsy_id, filename="synthetic_partial.csv")
        conn.commit()
        partial_grn = staging.list_staged_grns(conn, r["batch_id"])[0]
        result = staging.validate_staged_grn(conn, partial_grn["staged_grn_id"])
        conn.commit()
        comparison = staging.get_grn_po_comparison(conn, partial_grn["staged_grn_id"])
        row_sku_001 = next(row for row in comparison if row["external_sku"] == "DEMO-SKU-001")
        ok &= check("partial receipt (600 ordered/200 received): still verified", result["po_verification_status"] == "verified", str(result))
        ok &= check("partial receipt: computed_discrepancy_qty = 400", row_sku_001["computed_discrepancy_qty"] == 400)

        # Product entirely absent from GRN -- ordered 50, GRN has 0 lines for it
        p_absent = product_id_for_sku(conn, "DEMO-SKU-002")
        insert_official_po(conn, "SYNPO-ABSENT", scootsy_id, bangalore_id, "DEMO FACILITY B", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p1, "DEMO-SKU-001", 20), (p_absent, "DEMO-SKU-002", 50)])
        conn.commit()
        absent_csv = write_csv([grow(GrnNumber="SYNGRN-ABSENT", PurchaseOrderNumber="SYNPO-ABSENT", ReceivedQty="20")])
        r = staging.stage_grn_csv(conn, absent_csv, customer_id=scootsy_id, filename="synthetic_absent.csv")
        conn.commit()
        absent_grn = staging.list_staged_grns(conn, r["batch_id"])[0]
        staging.validate_staged_grn(conn, absent_grn["staged_grn_id"])
        conn.commit()
        comparison = staging.get_grn_po_comparison(conn, absent_grn["staged_grn_id"])
        row_sku_002 = next(row for row in comparison if row["external_sku"] == "DEMO-SKU-002")
        ok &= check("product absent from GRN: received_qty = 0", row_sku_002["received_qty"] == 0)
        ok &= check("product absent from GRN: computed_discrepancy_qty = 50", row_sku_002["computed_discrepancy_qty"] == 50)

        # Over-receipt
        insert_official_po(conn, "SYNPO-OVER", scootsy_id, bangalore_id, "DEMO FACILITY B", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p1, "DEMO-SKU-001", 100)])
        conn.commit()
        over_csv = write_csv([grow(GrnNumber="SYNGRN-OVER", PurchaseOrderNumber="SYNPO-OVER", ReceivedQty="101")])
        r = staging.stage_grn_csv(conn, over_csv, customer_id=scootsy_id, filename="synthetic_over.csv")
        conn.commit()
        over_grn = staging.list_staged_grns(conn, r["batch_id"])[0]
        result = staging.validate_staged_grn(conn, over_grn["staged_grn_id"])
        conn.commit()
        ok &= check("over-receipt (101/100): blocked, received_quantity_exceeds_ordered", result["po_verification_status"] == "blocked" and any(e["code"] == "received_quantity_exceeds_ordered" for e in result["po_verification_errors"]), str(result))

        # -----------------------------------------------------------------
        print("\n--- Exact-file idempotency ---")
        before_rows = table_count(conn, "grn_import_rows")
        before_grns = table_count(conn, "staged_grns")
        r2 = staging.stage_grn_csv(conn, expanded_grn, customer_id=scootsy_id, filename="demo_grn_staging.csv")
        conn.commit()
        ok &= check("re-staging identical file reuses the batch", r2["batch_id"] == grn_batch_id and r2["reused_existing_batch"])
        ok &= check("no new raw rows", table_count(conn, "grn_import_rows") == before_rows)
        ok &= check("no new staged GRNs", table_count(conn, "staged_grns") == before_grns)

        # -----------------------------------------------------------------
        print("\n--- Different-file, same-GRN-number conflict ---")
        dup_grn_csv = write_csv([grow(GrnNumber="SYN-GRN-1001", PurchaseOrderNumber="SYN-PO-1001", ReceivedQty="19")])
        r3 = staging.stage_grn_csv(conn, dup_grn_csv, customer_id=scootsy_id, filename="synthetic_duplicate_grn.csv")
        conn.commit()
        ok &= check("different bytes, same GRN number -> new batch, not reused", not r3["reused_existing_batch"] and r3["batch_id"] != grn_batch_id)
        dup_staged = staging.list_staged_grns(conn, r3["batch_id"])[0]
        result = staging.validate_staged_grn(conn, dup_staged["staged_grn_id"])
        conn.commit()
        ok &= check("duplicate conflict surfaced, not verified", result["po_verification_status"] == "blocked" and any(e["code"] == "duplicate_grn_in_other_batch" for e in result["po_verification_errors"]), str(result))
        original_still_intact = staged_grn_by_number("SYN-GRN-1001")
        ok &= check("original staged GRN untouched by the conflicting duplicate", original_still_intact["staged_grn_id"] == fixture_grn["staged_grn_id"])

        # -----------------------------------------------------------------
        print("\n--- Source is never guessed ---")
        po_dest_mumbai_source_blr = insert_official_po(conn, "SYNPO-MUMBAI", scootsy_id, bangalore_id, "Mumbai", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p1, "DEMO-SKU-001", 10)])
        conn.commit()
        mumbai_csv = write_csv([grow(GrnNumber="SYNGRN-MUMBAI", PurchaseOrderNumber="SYNPO-MUMBAI", FacilityName="Mumbai")])
        r = staging.stage_grn_csv(conn, mumbai_csv, customer_id=scootsy_id, filename="synthetic_mumbai.csv")
        conn.commit()
        mumbai_grn = staging.list_staged_grns(conn, r["batch_id"])[0]
        staging.validate_staged_grn(conn, mumbai_grn["staged_grn_id"])
        conn.commit()
        official_po_source = conn.execute("SELECT source_location_id FROM purchase_orders WHERE po_id = ?", (po_dest_mumbai_source_blr,)).fetchone()["source_location_id"]
        ok &= check("GRN's future source resolves through the PO's source_location_id (Bangalore), never the Mumbai facility", official_po_source == bangalore_id)
        ok &= check("staged_grns has no source-location column at all", "source_location_id" not in dict(conn.execute("SELECT * FROM staged_grns LIMIT 1").fetchone()).keys())

        # PO with source_location_id NULL
        p_null_src = insert_official_po(conn, "SYNPO-NOSRC", scootsy_id, None, "DEMO FACILITY B", "DEMO-SUPPLIER-001", "DRIZZL DEMO VENDOR", [(p1, "DEMO-SKU-001", 10)])
        conn.commit()
        nosrc_csv = write_csv([grow(GrnNumber="SYNGRN-NOSRC", PurchaseOrderNumber="SYNPO-NOSRC")])
        r = staging.stage_grn_csv(conn, nosrc_csv, customer_id=scootsy_id, filename="synthetic_nosrc.csv")
        conn.commit()
        nosrc_grn = staging.list_staged_grns(conn, r["batch_id"])[0]
        result = staging.validate_staged_grn(conn, nosrc_grn["staged_grn_id"])
        conn.commit()
        ok &= check("PO with no source_location_id -> official_po_source_missing, blocked, no default", result["po_verification_status"] == "blocked" and any(e["code"] == "official_po_source_missing" for e in result["po_verification_errors"]), str(result))

        # -----------------------------------------------------------------
        print("\n--- Zero ledger effect throughout ---")
        ok &= check("grn_receipts untouched", table_count(conn, "grn_receipts") == baseline_grn_receipts)
        ok &= check("inventory_movements untouched", table_count(conn, "inventory_movements") == baseline_movements)
        ok &= check("no legacy products rows (final check)", table_count(conn, "products") == 0)

    finally:
        conn.close()
        print(f"\nDropping throwaway database {TEST_DB_NAME}...")
        drop_test_database()

    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

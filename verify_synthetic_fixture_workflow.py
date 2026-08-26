"""End-to-end verification for the public synthetic portfolio fixtures.

Runs both PO -> GRN -> discrepancy chains through the production staging and
posting services in a disposable PostgreSQL database. No development or
production database is read or changed.
"""
from decimal import Decimal
from pathlib import Path

import activity_log
import discrepancy_csv_staging
import grn_csv_staging
import grn_posting
import ingest
import po_csv_staging
import po_posting
import reconcile
from verify_db import bootstrap_connection, create_database, drop_database


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "synthetic"
TEST_DB_NAME = "drizzl_inventory_test_synthetic_fixtures"
CUSTOMER_NAME = "Scootsy Logistics Private Limited"
SOURCE_LOCATION = "Drizzl Demo Warehouse"

CHAINS = (
    {
        "number": "01", "po": "SYN-PO-1001", "grn": "SYN-GRN-1001", "pr": "SYN-PR-1001",
        "skus": {
            "DEMO-SKU-001": (Decimal("48"), Decimal("46"), Decimal("2"), Decimal("120.00"), "Damaged"),
            "DEMO-SKU-002": (Decimal("36"), Decimal("35"), Decimal("1"), Decimal("60.00"), "Damaged"),
            "DEMO-SKU-003": (Decimal("60"), Decimal("57"), Decimal("3"), Decimal("150.00"), "Expired"),
            "DEMO-SKU-004": (Decimal("42"), Decimal("40"), Decimal("2"), Decimal("100.00"), "Packaging damage"),
            "DEMO-SKU-005": (Decimal("30"), Decimal("29"), Decimal("1"), Decimal("55.00"), "Expired"),
            "DEMO-SKU-006": (Decimal("54"), Decimal("51"), Decimal("3"), Decimal("135.00"), "Packaging damage"),
        },
    },
    {
        "number": "02", "po": "SYN-PO-1002", "grn": "SYN-GRN-1002", "pr": "SYN-PR-1002",
        "skus": {
            "DEMO-SKU-001": (Decimal("72"), Decimal("68"), Decimal("4"), Decimal("240.00"), "Short delivery"),
            "DEMO-SKU-002": (Decimal("48"), Decimal("46"), Decimal("2"), Decimal("120.00"), "Short delivery"),
            "DEMO-SKU-003": (Decimal("36"), Decimal("35"), Decimal("1"), Decimal("50.00"), "Quality issue"),
            "DEMO-SKU-004": (Decimal("60"), Decimal("57"), Decimal("3"), Decimal("150.00"), "Short delivery"),
            "DEMO-SKU-005": (Decimal("42"), Decimal("40"), Decimal("2"), Decimal("110.00"), "Quality issue"),
            "DEMO-SKU-006": (Decimal("30"), Decimal("28"), Decimal("2"), Decimal("90.00"), "Quality issue"),
        },
    },
)


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  [PASS] {message}")


def one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def run():
    expected_files = {
        f"demo_{kind}_{number}.csv"
        for number in ("01", "02") for kind in ("po", "grn", "discrepancy")
    }
    actual_files = {path.name for path in FIXTURES.glob("*.csv")}
    check(actual_files == expected_files, "fixture directory contains exactly the six principal demo CSVs")

    print(f"Creating throwaway database {TEST_DB_NAME}...")
    create_database(TEST_DB_NAME)
    conn = bootstrap_connection(TEST_DB_NAME)
    try:
        customer = one(conn, "SELECT id FROM customers WHERE name=?", (CUSTOMER_NAME,))
        location = one(conn, "SELECT id FROM locations WHERE name=?", (SOURCE_LOCATION,))
        check(customer is not None and location is not None, "synthetic customer and source warehouse are seeded")
        customer_id, location_id = customer["id"], location["id"]

        sku_product = {
            row["external_sku"]: row["product_id"]
            for row in conn.execute(
                "SELECT external_sku,product_id FROM customer_product_skus WHERE customer_id=? AND active=TRUE",
                (customer_id,),
            ).fetchall()
        }
        opening_balance = Decimal("1000")
        for product_id in set(sku_product.values()):
            ingest.record_movement(
                conn, movement_date="2026-08-01", sku_code=None,
                movement_type="opening_balance", quantity=opening_balance,
                location_to=SOURCE_LOCATION, product_id=product_id,
            )
        for chain in CHAINS:
            check(set(chain["skus"]) <= set(sku_product), f"chain {chain['number']} uses mapped customer SKUs")
        conn.commit()

        # POs: stage, assign the seeded Drizzl source warehouse, and post.
        for chain in CHAINS:
            path = FIXTURES / f"demo_po_{chain['number']}.csv"
            staged = po_csv_staging.stage_po_csv(conn, path, customer_id=customer_id, filename=path.name)
            pos = po_csv_staging.list_staged_pos(conn, staged["batch_id"])
            check(len(pos) == 1 and pos[0]["validation_status"] == "valid", f"PO fixture {path.name} stages as one valid PO")
            staged_po_id = pos[0]["staged_po_id"]
            po_csv_staging.assign_source_location(conn, staged["batch_id"], [staged_po_id], location_id)
            posted = po_posting.post_staged_purchase_orders(conn, staged["batch_id"], [staged_po_id])
            check(not posted["rejected"] and len(posted["posted"]) == 1, f"PO {chain['po']} posts from {SOURCE_LOCATION}")
            activity_log.log_activity(conn, "po_csv_upload", f"Uploaded synthetic {path.name}", "po", chain["po"])
            activity_log.log_activity(conn, "po_posted", f"Posted synthetic PO {chain['po']}", "po", chain["po"])
            conn.commit()

        # GRNs: stage against the official PO, verify, and post sale + loss.
        expected_balances = {product_id: opening_balance for product_id in sku_product.values()}
        balances_after_grn = {}
        for chain in CHAINS:
            path = FIXTURES / f"demo_grn_{chain['number']}.csv"
            staged = grn_csv_staging.stage_grn_csv(conn, path, customer_id, filename=path.name)
            grn_csv_staging.revalidate_grn_batch(conn, staged["batch_id"])
            grns = grn_csv_staging.list_staged_grns(conn, staged["batch_id"])
            check(len(grns) == 1 and grns[0]["review_status"] == "verified", f"GRN fixture {path.name} verifies against {chain['po']}")
            staged_grn_id = grns[0]["staged_grn_id"]
            posted = grn_posting.post_staged_grns(conn, staged["batch_id"], [staged_grn_id])
            check(not posted["rejected"] and len(posted["posted"]) == 1, f"GRN {chain['grn']} posts successfully")
            grn_id = posted["posted"][0]["grn_id"]
            sales = conn.execute(
                "SELECT product_id,quantity FROM inventory_movements WHERE source_grn_line_item_id IN "
                "(SELECT id FROM grn_line_items WHERE grn_id=?) AND movement_type='sale' AND voided=0",
                (grn_id,),
            ).fetchall()
            losses = conn.execute(
                "SELECT product_id,quantity FROM inventory_movements WHERE source_grn_id=? "
                "AND reference_type='grn_discrepancy' AND movement_type='loss' AND voided=0",
                (grn_id,),
            ).fetchall()
            sale_by_product = {row["product_id"]: Decimal(str(row["quantity"])) for row in sales}
            loss_by_product = {row["product_id"]: Decimal(str(row["quantity"])) for row in losses}
            for sku, (ordered, received, shortfall, _, _) in chain["skus"].items():
                product_id = sku_product[sku]
                check(sale_by_product[product_id] == received, f"{chain['grn']} records {received:g} sale units for {sku}")
                check(loss_by_product[product_id] == shortfall, f"{chain['grn']} records {shortfall:g} shortfall units for {sku}")
                expected_balances[product_id] -= ordered
                balance = Decimal(str(reconcile.current_balance_by_product(conn, location_id, product_id)))
                check(balance == expected_balances[product_id], f"{chain['grn']} removes the full ordered quantity for {sku}")
                balances_after_grn[product_id] = balance
            activity_log.log_activity(conn, "grn_csv_upload", f"Uploaded synthetic {path.name}", "grn", chain["grn"])
            activity_log.log_activity(conn, "grn_posted", f"Posted synthetic GRN {chain['grn']}", "grn", chain["grn"])
            conn.commit()

        # Discrepancies: stage, reconcile quantities/debits, classify, and
        # prove classification changes labels only—not inventory balances.
        for chain in CHAINS:
            path = FIXTURES / f"demo_discrepancy_{chain['number']}.csv"
            staged = discrepancy_csv_staging.stage_csv(conn, path, customer_id, filename=path.name)
            _, lines = discrepancy_csv_staging.get_batch(conn, staged["batch_id"])
            check(len(lines) == len(chain["skus"]) and all(line["review_status"] == "ready" for line in lines), f"discrepancy fixture {path.name} stages fully ready")
            by_sku = {line["external_sku"]: line for line in lines}
            for sku, (_, _, shortfall, amount, cause) in chain["skus"].items():
                check(by_sku[sku]["rejected_qty"] == shortfall, f"{chain['pr']} quantity reconciles for {sku}")
                check(by_sku[sku]["rejected_amount"] == amount, f"{chain['pr']} debit reconciles for {sku}")
                check(by_sku[sku]["rejected_reason"] == cause, f"{chain['pr']} cause reconciles for {sku}")
            classified = discrepancy_csv_staging.classify_ready(conn, staged["batch_id"])
            check(classified == len(chain["skus"]), f"{chain['pr']} classifies every shortfall line")
            for sku in chain["skus"]:
                product_id = sku_product[sku]
                after = Decimal(str(reconcile.current_balance_by_product(conn, location_id, product_id)))
                check(after == balances_after_grn[product_id], f"{chain['pr']} does not deduct {sku} inventory twice")
            activity_log.log_activity(conn, "discrepancy_csv_upload", f"Uploaded synthetic {path.name}", "discrepancy", chain["pr"])
            activity_log.log_activity(conn, "discrepancy_classified", f"Classified synthetic {chain['pr']}", "discrepancy", chain["pr"])
            conn.commit()

        summary = reconcile.discrepancy_debit_summary(conn, "2026-08-01", "2026-08-25")
        check(Decimal(str(summary["total_debited"])) == Decimal("1380.00"), "Debits & Losses totals 1,380.00 across both chains")
        check(summary["discrepancy_notes"] == 2 and summary["discrepancy_lines"] == 12, "Debits & Losses reports two notes and twelve lines")
        causes = {row["cause"]: Decimal(str(row["total_damaged"])) for row in reconcile.discrepancy_units_by_cause(conn)}
        check(causes == {
            "Damaged": Decimal("3"),
            "Expired": Decimal("4"),
            "Packaging damage": Decimal("5"),
            "Quality issue": Decimal("5"),
            "Short delivery": Decimal("9"),
        }, "dashboard cause totals include all five classified scenarios")
        tracker = {row["po_number"]: row for row in reconcile.po_grn_fulfillment(conn)}
        check(all(tracker[c["po"]]["fulfillment_status"] == "grn_posted_discrepancy" for c in CHAINS), "PO-GRN Tracker includes both completed discrepancy chains")
        for chain in CHAINS:
            for query in (chain["po"], chain["grn"], chain["pr"]):
                lookup = reconcile.lookup_document(conn, query)
                check(lookup and lookup["po"]["po_number"] == chain["po"] and lookup["grns"][0]["grn_number"] == chain["grn"], f"lookup connects {query} to its PO and GRN")
                check(lookup["grns"][0]["discrepancy_notes"][0]["pr_number"] == chain["pr"], f"lookup connects {query} to {chain['pr']}")
                if query == chain["pr"]:
                    check(lookup["matched_document_type"] == "discrepancy", f"lookup recognizes {query} as a discrepancy note")
        activities = activity_log.recent_activity(conn, limit=50)
        references = {row["reference_id"] for row in activities}
        check(all(c[key] in references for c in CHAINS for key in ("po", "grn", "pr")), "Activity Log contains both synthetic import chains")

        # A discrepancy belongs to its exact GRN. Voiding that GRN keeps the
        # note for audit but excludes it from current reporting; restoring the
        # same grn_id reactivates both the movements and the note automatically.
        chain = CHAINS[0]
        grn_row = one(conn, "SELECT grn_id FROM grn_receipts WHERE grn_number=? AND voided=0", (chain["grn"],))
        ingest.void_grn(conn, chain["grn"], "synthetic lifecycle verification")
        conn.commit()
        voided_summary = reconcile.discrepancy_debit_summary(conn, "2026-08-01", "2026-08-25")
        check(Decimal(str(voided_summary["total_debited"])) == Decimal("760.00"), "voided GRN removes its discrepancy debit from current reporting")
        check(voided_summary["discrepancy_notes"] == 1 and voided_summary["discrepancy_lines"] == 6, "voided GRN removes its discrepancy note and lines from current counts")
        voided_tracker = {row["po_number"]: row for row in reconcile.po_grn_fulfillment(conn)}
        check(voided_tracker[chain["po"]]["fulfillment_status"] == "awaiting_grn", "voided GRN returns its PO to awaiting GRN")
        historical = reconcile.lookup_document(conn, chain["pr"])
        historical_note = next(g for g in historical["grns"] if g["grn_id"] == grn_row["grn_id"])["discrepancy_notes"][0]
        check(not historical_note["active"], "voided GRN keeps its discrepancy visible as inactive history")

        ingest.unvoid_grn(conn, grn_row["grn_id"])
        conn.commit()
        restored_summary = reconcile.discrepancy_debit_summary(conn, "2026-08-01", "2026-08-25")
        check(Decimal(str(restored_summary["total_debited"])) == Decimal("1380.00"), "restoring the same GRN reactivates its discrepancy debit")
        restored_tracker = {row["po_number"]: row for row in reconcile.po_grn_fulfillment(conn)}
        check(restored_tracker[chain["po"]]["fulfillment_status"] == "grn_posted_discrepancy", "restored GRN returns to GRN + discrepancy posted")
        restored = reconcile.lookup_document(conn, chain["pr"])
        restored_note = next(g for g in restored["grns"] if g["grn_id"] == grn_row["grn_id"])["discrepancy_notes"][0]
        check(restored_note["active"], "restoring the same GRN reactivates its attached discrepancy note")
        print("PASSED -- both public synthetic fixture chains completed end to end.")
        return True
    finally:
        conn.close()
        drop_database(TEST_DB_NAME)


if __name__ == "__main__":
    try:
        ok = run()
    except Exception as exc:
        print(f"FAILED -- {type(exc).__name__}: {exc}")
        ok = False
        try:
            drop_database(TEST_DB_NAME)
        except Exception:
            pass
    raise SystemExit(0 if ok else 1)

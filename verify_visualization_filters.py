"""Focused checks for visualization-specific business dates and sources."""
import sys

import app as app_module
import reconcile
from db import get_connection
from verify_db import bootstrap_connection, create_database, drop_database

TEST_DB_NAME = "drizzl_inventory_test_visualization_filters"


def check(label, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    return bool(condition)


def run():
    create_database(TEST_DB_NAME)
    conn = bootstrap_connection(TEST_DB_NAME)
    ok = True
    try:
        customer_id = conn.execute("SELECT id FROM customers ORDER BY id LIMIT 1").fetchone()["id"]
        product = conn.execute("SELECT product_id,barcode FROM master_products ORDER BY product_id LIMIT 1").fetchone()
        location = conn.execute("SELECT id,name FROM locations ORDER BY id LIMIT 1").fetchone()

        def add_po(number, generated, qty):
            po_id = conn.execute(
                """INSERT INTO purchase_orders
                   (po_number,customer_id,external_po_created_at,facility_name,
                    destination_facility_name,source_location_id)
                   VALUES (?,?,?,?,?,?) RETURNING po_id""",
                (number, customer_id, generated, "Facility A", "Facility A", location["id"]),
            ).fetchone()["po_id"]
            conn.execute(
                """INSERT INTO po_line_items
                   (po_number,item_code,item_desc,qty,product_id,external_sku)
                   VALUES (?,?,?,?,?,?)""",
                (number, product["barcode"], "Test product", qty, product["product_id"], product["barcode"]),
            )
            return po_id

        july_po = add_po("VIS-PO-JULY", "2026-07-10 08:00:00", 100)
        add_po("VIS-PO-AUG", "2026-08-10 08:00:00", 250)
        grn_id = conn.execute(
            """INSERT INTO grn_receipts
               (grn_number,po_number,po_id,customer_id,create_date,source_location_id)
               VALUES (?,?,?,?,?,?) RETURNING grn_id""",
            ("VIS-GRN-JULY", "VIS-PO-JULY", july_po, customer_id, "2026-07-12T10:30:00", location["id"]),
        ).fetchone()["grn_id"]
        batch_id = conn.execute(
            """INSERT INTO discrepancy_import_batches
               (customer_id,source_filename,file_sha256) VALUES (?,?,?) RETURNING batch_id""",
            (customer_id, "visualization.csv", "visualization-filter-test"),
        ).fetchone()["batch_id"]
        conn.execute(
            """INSERT INTO staged_discrepancy_lines
               (batch_id,source_row_number,raw_data,pr_number,po_number,grn_number,
                external_sku,product_id,rejected_qty,rejected_reason,official_grn_id,
                rejected_amount,completed_date,review_status,classified_at)
               VALUES (?,?,?::jsonb,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (batch_id, 2, '{}', "VIS-PR", "VIS-PO-JULY", "VIS-GRN-JULY",
             product["barcode"], product["product_id"], 12, "Short", grn_id,
             1008, "2026-08-05", "ready"),
        )
        # A manual loss must never enter the discrepancy-only charts.
        conn.execute(
            """INSERT INTO inventory_movements
               (movement_date,sku_code,product_id,movement_type,quantity,location_from_id,
                reference_type,notes)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("2026-07-12", product["barcode"], product["product_id"], "loss", 999,
             location["id"], "manual", "Manual warehouse loss"),
        )
        conn.commit()

        july = reconcile.po_quantity_by_facility(conn, date_from="2026-07-01", date_to="2026-07-31")
        august = reconcile.po_quantity_by_facility(conn, date_from="2026-08-01", date_to="2026-08-31")
        ok &= check("PO generated-date filter includes July PO only", sum(r["total_qty"] for r in july) == 100)
        ok &= check("PO generated-date filter includes August PO only", sum(r["total_qty"] for r in august) == 250)

        causes = reconcile.discrepancy_units_by_cause(conn, date_from="2026-07-12", date_to="2026-07-12")
        ok &= check("discrepancy cause uses GRN create date", len(causes) == 1 and causes[0]["total_damaged"] == 12)
        ok &= check("manual loss is excluded from discrepancy cause", all(r["total_damaged"] != 999 for r in causes))
        trend = reconcile.discrepancy_trend_over_time(conn, date_from="2026-07-12", date_to="2026-07-12")
        ok &= check("discrepancy trend uses GRN create date", len(trend) == 1 and str(trend[0]["date"]) == "2026-07-12" and trend[0]["qty"] == 12)

        chart_data = app_module._build_chart_data(
            conn, discrepancy_date_from="2026-07-12", discrepancy_date_to="2026-07-12"
        )
        ok &= check(
            "discrepancy chart label shows month, day, and year without time",
            chart_data["damage_trend"]["labels"] == ["Jul 12, 2026"],
            str(chart_data["damage_trend"]["labels"]),
        )

        debit_august = reconcile.discrepancy_debit_summary(
            conn, date_from="2026-08-01", date_to="2026-08-31"
        )
        debit_july = reconcile.discrepancy_debit_summary(
            conn, date_from="2026-07-01", date_to="2026-07-31"
        )
        ok &= check("debit total uses discrepancy CompletedDate", debit_august["total_debited"] == 1008)
        ok &= check("debit date filter excludes other months", debit_july["total_debited"] == 0)
        completed_causes = reconcile.discrepancy_units_by_cause(
            conn, date_from="2026-08-01", date_to="2026-08-31", date_source="completed"
        )
        completed_causes_july = reconcile.discrepancy_units_by_cause(
            conn, date_from="2026-07-01", date_to="2026-07-31", date_source="completed"
        )
        ok &= check("cause totals use the same CompletedDate filter", completed_causes[0]["total_damaged"] == 12)
        ok &= check("cause CompletedDate filter excludes other months", completed_causes_july == [])
        ok &= check("debit total counts the note once", debit_august["discrepancy_notes"] == 1)
        ok &= check("debit summary counts distinct POs", debit_august["purchase_orders"] == 1)
        debit_by_po = reconcile.discrepancy_debits_by_po(
            conn, date_from="2026-08-01", date_to="2026-08-31"
        )
        ok &= check(
            "debit-by-PO table reconciles to the summary",
            len(debit_by_po) == 1
            and debit_by_po[0]["po_number"] == "VIS-PO-JULY"
            and debit_by_po[0]["total_debited"] == debit_august["total_debited"],
        )
        lookup = reconcile.lookup_document(conn, "VIS-PO-JULY")
        attached = lookup["grns"][0]["discrepancy_notes"]
        ok &= check(
            "lookup attaches the classified discrepancy to its exact GRN",
            len(attached) == 1
            and attached[0]["pr_number"] == "VIS-PR"
            and attached[0]["rejected_amount"] == 1008
            and len(attached[0]["lines"]) == 1,
        )

        with app_module.app.test_request_context("/?po_date_from=2026-08-02&po_date_to=2026-08-01"):
            _, _, error = app_module._optional_date_range("po")
        ok &= check("reversed date range is rejected", error is not None)
        with app_module.app.test_request_context("/?po_period=15d"):
            period, quick_from, quick_to, error = app_module._visualization_date_range("po")
        ok &= check(
            "15-day quick range is inclusive",
            period == "15d" and error is None
            and (app_module.date.fromisoformat(quick_to) - app_module.date.fromisoformat(quick_from)).days == 14,
        )
        template_source = open("templates/visualizations.html").read()
        ok &= check("stock filter reload returns to stock chart section", '#stock-visualization' in template_source)
        ok &= check("PO filter reload returns to PO chart section", '#po-visualization' in template_source)
        ok &= check("discrepancy filter reload returns to discrepancy chart section", '#discrepancy-visualization' in template_source)
        return ok
    finally:
        conn.close()
        drop_database(TEST_DB_NAME)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

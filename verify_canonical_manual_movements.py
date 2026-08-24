"""Regression checks for Master-Product-only manual inventory movements."""
import sys

from werkzeug.security import generate_password_hash

from verify_db import bootstrap_connection, create_database, drop_database, point_app_at

TEST_DB_NAME = "drizzl_inventory_test_manual_movements"
point_app_at(TEST_DB_NAME)

from app import app, product_label  # noqa: E402  (database target must be patched first)
from db import get_connection  # noqa: E402


def check(label, condition, failures, detail=""):
    if not condition:
        failures.append(f"  {label}" + (f": {detail}" if detail else ""))


def run_checks():
    conn = get_connection()
    failures = []
    try:
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["TESTING"] = True
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("manual_movement_test", generate_password_hash("not-a-real-password")),
        )
        conn.commit()

        client = app.test_client()
        login = client.post(
            "/login",
            data={"username": "manual_movement_test", "password": "not-a-real-password"},
        )
        check("test login succeeds", login.status_code in (302, 303), failures)

        product = conn.execute(
            "SELECT product_id, barcode, product_name FROM master_products ORDER BY product_id LIMIT 1"
        ).fetchone()

        page = client.get("/movements/new")
        body = page.get_data(as_text=True)
        check("movement page renders", page.status_code == 200, failures)
        check("Master Product dropdown is present", 'name="product_id"' in body, failures)
        check("free-text sku_code input is absent", 'name="sku_code"' not in body, failures)
        check("brand-new SKU description input is absent", 'name="sku_desc"' not in body, failures)
        check("short active Master Product label is listed", product_label(product["product_name"]) in body, failures)
        check("barcode is absent from the product dropdown", product["barcode"] not in body, failures)

        before_legacy = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
        created = client.post(
            "/movements/new",
            data={
                "movement_date": "2026-08-22",
                "movement_type": "opening_balance",
                "product_id": str(product["product_id"]),
                "quantity": "500",
                "location_to": "Drizzl Demo Warehouse",
                "location_to_type": "own_facility",
            },
        )
        check("canonical opening balance redirects", created.status_code in (302, 303), failures)
        movement = conn.execute(
            "SELECT product_id, sku_code, quantity FROM inventory_movements ORDER BY id DESC LIMIT 1"
        ).fetchone()
        check("movement carries product_id", movement and movement["product_id"] == product["product_id"], failures)
        check("movement barcode is derived from Master Product", movement and movement["sku_code"] == product["barcode"], failures)
        check("movement quantity is preserved", movement and float(movement["quantity"]) == 500, failures)
        after_legacy = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
        check("manual movement creates no legacy product", after_legacy == before_legacy, failures)

        rejected = client.post(
            "/movements/new",
            data={
                "movement_date": "2026-08-22",
                "movement_type": "production",
                "product_id": "999999",
                "quantity": "10",
                "location_to": "Drizzl Demo Warehouse",
            },
        )
        rejected_body = rejected.get_data(as_text=True)
        check("unknown Master Product is rejected", "does not exist or is inactive" in rejected_body, failures)
        count = conn.execute("SELECT COUNT(*) AS n FROM inventory_movements").fetchone()["n"]
        check("unknown product creates no movement", count == 1, failures, f"got {count}")

        warning = client.post(
            "/movements/new",
            data={
                "movement_date": "2026-08-22",
                "movement_type": "sale",
                "product_id": str(product["product_id"]),
                "quantity": "600",
                "location_from": "Drizzl Demo Warehouse",
            },
        )
        warning_body = warning.get_data(as_text=True)
        check("negative warning is shown", "create negative inventory" in warning_body, failures)
        check("warning preserves product_id", f'name="product_id" value="{product["product_id"]}"' in warning_body, failures)

        override = client.post(
            "/movements/new",
            data={
                "confirmed_override": "1",
                "severity": "negative",
                "movement_date": "2026-08-22",
                "movement_type": "sale",
                "product_id": str(product["product_id"]),
                "quantity": "600",
                "location_from": "Drizzl Demo Warehouse",
                "override_reason": "Regression test",
            },
        )
        check("confirmed negative movement redirects", override.status_code in (302, 303), failures)
        latest = conn.execute(
            "SELECT product_id, sku_code FROM inventory_movements ORDER BY id DESC LIMIT 1"
        ).fetchone()
        flag = conn.execute(
            "SELECT product_id, sku_code FROM inventory_flags ORDER BY id DESC LIMIT 1"
        ).fetchone()
        check("confirmed movement stays canonical", latest and latest["product_id"] == product["product_id"], failures)
        check("negative flag stays canonical", flag and flag["product_id"] == product["product_id"], failures)
        check("negative flag uses Master barcode", flag and flag["sku_code"] == product["barcode"], failures)

        check("removed legacy PDF upload returns 404", client.get("/upload").status_code == 404, failures)
    finally:
        conn.close()

    if failures:
        print(f"FAILED ({len(failures)} issue(s)):")
        print("\n".join(failures))
    else:
        print(
            "PASSED -- manual movements require an active Master Product, derive the barcode, never create "
            "legacy products, preserve product_id through warning confirmation and flags, reject unknown "
            "products, and the legacy PDF upload route is gone."
        )
    return not failures


def run():
    create_database(TEST_DB_NAME)
    bootstrap = bootstrap_connection(TEST_DB_NAME)
    bootstrap.close()
    try:
        return run_checks()
    finally:
        drop_database(TEST_DB_NAME)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

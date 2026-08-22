"""
Verifies Phase 12's security additions: authentication, CSRF protection,
and production configuration safety.

Runs entirely against a disposable throwaway Postgres database
(drizzl_inventory_test_security) -- config.DATABASE_URL is monkeypatched
before `app` is imported, same pattern as verify_grn_review_ui.py. The
real drizzl_inventory database is never touched.

Unlike verify_po_review_ui.py/verify_grn_review_ui.py (which disable CSRF
for their own test_client() runs -- a same-process test harness has no
cross-site attacker to defend against, matching Flask-WTF's own
documented testing guidance), THIS script is specifically testing that
CSRF enforcement itself works, so WTF_CSRF_ENABLED stays on by default
here and is only turned off for the one control test that proves a
*valid* token still lets a real request through.
"""
import subprocess
import sys

import psycopg2
from werkzeug.security import generate_password_hash

TEST_DB_NAME = "drizzl_inventory_test_security"

import config
config.DATABASE_URL = f"dbname={TEST_DB_NAME}"  # must happen before `import app`

import db as db_module
from app import app


def check(label, condition, detail=""):
    condition = bool(condition)
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    return condition


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


def get_csrf_token(client, path):
    resp = client.get(path)
    html = resp.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    return html[start:end]


def run():
    print(f"Creating throwaway database {TEST_DB_NAME}...")
    create_test_database()
    ok = True

    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("testadmin", generate_password_hash("correct-horse-battery-staple")),
        )
        conn.commit()

        raw_hash = conn.execute("SELECT password_hash FROM users WHERE username = ?", ("testadmin",)).fetchone()["password_hash"]
    finally:
        conn.close()

    print("\n--- 4: passwords are hashed, never stored in plain text ---")
    ok &= check("stored password_hash is not the plaintext password", raw_hash != "correct-horse-battery-staple")
    ok &= check("stored password_hash looks like a Werkzeug hash (contains a method prefix)", raw_hash.startswith(("pbkdf2:", "scrypt:")), raw_hash[:20])

    app.config["WTF_CSRF_ENABLED"] = True
    app.config["TESTING"] = True
    client = app.test_client()

    print("\n--- 1: unauthenticated user cannot access protected workflows ---")
    resp = client.get("/", follow_redirects=False)
    ok &= check("GET / redirects (not 200) when logged out", resp.status_code in (301, 302, 303, 308), f"got {resp.status_code}")
    resp = client.get("/lookup", follow_redirects=True)
    ok &= check("following the redirect lands on the login page", b"Log in" in resp.data)
    resp = client.get("/upload", follow_redirects=False)
    ok &= check("GET /upload also redirects when logged out", resp.status_code in (301, 302, 303, 308), f"got {resp.status_code}")

    print("\n--- health endpoint is exempt and reveals nothing sensitive ---")
    resp = client.get("/health")
    ok &= check("GET /health works without a session", resp.status_code == 200, f"got {resp.status_code}")
    body = resp.get_data(as_text=True)
    ok &= check("health response body has no schema/secret leakage", "SECRET_KEY" not in body and "DATABASE_URL" not in body and "CREATE TABLE" not in body)

    print("\n--- 3: invalid login fails ---")
    resp = client.post("/login", data={
        "csrf_token": get_csrf_token(client, "/login"),
        "username": "testadmin", "password": "wrong-password",
    }, follow_redirects=True)
    ok &= check("wrong password does not log in", b"Invalid username or password" in resp.data)
    resp = client.get("/", follow_redirects=False)
    ok &= check("still redirected (no session was created)", resp.status_code in (301, 302, 303, 308))

    print("\n--- 2: valid login works ---")
    resp = client.post("/login", data={
        "csrf_token": get_csrf_token(client, "/login"),
        "username": "testadmin", "password": "correct-horse-battery-staple",
    }, follow_redirects=True)
    ok &= check("correct credentials log in (dashboard reachable)", resp.status_code == 200 and b"Dashboard" in resp.data)
    resp = client.get("/", follow_redirects=False)
    ok &= check("protected route now reachable with a session", resp.status_code == 200, f"got {resp.status_code}")

    movement_fields = {
        "sku_code": "TESTSKU", "quantity": "5", "movement_type": "opening_balance",
        "location_to": "Drizzl Demo Warehouse", "movement_date": "2026-08-17",
    }

    print("\n--- 6: CSRF blocks a mutation request with a missing/invalid token ---")
    resp = client.post("/movements/new", data={"csrf_token": "not-a-real-token", **movement_fields})
    ok &= check("invalid CSRF token is rejected (400)", resp.status_code == 400, f"got {resp.status_code}")
    resp = client.post("/movements/new", data=movement_fields)
    ok &= check("missing CSRF token is rejected (400)", resp.status_code == 400, f"got {resp.status_code}")

    print("\n--- 7: a valid form (real token) still works ---")
    resp = client.post("/movements/new", data={
        "csrf_token": get_csrf_token(client, "/movements/new"), **movement_fields,
    }, follow_redirects=True)
    ok &= check("valid CSRF token + valid form succeeds", resp.status_code == 200 and b"error" not in resp.data.lower()[:200])
    conn = db_module.get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM inventory_movements WHERE sku_code = 'TESTSKU'").fetchone()
        ok &= check("the movement was actually recorded", row["n"] == 1, f"got {row['n']}")
    finally:
        conn.close()

    print("\n--- 5: logout works ---")
    resp = client.post("/logout", data={"csrf_token": get_csrf_token(client, "/")}, follow_redirects=False)
    ok &= check("logout redirects", resp.status_code in (301, 302, 303, 308))
    resp = client.get("/", follow_redirects=False)
    ok &= check("protected route unreachable again after logout", resp.status_code in (301, 302, 303, 308))

    print("\n--- 9: debug is off whenever APP_ENV=production ---")
    import importlib
    import os
    old_env = dict(os.environ)
    try:
        os.environ["APP_ENV"] = "production"
        os.environ["SECRET_KEY"] = "test-secret-for-this-check-only"
        os.environ["DATABASE_URL"] = f"dbname={TEST_DB_NAME}"
        prod_config = importlib.reload(config)
        ok &= check("config.DEBUG is False under APP_ENV=production", prod_config.DEBUG is False)
        ok &= check("config.IS_PRODUCTION is True", prod_config.IS_PRODUCTION is True)
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        importlib.reload(config)

    print("\n--- 9 (cont'd): production config has no hard-coded secret -- refuses to start without one ---")
    probe_env = dict(old_env)
    probe_env.pop("SECRET_KEY", None)
    probe_env["APP_ENV"] = "production"
    probe_env["DATABASE_URL"] = f"dbname={TEST_DB_NAME}"
    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=".", env=probe_env, capture_output=True, text=True,
    )
    ok &= check(
        "`import config` exits non-zero with APP_ENV=production and no SECRET_KEY",
        result.returncode != 0, f"returncode={result.returncode}",
    )
    ok &= check(
        "the failure message names the missing variable, not a stack trace of internals",
        "SECRET_KEY" in (result.stdout + result.stderr),
    )

    print("\n--- source check: no insecure SECRET_KEY fallback string reachable in app.py ---")
    app_source = open("app.py").read()
    ok &= check(
        "app.py does not contain a hard-coded fallback secret string",
        "dev-only-secret-change-before-deploy" not in app_source and 'os.environ.get("SECRET_KEY", "' not in app_source,
    )

    print(f"\nDropping throwaway database {TEST_DB_NAME}...")
    drop_test_database()
    return ok


if __name__ == "__main__":
    ok = run()
    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    sys.exit(0 if ok else 1)

"""Database connection helper. Connects to the drizzl_inventory Postgres
database, creating its tables/seed data from schema_postgres.sql on first use."""
import psycopg2
import psycopg2.extras
from pathlib import Path

import config

# Phase 12: DATABASE_URL (a full libpq connection string/DSN, e.g.
# "postgresql://user:pass@host:5432/dbname" or the local dev default
# "dbname=drizzl_inventory") is now the single source of truth for which
# database to connect to -- config.py resolves it from the environment,
# with a local-dev fallback. DB_NAME stays exported for anything that
# still wants just the database's short name (informational only, e.g.
# db.py's own __main__ printout) -- it is never used to build the actual
# connection anymore.
DB_NAME = "drizzl_inventory"
SCHEMA_PATH = Path(__file__).parent / "schema_postgres.sql"
# Phase 1 catalog seed (master_products + customer_product_skus). Reuses
# the migration file directly instead of duplicating its idempotent seed
# logic in two places -- see _seed_catalog() below.
CATALOG_MIGRATION_PATH = Path(__file__).parent / "migrations" / "001_master_product_identity.sql"


class _PGConnection:
    """Wraps a psycopg2 connection so the rest of the app -- written against
    sqlite3's API -- can keep calling conn.execute(sql, params) with '?'
    placeholders and reading rows as row["col"], unchanged."""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def executescript(self, sql_text):
        cur = self._conn.cursor()
        cur.execute(sql_text)
        cur.close()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def _seed(conn):
    """Known reference data every install needs, independent of any
    uploaded document: today's one real customer, and the home base
    stock ships from."""
    conn.execute(
        "INSERT INTO customers (name, notes) VALUES (?, ?)",
        ("Scootsy Logistics Private Limited", "Swiggy Instamart's B2B fulfillment arm"),
    )
    conn.execute(
        "INSERT INTO locations (name, type) VALUES (?, ?)",
        ("Drizzl Demo Warehouse", "own_facility"),
    )


def _seed_catalog(conn):
    """Must run after _seed() -- the migration file looks up Scootsy's
    customer_id by name, so Scootsy has to already exist. Only called
    once, on a genuinely fresh install (same needs_seed gate as _seed()),
    not on every connection -- the migration itself is idempotent, but
    there's no reason to re-run it on every request."""
    conn.executescript(CATALOG_MIGRATION_PATH.read_text())


def get_connection():
    """Connects using config.DATABASE_URL (read at call time, not import
    time, so tests can monkeypatch config.DATABASE_URL before the first
    call and every connection -- including ones made from inside app.py's
    own request handlers -- transparently targets a different database).

    The auto-bootstrap below (create tables / seed reference data on a
    genuinely empty database) is a local-dev convenience, not a
    migration runner -- it only ever does a plain CREATE TABLE pass
    against a database with no `customers` table yet, never an ALTER/DROP,
    and becomes a pure no-op forever after the first real connection. In
    production, provision the schema explicitly first (see README) so
    this path is never exercised by a live request."""
    raw_conn = psycopg2.connect(config.DATABASE_URL)
    conn = _PGConnection(raw_conn)

    tables_exist = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'customers'"
    ).fetchone() is not None
    if not tables_exist:
        conn.executescript(SCHEMA_PATH.read_text())

    needs_seed = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"] == 0
    if needs_seed:
        _seed(conn)
        _seed_catalog(conn)

    conn.commit()
    return conn


if __name__ == "__main__":
    conn = get_connection()
    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name"
    ).fetchall()
    print(f"Connected to Postgres database '{DB_NAME}'")
    print("Tables:", ", ".join(t["table_name"] for t in tables))
    conn.close()

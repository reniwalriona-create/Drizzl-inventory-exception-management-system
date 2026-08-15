"""Database connection helper. Connects to the drizzl_inventory Postgres
database, creating its tables/seed data from schema_postgres.sql on first use."""
import psycopg2
import psycopg2.extras
from pathlib import Path

DB_NAME = "drizzl_inventory"
SCHEMA_PATH = Path(__file__).parent / "schema_postgres.sql"


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


def get_connection():
    raw_conn = psycopg2.connect(dbname=DB_NAME)
    conn = _PGConnection(raw_conn)

    tables_exist = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'customers'"
    ).fetchone() is not None
    if not tables_exist:
        conn.executescript(SCHEMA_PATH.read_text())

    needs_seed = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"] == 0
    if needs_seed:
        _seed(conn)

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

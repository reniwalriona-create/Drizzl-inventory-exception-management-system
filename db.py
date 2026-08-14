"""Database connection helper. Creates inventory.db from schema.sql on first use."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "inventory.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


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


def _ensure_activity_log(conn):
    """activity_log was added after inventory.db already existed on some
    installs -- create it here too so an existing database picks it up
    without needing a manual migration step."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type    TEXT NOT NULL,
            description    TEXT NOT NULL,
            reference_type TEXT,
            reference_id   TEXT,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_log_created_at ON activity_log(created_at)")


def _ensure_column(conn, table, column, coltype):
    """SQLite has no "ADD COLUMN IF NOT EXISTS" -- check PRAGMA table_info
    first so re-running this on a database that already has the column
    (e.g. every fresh install, where schema.sql already created it) is a
    harmless no-op rather than an error."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def _ensure_negative_inventory_protection(conn):
    """negative_override_reason and inventory_flags were added after
    inventory.db already existed on some installs -- same pattern as
    _ensure_activity_log above."""
    _ensure_column(conn, "inventory_movements", "negative_override_reason", "TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory_flags (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            movement_id       INTEGER,
            sku_code          TEXT,
            location_name     TEXT,
            source            TEXT NOT NULL,
            reference_id      TEXT,
            available_before  REAL,
            requested_qty     REAL,
            resulting_balance REAL,
            reason            TEXT,
            resolved          INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_flags_unresolved ON inventory_flags(resolved)")


def _ensure_void_columns(conn):
    """voided/void_reason/voided_at were added to four tables after
    inventory.db already existed on some installs -- same pattern as
    above. See PROJECT_HANDOFF.md for the void-not-delete writeup."""
    for table in ("inventory_movements", "purchase_orders", "grn_receipts", "discrepancy_notes"):
        _ensure_column(conn, table, "voided", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, table, "void_reason", "TEXT")
        _ensure_column(conn, table, "voided_at", "TEXT")


def _ensure_commitment_columns(conn):
    """source_location_id (purchase_orders + grn_receipts) and
    commitment_override_reason (inventory_movements) were added after
    inventory.db already existed on some installs -- same pattern as
    above. See PROJECT_HANDOFF.md for the Committed/Uncommitted writeup."""
    _ensure_column(conn, "purchase_orders", "source_location_id", "INTEGER REFERENCES locations(id)")
    _ensure_column(conn, "grn_receipts", "source_location_id", "INTEGER REFERENCES locations(id)")
    _ensure_column(conn, "inventory_movements", "commitment_override_reason", "TEXT")


def get_connection():
    is_new = not DB_PATH.exists()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if is_new:
        conn.executescript(SCHEMA_PATH.read_text())
        _seed(conn)
        conn.commit()
    else:
        _ensure_activity_log(conn)
        _ensure_negative_inventory_protection(conn)
        _ensure_void_columns(conn)
        _ensure_commitment_columns(conn)
        conn.commit()
    return conn


if __name__ == "__main__":
    conn = get_connection()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    print(f"Database ready at {DB_PATH}")
    print("Tables:", ", ".join(t["name"] for t in tables))
    conn.close()

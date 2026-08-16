"""
Purchase Order identity helpers (Phase 2).

po_id is Drizzl's own internal relational identifier -- the actual
PostgreSQL primary key of purchase_orders as of the Phase 2 migration.
po_number is retained as temporary backwards-compatible scaffolding --
every existing PO/GRN/Discrepancy Note/Debit Note/Appointment table still
references po_number, not po_id, until a later phase migrates them. The
customer-issued PO number is exposed here semantically as
external_po_number even though the physical column is still named
po_number, deliberately avoiding a second, sync-risk-prone physical
column for now. See PROJECT_HANDOFF.md and
migrations/002_po_identity_foundation.sql.
"""


def _clean(value):
    return value.strip() if isinstance(value, str) else value


def _row_to_po(row):
    if row is None:
        return None
    po = dict(row)
    po["external_po_number"] = po["po_number"]
    return po


def get_po_by_id(conn, po_id):
    row = conn.execute("SELECT * FROM purchase_orders WHERE po_id = ?", (po_id,)).fetchone()
    return _row_to_po(row)


def get_po_by_customer_and_number(conn, customer_id, external_po_number):
    """Exact match only -- never fuzzy, never infers the customer, never
    matches by PO number alone when a customer_id is supplied."""
    external_po_number = _clean(external_po_number)
    row = conn.execute(
        "SELECT * FROM purchase_orders WHERE customer_id = ? AND po_number = ?",
        (customer_id, external_po_number),
    ).fetchone()
    return _row_to_po(row)


def resolve_po_identity(conn, customer_id, external_po_number):
    """Thin strict wrapper around get_po_by_customer_and_number() -- same
    exact-match rules, kept as a separate name for ingestion-style callers
    that are 'resolving an identity' rather than 'looking up a known PO'."""
    return get_po_by_customer_and_number(conn, customer_id, external_po_number)

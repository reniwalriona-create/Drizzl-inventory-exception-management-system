"""
Canonical Drizzl product identity and customer-SKU mapping (Phase 1).

Deliberately separate from ingest.py's _ensure_product() and the legacy
products/sku_code system, which every existing PO/GRN/movement table still
uses -- nothing here is wired into that path yet. See PROJECT_HANDOFF.md.
"""


def _clean(value):
    return value.strip() if isinstance(value, str) else value


def get_master_product_by_barcode(conn, barcode):
    """Returns the master_products row for this barcode, or None if it
    doesn't exist -- never guessed/created here."""
    barcode = _clean(barcode)
    return conn.execute(
        "SELECT product_id, barcode, product_name, unit_size, active FROM master_products WHERE barcode = ?",
        (barcode,),
    ).fetchone()


def resolve_customer_sku(conn, customer_id, external_sku):
    """A customer's own SKU code -> the master product it represents, or
    None if this customer has no mapping for that code. Never guesses and
    never creates a master product as a side effect."""
    external_sku = _clean(external_sku)
    return conn.execute(
        """
        SELECT mp.product_id, mp.barcode, mp.product_name, mp.unit_size
        FROM customer_product_skus cps
        JOIN master_products mp ON mp.product_id = cps.product_id
        WHERE cps.customer_id = ? AND cps.external_sku = ? AND cps.active = TRUE
        """,
        (customer_id, external_sku),
    ).fetchone()


def add_customer_sku_mapping(conn, customer_id, product_id, external_sku, external_description=None):
    """Records that this customer's external_sku means this product_id.
    Raises if (customer_id, external_sku) already exists -- callers that
    want upsert behavior should check resolve_customer_sku() first."""
    external_sku = _clean(external_sku)
    return conn.execute(
        """
        INSERT INTO customer_product_skus (customer_id, product_id, external_sku, external_description)
        VALUES (?, ?, ?, ?)
        RETURNING id
        """,
        (customer_id, product_id, external_sku, external_description),
    ).fetchone()["id"]


def list_customer_sku_mappings(conn, customer_id):
    """Every SKU mapping this customer has, each joined with the master
    product it resolves to."""
    return conn.execute(
        """
        SELECT cps.id, cps.external_sku, cps.external_description, cps.active,
               mp.product_id, mp.barcode, mp.product_name, mp.unit_size
        FROM customer_product_skus cps
        JOIN master_products mp ON mp.product_id = cps.product_id
        WHERE cps.customer_id = ?
        ORDER BY mp.product_name
        """,
        (customer_id,),
    ).fetchall()

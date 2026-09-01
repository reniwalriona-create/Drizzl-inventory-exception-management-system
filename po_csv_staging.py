"""
PO CSV staging (Phase 3).

Stages a bulk Demo Commerce-style PO CSV export into po_import_batches /
po_import_rows / staged_purchase_orders / staged_po_lines. Nothing here
ever touches the official ledger (purchase_orders, po_line_items,
inventory_movements, grn_receipts, debit_notes) -- a staged PO is not an
official PO. Review and posting are later phases.

Transaction ownership: these functions do NOT call conn.commit() or
conn.rollback() themselves (unlike ingest.py's upsert_* functions) --
the caller owns the transaction, so a whole CSV can be staged atomically
and a fatal error can be rolled back cleanly by the caller.

Deliberately does not use ingest.py's _ensure_product()/legacy products
table, and does not call any function with official ledger effects
(upsert_po(), record_movement(), etc.) -- see TECHNICAL_README.md.
"""
import csv
import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

import catalog

# Structural columns without which a row/PO can't be identified or staged
# at all. Everything else is parsed when present, stored as NULL when
# absent -- see _parse_row_metadata()/_parse_line_fields().
REQUIRED_COLUMNS = [
    "PoNumber", "Entity", "FacilityId", "FacilityName", "City",
    "Status", "VendorName", "SkuCode", "SkuDescription", "OrderedQty",
]

PO_LEVEL_FIELDS = [
    "Entity", "FacilityId", "FacilityName", "City", "PoCreatedAt",
    "PoModifiedAt", "Status", "SupplierCode", "VendorName", "PoAmount",
    "ExpectedDeliveryDate", "PoExpiryDate", "OtbReferenceNumber",
    "InternalExternalPo", "ReferencePoNumber",
]


class FatalImportError(Exception):
    """Raised for file-level problems that must prevent the whole batch
    from being staged -- the caller should roll back on this."""


def _clean(value):
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def _normalize_entity(value):
    return _clean(value).casefold() if _clean(value) is not None else None


def _parse_decimal(value):
    """Returns (parsed_value_or_None, error_or_None)."""
    value = _clean(value)
    if value is None:
        return None, None
    try:
        return Decimal(value), None
    except InvalidOperation:
        return None, f"could not parse {value!r} as a number"


def _parse_int(value):
    value = _clean(value)
    if value is None:
        return None, None
    try:
        return int(value), None
    except ValueError:
        return None, f"could not parse {value!r} as an integer"


def _parse_timestamp(value):
    value = _clean(value)
    if value is None:
        return None, None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S"), None
    except ValueError:
        return None, f"could not parse {value!r} as a timestamp (expected YYYY-MM-DD HH:MM:SS)"


def _parse_date(value):
    value = _clean(value)
    if value is None:
        return None, None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date(), None
    except ValueError:
        return None, f"could not parse {value!r} as a date (expected YYYY-MM-DD)"


def _file_sha256(csv_path):
    h = hashlib.sha256()
    with open(csv_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_or_create_batch(conn, customer_id, filename, file_hash, source_entity):
    existing = conn.execute(
        "SELECT batch_id FROM po_import_batches WHERE customer_id = ? AND file_sha256 = ?",
        (customer_id, file_hash),
    ).fetchone()
    if existing:
        return existing["batch_id"], True

    row = conn.execute(
        """
        INSERT INTO po_import_batches (customer_id, source_filename, file_sha256, source_entity)
        VALUES (?, ?, ?, ?)
        RETURNING batch_id
        """,
        (customer_id, filename, file_hash, source_entity),
    ).fetchone()
    return row["batch_id"], False


def _resolve_customer(conn, customer_id, entities_in_file):
    """Applies the exact rules from the spec: single non-blank entity in
    the file, case-insensitive/whitespace-insensitive exact match, no
    fuzzy matching, no auto-create, no "only one customer" fallback."""
    non_blank = {e for e in entities_in_file if _clean(e) is not None}
    if len(non_blank) > 1:
        raise FatalImportError(f"CSV contains multiple distinct customer entities: {sorted(non_blank)}")

    file_entity = next(iter(non_blank), None)
    file_entity_norm = _normalize_entity(file_entity) if file_entity else None

    if customer_id is not None:
        row = conn.execute("SELECT id, name FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if row is None:
            raise FatalImportError(f"customer_id {customer_id} does not exist")
        if file_entity_norm is not None and file_entity_norm != _normalize_entity(row["name"]):
            raise FatalImportError(
                f"CSV entity {file_entity!r} does not match the supplied customer {row['name']!r}"
            )
        return row["id"], file_entity

    if file_entity_norm is None:
        raise FatalImportError("CSV has no non-blank Entity value and no customer_id was supplied")

    candidates = conn.execute("SELECT id, name FROM customers").fetchall()
    matches = [c for c in candidates if _normalize_entity(c["name"]) == file_entity_norm]
    if len(matches) != 1:
        raise FatalImportError(f"CSV entity {file_entity!r} did not resolve to exactly one known customer")
    return matches[0]["id"], file_entity


def _validate_po_level_consistency(rows_for_po):
    """Returns a list of structured errors if any PO-level field disagrees
    across the rows sharing this PoNumber."""
    errors = []
    if not rows_for_po:
        return errors
    first = rows_for_po[0]
    for field in PO_LEVEL_FIELDS:
        first_val = _clean(first.get(field))
        for r in rows_for_po[1:]:
            val = _clean(r.get(field))
            if val != first_val:
                errors.append({
                    "code": "inconsistent_po_metadata",
                    "field": field,
                    "message": f"Rows for this PO disagree on {field}",
                    "value": [first_val, val],
                })
                break
    return errors


def stage_po_csv(conn, csv_path, customer_id=None, filename=None):
    """Stages one CSV file. Returns a dict:
        {"batch_id": ..., "reused_existing_batch": bool}
    Raises FatalImportError for file-level problems -- the caller should
    roll back the transaction in that case. Does not commit/rollback
    itself; the caller owns the transaction."""
    filename = filename or str(csv_path)
    file_hash = _file_sha256(csv_path)

    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise FatalImportError("CSV has no header row")
            missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
            if missing:
                raise FatalImportError(f"CSV is missing required structural column(s): {missing}")
            rows = list(reader)
    except csv.Error as e:
        raise FatalImportError(f"file could not be read as CSV: {e}")
    except OSError as e:
        raise FatalImportError(f"file could not be opened: {e}")

    entities_in_file = {r.get("Entity") for r in rows}
    resolved_customer_id, source_entity = _resolve_customer(conn, customer_id, entities_in_file)

    batch_id, reused = _get_or_create_batch(conn, resolved_customer_id, filename, file_hash, source_entity)
    if reused:
        return {"batch_id": batch_id, "reused_existing_batch": True}

    # Group rows by PoNumber (order-preserving) so each PO's lines stay together.
    po_groups = {}
    for i, row in enumerate(rows, start=1):
        po_number = _clean(row.get("PoNumber"))
        po_groups.setdefault(po_number, []).append((i, row))

    for po_number, indexed_rows in po_groups.items():
        rows_only = [r for _, r in indexed_rows]
        po_level_errors = _validate_po_level_consistency(rows_only) if po_number else []
        po_blocked = po_number is None or bool(po_level_errors)

        staged_po_id = None
        if po_number is not None:
            first_row = rows_only[0]
            po_created_at, _ = _parse_timestamp(first_row.get("PoCreatedAt"))
            po_modified_at, _ = _parse_timestamp(first_row.get("PoModifiedAt"))
            po_amount, _ = _parse_decimal(first_row.get("PoAmount"))
            expected_delivery_date, _ = _parse_date(first_row.get("ExpectedDeliveryDate"))
            po_expiry_date, _ = _parse_date(first_row.get("PoExpiryDate"))
            po_ageing, _ = _parse_int(first_row.get("PoAgeing"))

            po_row = conn.execute(
                """
                INSERT INTO staged_purchase_orders
                    (batch_id, customer_id, external_po_number, entity_raw,
                     destination_facility_id, destination_facility_name, destination_city,
                     po_created_at, po_modified_at, external_status, supplier_code, vendor_name,
                     po_amount, expected_delivery_date, po_expiry_date, otb_reference_number,
                     internal_external_po, po_ageing, brand_name, reference_po_number,
                     source_location_id, validation_status, validation_errors)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                RETURNING staged_po_id
                """,
                (
                    batch_id, resolved_customer_id, po_number, _clean(first_row.get("Entity")),
                    _clean(first_row.get("FacilityId")), _clean(first_row.get("FacilityName")),
                    _clean(first_row.get("City")), po_created_at, po_modified_at,
                    _clean(first_row.get("Status")), _clean(first_row.get("SupplierCode")),
                    _clean(first_row.get("VendorName")), po_amount, expected_delivery_date,
                    po_expiry_date, _clean(first_row.get("OtbReferenceNumber")),
                    _clean(first_row.get("InternalExternalPo")), po_ageing,
                    _clean(first_row.get("BrandName")), _clean(first_row.get("ReferencePoNumber")),
                    "blocked" if po_blocked else "valid", json.dumps(po_level_errors),
                ),
            ).fetchone()
            staged_po_id = po_row["staged_po_id"]

        for source_row_number, row in indexed_rows:
            line_errors = []
            if po_number is None:
                line_errors.append({"code": "missing_po_number", "field": "PoNumber", "message": "PoNumber is blank"})

            row_id = conn.execute(
                """
                INSERT INTO po_import_rows (batch_id, source_row_number, raw_data, validation_status, validation_errors)
                VALUES (?, ?, ?, 'valid', '[]'::jsonb)
                RETURNING row_id
                """,
                (batch_id, source_row_number, json.dumps(row)),
            ).fetchone()["row_id"]

            external_sku = _clean(row.get("SkuCode"))
            if not external_sku:
                line_errors.append({"code": "missing_sku", "field": "SkuCode", "message": "SkuCode is blank"})

            product_id = None
            if external_sku:
                resolved = catalog.resolve_customer_sku(conn, resolved_customer_id, external_sku)
                if resolved is None:
                    line_errors.append({
                        "code": "unmapped_customer_sku", "field": "SkuCode",
                        "message": "Customer SKU does not map to a Master Product", "value": external_sku,
                    })
                else:
                    product_id = resolved["product_id"]

            ordered_qty, err = _parse_decimal(row.get("OrderedQty"))
            if err:
                line_errors.append({"code": "invalid_number", "field": "OrderedQty", "message": err, "value": row.get("OrderedQty")})
            received_qty, err = _parse_decimal(row.get("ReceivedQty"))
            if err:
                line_errors.append({"code": "invalid_number", "field": "ReceivedQty", "message": err, "value": row.get("ReceivedQty")})
            balanced_qty, err = _parse_decimal(row.get("BalancedQty"))
            if err:
                line_errors.append({"code": "invalid_number", "field": "BalancedQty", "message": err, "value": row.get("BalancedQty")})
            tax, err = _parse_decimal(row.get("Tax"))
            if err:
                line_errors.append({"code": "invalid_number", "field": "Tax", "message": err, "value": row.get("Tax")})
            line_value_without_tax, err = _parse_decimal(row.get("PoLineValueWithoutTax"))
            if err:
                line_errors.append({"code": "invalid_number", "field": "PoLineValueWithoutTax", "message": err, "value": row.get("PoLineValueWithoutTax")})
            line_value_with_tax, err = _parse_decimal(row.get("PoLineValueWithTax"))
            if err:
                line_errors.append({"code": "invalid_number", "field": "PoLineValueWithTax", "message": err, "value": row.get("PoLineValueWithTax")})
            mrp, err = _parse_decimal(row.get("Mrp"))
            if err:
                line_errors.append({"code": "invalid_number", "field": "Mrp", "message": err, "value": row.get("Mrp")})
            unit_based_cost, err = _parse_decimal(row.get("UnitBasedCost"))
            if err:
                line_errors.append({"code": "invalid_number", "field": "UnitBasedCost", "message": err, "value": row.get("UnitBasedCost")})

            line_status = "blocked" if line_errors else "valid"

            # Mirror the same status/errors back onto the raw row -- it
            # was inserted first (raw data always captured, unconditionally)
            # but now that validation is known, po_import_rows should
            # reflect it too, not just staged_po_lines.
            if line_errors:
                conn.execute(
                    "UPDATE po_import_rows SET validation_status = ?, validation_errors = ? WHERE row_id = ?",
                    (line_status, json.dumps(line_errors), row_id),
                )

            conn.execute(
                """
                INSERT INTO staged_po_lines
                    (staged_po_id, raw_row_id, source_row_number, external_sku, external_sku_description,
                     product_id, category_id, ordered_qty, received_qty, balanced_qty, tax,
                     line_value_without_tax, line_value_with_tax, mrp, unit_based_cost,
                     validation_status, validation_errors)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    staged_po_id, row_id, source_row_number, external_sku,
                    _clean(row.get("SkuDescription")), product_id, _clean(row.get("CategoryId")),
                    ordered_qty, received_qty, balanced_qty, tax, line_value_without_tax,
                    line_value_with_tax, mrp, unit_based_cost, line_status, json.dumps(line_errors),
                ),
            )

            if line_errors and staged_po_id is not None:
                conn.execute(
                    "UPDATE staged_purchase_orders SET validation_status = 'blocked' WHERE staged_po_id = ?",
                    (staged_po_id,),
                )

    return {"batch_id": batch_id, "reused_existing_batch": False}


def review_status(validation_status, source_location_id, posted_po_id=None, duplicate_kind=None):
    """The UI-only review state derived from Phase 3's validation_status,
    whether a Drizzl source has been manually assigned, and (Phase 5)
    whether this staged PO has been posted to the official ledger -- see
    TECHNICAL_README.md. Deliberately not a stored column: 'posted' takes
    precedence over everything else once posted_po_id is set (posting is
    permanent -- a posted PO's validation_status/source can't un-post it);
    'blocked' means a data/product validation problem exists (source
    assignment does NOT override this); 'needs_source' means the data is
    clean but no Drizzl warehouse has been assigned yet; 'ready' means
    both are satisfied and it is eligible to be posted -- 'ready' never
    means posted, only postable."""
    if posted_po_id is not None:
        return "posted"
    if validation_status == "blocked":
        return "blocked"
    if duplicate_kind in {"exact_duplicate", "review_required", "reviewed_duplicate"}:
        return duplicate_kind
    if source_location_id is None:
        return "needs_source"
    return "ready"


def get_import_batch(conn, batch_id):
    row = conn.execute(
        """
        SELECT b.*, c.name AS customer_name
        FROM po_import_batches b
        JOIN customers c ON c.id = b.customer_id
        WHERE b.batch_id = ?
        """,
        (batch_id,),
    ).fetchone()
    return dict(row) if row else None


def list_staged_pos(conn, batch_id):
    """Each staged PO plus its line count, total ordered quantity, and
    derived review_status -- everything the batch review table needs."""
    rows = conn.execute(
        """
        SELECT p.*, COUNT(l.staged_line_id) AS line_count,
               COALESCE(SUM(l.ordered_qty), 0) AS total_ordered_qty,
               loc.name AS source_location_name
        FROM staged_purchase_orders p
        LEFT JOIN staged_po_lines l ON l.staged_po_id = p.staged_po_id
        LEFT JOIN locations loc ON loc.id = p.source_location_id
        WHERE p.batch_id = ?
        GROUP BY p.staged_po_id, loc.name
        ORDER BY p.staged_po_id
        """,
        (batch_id,),
    ).fetchall()
    result = []
    for r in rows:
        po = dict(r)
        import po_posting
        classification = po_posting.classify_existing_po(conn, po)
        po.update(classification)
        po["review_status"] = review_status(
            po["validation_status"], po["source_location_id"], po["posted_po_id"], classification["kind"]
        )
        result.append(po)
    return result


def batch_summary(conn, batch_id):
    """Server-derived counts for the batch review header -- never trust
    the browser for this."""
    pos = list_staged_pos(conn, batch_id)
    counts = {
        "ready": 0, "needs_source": 0, "blocked": 0, "posted": 0,
        "exact_duplicate": 0, "review_required": 0, "reviewed_duplicate": 0,
    }
    for po in pos:
        counts[po["review_status"]] += 1
    line_count = sum(po["line_count"] for po in pos)
    return {"orders": len(pos), "lines": line_count, **counts}


def revalidate_product_mappings(conn, batch_id):
    """Re-check unmapped customer SKUs after a mapping is added manually.

    This deliberately does not create Master Products or mappings and does not
    re-parse the source file. It only revisits staged, unposted lines using the
    authoritative customer-SKU mapping table, then recomputes each staged PO's
    validation status from its own header errors plus its line statuses.
    """
    batch = get_import_batch(conn, batch_id)
    if batch is None:
        raise ValueError(f"PO import batch {batch_id} does not exist.")

    lines = conn.execute(
        """
        SELECT l.staged_line_id, l.raw_row_id, l.external_sku,
               l.validation_errors, p.customer_id
        FROM staged_po_lines l
        JOIN staged_purchase_orders p ON p.staged_po_id = l.staged_po_id
        WHERE p.batch_id = ? AND p.posted_po_id IS NULL
        """,
        (batch_id,),
    ).fetchall()

    changed = 0
    for line in lines:
        errors = [e for e in line["validation_errors"] if e.get("code") != "unmapped_customer_sku"]
        product_id = None
        if line["external_sku"]:
            resolved = catalog.resolve_customer_sku(conn, line["customer_id"], line["external_sku"])
            if resolved is not None:
                product_id = resolved["product_id"]
            else:
                errors.append({
                    "code": "unmapped_customer_sku",
                    "field": "SkuCode",
                    "message": "Customer SKU does not map to a Master Product",
                    "value": line["external_sku"],
                })
        status = "blocked" if errors else "valid"
        conn.execute(
            "UPDATE staged_po_lines SET product_id = ?, validation_status = ?, validation_errors = ? "
            "WHERE staged_line_id = ?",
            (product_id, status, json.dumps(errors), line["staged_line_id"]),
        )
        conn.execute(
            "UPDATE po_import_rows SET validation_status = ?, validation_errors = ? WHERE row_id = ?",
            (status, json.dumps(errors), line["raw_row_id"]),
        )
        changed += 1

    pos = conn.execute(
        "SELECT staged_po_id, validation_errors FROM staged_purchase_orders "
        "WHERE batch_id = ? AND posted_po_id IS NULL",
        (batch_id,),
    ).fetchall()
    for po in pos:
        has_blocked_line = conn.execute(
            "SELECT 1 FROM staged_po_lines WHERE staged_po_id = ? AND validation_status = 'blocked' LIMIT 1",
            (po["staged_po_id"],),
        ).fetchone() is not None
        status = "blocked" if po["validation_errors"] or has_blocked_line else "valid"
        conn.execute(
            "UPDATE staged_purchase_orders SET validation_status = ? WHERE staged_po_id = ?",
            (status, po["staged_po_id"]),
        )
    return changed


def list_recent_batches(conn, limit=20):
    rows = conn.execute(
        """
        SELECT b.*, c.name AS customer_name
        FROM po_import_batches b
        JOIN customers c ON c.id = b.customer_id
        ORDER BY b.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    result = []
    for r in rows:
        batch = dict(r)
        batch["summary"] = batch_summary(conn, batch["batch_id"])
        result.append(batch)
    return result


def get_staged_po(conn, staged_po_id):
    row = conn.execute(
        """
        SELECT p.*, loc.name AS source_location_name
        FROM staged_purchase_orders p
        LEFT JOIN locations loc ON loc.id = p.source_location_id
        WHERE p.staged_po_id = ?
        """,
        (staged_po_id,),
    ).fetchone()
    if row is None:
        return None
    po = dict(row)
    import po_posting
    classification = po_posting.classify_existing_po(conn, po)
    po.update(classification)
    po["review_status"] = review_status(
        po["validation_status"], po["source_location_id"], po["posted_po_id"], classification["kind"]
    )
    po["lines"] = [
        dict(r) for r in conn.execute(
            """
            SELECT l.*, mp.barcode AS master_barcode, mp.product_name AS master_product_name
            FROM staged_po_lines l
            LEFT JOIN master_products mp ON mp.product_id = l.product_id
            WHERE l.staged_po_id = ?
            ORDER BY l.staged_line_id
            """,
            (staged_po_id,),
        ).fetchall()
    ]
    return po


def get_staged_po_raw_rows(conn, staged_po_id):
    """The original CSV rows behind this staged PO's lines, read-only --
    for the detail page's 'view raw source rows' section."""
    return [
        dict(r) for r in conn.execute(
            """
            SELECT r.row_id, r.source_row_number, r.raw_data
            FROM staged_po_lines l
            JOIN po_import_rows r ON r.row_id = l.raw_row_id
            WHERE l.staged_po_id = ?
            ORDER BY r.source_row_number
            """,
            (staged_po_id,),
        ).fetchall()
    ]


def assign_source_location(conn, batch_id, staged_po_ids, source_location_id):
    """Assigns source_location_id to every staged PO in staged_po_ids, all
    of which must belong to batch_id. Atomic and strict: rejects the whole
    operation (no partial update) if the location doesn't exist, any
    staged_po_id is unknown or belongs to a different batch, or any of
    them has already been posted to the official ledger (Phase 5) -- once
    posted_po_id is set, the reviewed source is what got written into the
    official PO, and silently changing it here would desync the two
    without any trace. Only ever touches staged_purchase_orders -- never
    the official ledger. Caller owns commit/rollback, consistent with
    stage_po_csv()."""
    staged_po_ids = list(staged_po_ids)
    if not staged_po_ids:
        raise ValueError("No staged purchase orders were selected.")

    location = conn.execute("SELECT id FROM locations WHERE id = ?", (source_location_id,)).fetchone()
    if location is None:
        raise ValueError(f"Location id {source_location_id} does not exist.")

    placeholders = ",".join(["?"] * len(staged_po_ids))
    rows = conn.execute(
        f"SELECT staged_po_id, batch_id, posted_po_id FROM staged_purchase_orders WHERE staged_po_id IN ({placeholders})",
        tuple(staged_po_ids),
    ).fetchall()
    found_ids = {r["staged_po_id"] for r in rows}
    missing = set(staged_po_ids) - found_ids
    if missing:
        raise ValueError(f"Staged PO id(s) {sorted(missing)} do not exist.")
    wrong_batch = {r["staged_po_id"] for r in rows if r["batch_id"] != batch_id}
    if wrong_batch:
        raise ValueError(f"Staged PO id(s) {sorted(wrong_batch)} do not belong to this batch.")
    already_posted = {r["staged_po_id"] for r in rows if r["posted_po_id"] is not None}
    if already_posted:
        raise ValueError(
            f"Staged PO id(s) {sorted(already_posted)} have already been posted to the official "
            "ledger and can no longer have their source warehouse changed here."
        )

    conn.execute(
        f"UPDATE staged_purchase_orders SET source_location_id = ? WHERE staged_po_id IN ({placeholders})",
        (source_location_id, *staged_po_ids),
    )
    return len(staged_po_ids)

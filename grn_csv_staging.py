"""
GRN CSV staging (Phase 6).

Stages a bulk Scootsy-style GRN CSV export into grn_import_batches /
grn_import_rows / staged_grns / staged_grn_lines / staged_grn_line_source_rows,
normalizes raw rows into physical receipt lines, and verifies each staged
GRN against an official PO. Nothing here ever touches the official ledger
(grn_receipts, grn_line_items, inventory_movements) -- a staged GRN is not
an official GRN. Posting/review UI are later phases. See
PROJECT_HANDOFF.md.

Unlike the PO CSV (which names its customer via an Entity column), this
export identifies the SUPPLIER (Drizzl itself, via VendorName/SupplierCode)
but never the customer/buyer -- customer_id must always be supplied
explicitly by the caller, never inferred from VendorName, GRN prefix, or
"only one customer exists today".

Transaction ownership: these functions do NOT call conn.commit() or
conn.rollback() themselves -- the caller owns the transaction, same as
po_csv_staging.py.

Deliberately does not call ingest.py's upsert_grn()/_ensure_po_stub()/
_ensure_product() or record_movement() -- those belong to the legacy
PDF-GRN architecture and have official-ledger effects.
"""
import csv
import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

import catalog
import purchase_orders

# Structural columns without which a row/GRN can't be identified or
# normalized at all. Everything else is parsed when present, stored as
# NULL when absent.
REQUIRED_COLUMNS = [
    "GrnNumber", "PurchaseOrderNumber", "FacilityName", "SupplierCode",
    "VendorName", "InvoiceNumber", "SkuCode", "SkuDescription", "ReceivedQty",
]

# GRN-header fields repeated on every line of the same GrnNumber -- any two
# non-blank values disagreeing across the GRN's rows is a validation error.
# DnNumber is handled separately (blank is allowed alongside one non-blank
# value; only *multiple distinct* non-blank values block).
HEADER_FIELDS = [
    "PurchaseOrderNumber", "FacilityName", "SupplierCode", "VendorName",
    "InvoiceNumber", "InvoiceDate", "CreatedAtDate",
]

# Per-row fields (excluding DNQuantity/DNValue) that define whether two raw
# rows represent the same physical receipt line vs. a genuine separate lot.
# See _signature() and the module docstring in verify_grn_csv_staging.py
# for the worked examples this was validated against.
SIGNATURE_FIELDS = [
    "external_sku", "external_sku_description", "brand_name", "category",
    "received_qty", "grn_line_value_without_tax", "grn_line_value_with_tax",
    "lot_mrp", "lot_expiry_date", "cgst_rate", "cgst_amount", "sgst_rate",
    "sgst_amount", "igst_rate", "igst_amount", "cess_rate", "cess_amount",
    "additional_cess", "total_tax", "total_amount", "row_dn_number",
]


class FatalImportError(Exception):
    """Raised for file-level problems that must prevent the whole batch
    from being staged -- the caller should roll back on this."""


def _clean(value):
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def _normalize_ci(value):
    """Trimmed, case-insensitive comparison key -- never fuzzy."""
    cleaned = _clean(value)
    return cleaned.casefold() if cleaned is not None else None


def _parse_decimal(value):
    """Returns (parsed_value_or_None, error_or_None)."""
    value = _clean(value)
    if value is None:
        return None, None
    try:
        return Decimal(value), None
    except InvalidOperation:
        return None, f"could not parse {value!r} as a number"


def _parse_date(value):
    value = _clean(value)
    if value is None:
        return None, None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date(), None
    except ValueError:
        return None, f"could not parse {value!r} as a date (expected YYYY-MM-DD)"


def _parse_timestamp(value):
    value = _clean(value)
    if value is None:
        return None, None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S"), None
    except ValueError:
        return None, f"could not parse {value!r} as a timestamp (expected YYYY-MM-DD HH:MM:SS)"


def _file_sha256(csv_path):
    h = hashlib.sha256()
    with open(csv_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _err(code, message, field=None, value=None, severity="error"):
    e = {"code": code, "message": message, "severity": severity}
    if field is not None:
        e["field"] = field
    if value is not None:
        e["value"] = value
    return e


def _get_or_create_batch(conn, customer_id, filename, file_hash):
    existing = conn.execute(
        "SELECT batch_id FROM grn_import_batches WHERE customer_id = ? AND file_sha256 = ?",
        (customer_id, file_hash),
    ).fetchone()
    if existing:
        return existing["batch_id"], True

    row = conn.execute(
        """
        INSERT INTO grn_import_batches (customer_id, source_filename, file_sha256)
        VALUES (?, ?, ?)
        RETURNING batch_id
        """,
        (customer_id, filename, file_hash),
    ).fetchone()
    return row["batch_id"], False


def _validate_header_consistency(rows_for_grn):
    """Returns a list of structured errors if a GRN-header field disagrees
    across the rows sharing this GrnNumber. DnNumber is handled by
    _validate_dn_number() instead, not here."""
    errors = []
    if not rows_for_grn:
        return errors
    first = rows_for_grn[0]
    for field in HEADER_FIELDS:
        first_val = _clean(first.get(field))
        for r in rows_for_grn[1:]:
            val = _clean(r.get(field))
            if val != first_val:
                errors.append(_err(
                    "inconsistent_grn_metadata",
                    f"Rows for this GRN disagree on {field}",
                    field=field, value=[first_val, val],
                ))
                break
    return errors


def _validate_dn_number(rows_for_grn):
    """Blank DnNumber is normal (a rejection can exist with no DN issued
    yet). Multiple DISTINCT non-blank DN numbers within one GRN violates
    the "one Discrepancy Note per GRN" business rule and blocks. Returns
    (dn_number_or_None, errors)."""
    distinct = {_clean(r.get("DnNumber")) for r in rows_for_grn} - {None}
    if len(distinct) > 1:
        return None, [_err(
            "multiple_dn_numbers",
            f"Rows for this GRN reference more than one distinct DN number: {sorted(distinct)}",
            field="DnNumber", value=sorted(distinct),
        )]
    return (next(iter(distinct)) if distinct else None), []


def _parse_line_fields(row):
    """Parses every known per-row field. Returns (fields_dict, errors) --
    fields_dict always has every key (None where absent/unparseable), so a
    malformed row still gets a normalized object retained where possible,
    per PROJECT_HANDOFF.md."""
    fields = {}
    errors = []

    fields["external_sku"] = _clean(row.get("SkuCode"))
    if not fields["external_sku"]:
        errors.append(_err("missing_sku", "SkuCode is blank", field="SkuCode"))
    fields["external_sku_description"] = _clean(row.get("SkuDescription"))
    fields["brand_name"] = _clean(row.get("BrandName"))
    fields["category"] = _clean(row.get("Category"))
    fields["row_dn_number"] = _clean(row.get("DnNumber"))

    decimal_fields = [
        ("received_qty", "ReceivedQty"), ("dn_quantity", "DNQuantity"), ("dn_value", "DNValue"),
        ("grn_line_value_without_tax", "GrnLineValueWithoutTax"),
        ("grn_line_value_with_tax", "GrnLineValueWithTax"),
        ("lot_mrp", "LotMrp"),
        ("cgst_rate", "CgstRate"), ("cgst_amount", "CgstAmount"),
        ("sgst_rate", "SgstRate"), ("sgst_amount", "SgstAmount"),
        ("igst_rate", "IgstRate"), ("igst_amount", "IgstAmount"),
        ("cess_rate", "CessRate"), ("cess_amount", "CessAmount"),
        ("additional_cess", "AdditionalCess"),
        ("total_tax", "TotalTax"), ("total_amount", "TotalAmount"),
    ]
    for out_key, csv_col in decimal_fields:
        parsed, err = _parse_decimal(row.get(csv_col))
        fields[out_key] = parsed
        if err:
            errors.append(_err("invalid_number", err, field=csv_col, value=row.get(csv_col)))

    if fields["received_qty"] is None and _clean(row.get("ReceivedQty")) is not None:
        pass  # already flagged above by the invalid_number check
    elif fields["received_qty"] is None:
        errors.append(_err("missing_received_qty", "ReceivedQty is blank", field="ReceivedQty"))
    elif fields["received_qty"] < 0:
        errors.append(_err("negative_quantity", "ReceivedQty is negative", field="ReceivedQty", value=str(fields["received_qty"])))

    if fields["dn_quantity"] is not None and fields["dn_quantity"] < 0:
        errors.append(_err("negative_quantity", "DNQuantity is negative", field="DNQuantity", value=str(fields["dn_quantity"])))

    lot_expiry, err = _parse_date(row.get("LotExpiryDate"))
    fields["lot_expiry_date"] = lot_expiry
    if err:
        errors.append(_err("invalid_date", err, field="LotExpiryDate", value=row.get("LotExpiryDate")))

    return fields, errors


def _signature(fields):
    # Decimal equality/hashing is already value-based regardless of
    # exponent or trailing zeros (Decimal('4032.00') == Decimal('4032.0')
    # and they hash identically), so no extra normalization is needed here.
    return tuple(fields[k] for k in SIGNATURE_FIELDS)


def _dn_pair(fields):
    qty = fields["dn_quantity"] if fields["dn_quantity"] is not None else Decimal(0)
    val = fields["dn_value"] if fields["dn_value"] is not None else Decimal(0)
    return (qty, val)


def _normalize_candidates(candidates):
    """Groups a GRN's per-row candidates (each: fields, errors, raw_row_id)
    by physical-receipt signature and classifies each group into Cases
    A-D. Returns a list of normalized-line dicts:
        {fields, errors (list), raw_row_ids (list)}
    See the verification suites for synthetic Case A-D examples, including
    duplicate-DN collapse and multi-lot preservation."""
    groups = {}
    order = []
    for c in candidates:
        sig = _signature(c["fields"])
        if sig not in groups:
            groups[sig] = []
            order.append(sig)
        groups[sig].append(c)

    normalized = []
    for sig in order:
        members = groups[sig]
        base = dict(members[0]["fields"])
        errors = list(members[0]["errors"])
        for m in members[1:]:
            errors.extend(m["errors"])
        raw_row_ids = [m["raw_row_id"] for m in members]

        distinct_pairs = {_dn_pair(m["fields"]) for m in members}
        zero_pair = (Decimal(0), Decimal(0))

        if len(members) == 1:
            # Case A: single raw row, use its DN values as-is.
            pass
        elif len(distinct_pairs) == 1:
            # Case C: exact duplicate representations -- collapse, warn.
            errors.append(_err(
                "duplicate_source_row_collapsed",
                f"{len(members)} identical source rows were normalized to one receipt line",
                severity="warning",
            ))
        elif len(distinct_pairs) == 2 and zero_pair in distinct_pairs:
            positive_pair = next(p for p in distinct_pairs if p != zero_pair)
            if positive_pair[0] > 0:
                # Case B: one zero representation, one single positive
                # representation -- collapse to the positive one, never sum.
                positive_member = next(m for m in members if _dn_pair(m["fields"]) == positive_pair)
                base["dn_quantity"] = positive_member["fields"]["dn_quantity"]
                base["dn_value"] = positive_member["fields"]["dn_value"]
            else:
                errors.append(_err(
                    "ambiguous_dn_duplicate_rows",
                    "Multiple source rows for this receipt line have conflicting DN representations",
                    severity="error",
                ))
                base["dn_quantity"] = None
                base["dn_value"] = None
        else:
            # Case D: ambiguous -- quarantine, do not guess.
            errors.append(_err(
                "ambiguous_dn_duplicate_rows",
                "Multiple source rows for this receipt line have conflicting DN representations",
                severity="error",
            ))
            base["dn_quantity"] = None
            base["dn_value"] = None

        normalized.append({"fields": base, "errors": errors, "raw_row_ids": raw_row_ids})
    return normalized


def stage_grn_csv(conn, csv_path, customer_id, filename=None):
    """Stages one GRN CSV file for the given (required, never inferred)
    customer_id. Returns {"batch_id": ..., "reused_existing_batch": bool}.
    Raises FatalImportError for file-level problems -- the caller should
    roll back the transaction in that case. Does not commit/rollback
    itself; the caller owns the transaction."""
    if customer_id is None:
        raise FatalImportError("customer_id is required for GRN CSV staging -- it cannot be inferred from the file.")
    customer_row = conn.execute("SELECT id FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if customer_row is None:
        raise FatalImportError(f"customer_id {customer_id} does not exist")

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

    batch_id, reused = _get_or_create_batch(conn, customer_id, filename, file_hash)
    if reused:
        return {"batch_id": batch_id, "reused_existing_batch": True}

    grn_groups = {}
    for i, row in enumerate(rows, start=1):
        grn_number = _clean(row.get("GrnNumber"))
        grn_groups.setdefault(grn_number, []).append((i, row))

    for grn_number, indexed_rows in grn_groups.items():
        rows_only = [r for _, r in indexed_rows]

        # Insert every raw row unconditionally first -- raw_data is never
        # lost even if the GRN itself can't be identified/normalized.
        row_ids = []
        for source_row_number, row in indexed_rows:
            row_id = conn.execute(
                """
                INSERT INTO grn_import_rows (batch_id, source_row_number, raw_data, validation_status, validation_errors)
                VALUES (?, ?, ?, 'valid', '[]'::jsonb)
                RETURNING row_id
                """,
                (batch_id, source_row_number, json.dumps(row)),
            ).fetchone()["row_id"]
            row_ids.append(row_id)

        if grn_number is None:
            for row_id in row_ids:
                conn.execute(
                    "UPDATE grn_import_rows SET validation_status = 'blocked', validation_errors = ? WHERE row_id = ?",
                    (json.dumps([_err("missing_grn_number", "GrnNumber is blank", field="GrnNumber")]), row_id),
                )
            continue  # cannot be grouped into any staged_grns header

        header_errors = _validate_header_consistency(rows_only)
        dn_number, dn_errors = _validate_dn_number(rows_only)
        header_errors = header_errors + dn_errors

        first_row = rows_only[0]
        invoice_date, _ = _parse_date(first_row.get("InvoiceDate"))
        external_created_at, _ = _parse_timestamp(first_row.get("CreatedAtDate"))

        staged_grn_id = conn.execute(
            """
            INSERT INTO staged_grns
                (batch_id, customer_id, external_grn_number, external_po_number,
                 facility_name, supplier_code, vendor_name, invoice_number,
                 invoice_date, external_created_at, dn_number,
                 validation_status, validation_errors)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING staged_grn_id
            """,
            (
                batch_id, customer_id, grn_number, _clean(first_row.get("PurchaseOrderNumber")),
                _clean(first_row.get("FacilityName")), _clean(first_row.get("SupplierCode")),
                _clean(first_row.get("VendorName")), _clean(first_row.get("InvoiceNumber")),
                invoice_date, external_created_at, dn_number,
                "blocked" if header_errors else "valid", json.dumps(header_errors),
            ),
        ).fetchone()["staged_grn_id"]

        candidates = []
        for (source_row_number, row), row_id in zip(indexed_rows, row_ids):
            fields, errors = _parse_line_fields(row)
            resolved_product_id = None
            if fields["external_sku"]:
                resolved = catalog.resolve_customer_sku(conn, customer_id, fields["external_sku"])
                if resolved is None:
                    errors.append(_err(
                        "unmapped_customer_sku", "Customer SKU does not map to a Master Product",
                        field="SkuCode", value=fields["external_sku"],
                    ))
                else:
                    resolved_product_id = resolved["product_id"]
            fields["product_id"] = resolved_product_id
            candidates.append({"fields": fields, "errors": errors, "raw_row_id": row_id})

        any_line_blocked = False
        for line in _normalize_candidates(candidates):
            f = line["fields"]
            errors = line["errors"]
            line_status = "blocked" if any(e["severity"] == "error" for e in errors) else "valid"
            if line_status == "blocked":
                any_line_blocked = True

            line_id = conn.execute(
                """
                INSERT INTO staged_grn_lines
                    (staged_grn_id, external_sku, external_sku_description, product_id,
                     brand_name, category, received_qty, dn_quantity, dn_value,
                     grn_line_value_without_tax, grn_line_value_with_tax, lot_mrp,
                     lot_expiry_date, cgst_rate, cgst_amount, sgst_rate, sgst_amount,
                     igst_rate, igst_amount, cess_rate, cess_amount, additional_cess,
                     total_tax, total_amount, validation_status, validation_errors)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING staged_grn_line_id
                """,
                (
                    staged_grn_id, f["external_sku"], f["external_sku_description"], f["product_id"],
                    f["brand_name"], f["category"], f["received_qty"], f["dn_quantity"], f["dn_value"],
                    f["grn_line_value_without_tax"], f["grn_line_value_with_tax"], f["lot_mrp"],
                    f["lot_expiry_date"], f["cgst_rate"], f["cgst_amount"], f["sgst_rate"], f["sgst_amount"],
                    f["igst_rate"], f["igst_amount"], f["cess_rate"], f["cess_amount"], f["additional_cess"],
                    f["total_tax"], f["total_amount"], line_status, json.dumps(errors),
                ),
            ).fetchone()["staged_grn_line_id"]

            for raw_row_id in line["raw_row_ids"]:
                conn.execute(
                    "INSERT INTO staged_grn_line_source_rows (staged_grn_line_id, raw_row_id) VALUES (?, ?)",
                    (line_id, raw_row_id),
                )
                if line_status == "blocked":
                    conn.execute(
                        "UPDATE grn_import_rows SET validation_status = 'blocked', validation_errors = ? WHERE row_id = ?",
                        (json.dumps(errors), raw_row_id),
                    )

        if any_line_blocked:
            conn.execute(
                "UPDATE staged_grns SET validation_status = 'blocked' WHERE staged_grn_id = ?",
                (staged_grn_id,),
            )

    return {"batch_id": batch_id, "reused_existing_batch": False}


def get_grn_import_batch(conn, batch_id):
    row = conn.execute(
        """
        SELECT b.*, c.name AS customer_name
        FROM grn_import_batches b
        JOIN customers c ON c.id = b.customer_id
        WHERE b.batch_id = ?
        """,
        (batch_id,),
    ).fetchone()
    return dict(row) if row else None


def grn_review_status(validation_status, po_verification_status, posted_grn_id=None):
    """The UI-only review state derived from Phase 6/7's two independent
    status fields plus (Phase 8) whether this staged GRN has been posted
    -- never stored, never overwrites any of the three underlying
    fields. 'posted' takes precedence over everything else once
    posted_grn_id is set -- posting is permanent, a posted record's
    validation_status/po_verification_status can no longer change it (see
    validate_staged_grn(), which now refuses to touch a posted record).
    'quarantined' means either the intrinsic staging/normalization
    findings are blocked (e.g. an unmapped SKU or an ambiguous duplicate-
    DN row) OR PO verification hasn't succeeded (missing/voided/
    mismatched PO, over-receipt, etc.) -- either is sufficient to keep a
    GRN from being postable. 'verified' means both layers are clean and
    it is eligible to be posted -- 'verified' never means posted, only
    postable, until posted_grn_id is actually set."""
    if posted_grn_id is not None:
        return "posted"
    if validation_status == "blocked":
        return "quarantined"
    if po_verification_status != "verified":
        return "quarantined"
    return "verified"


def grn_display_status(validation_status, po_verification_status, posted_grn_id=None,
                       validation_errors=None, po_verification_errors=None):
    """Specific operator-facing reason while preserving review_status as
    the stable postability contract used by posting code and tests."""
    if posted_grn_id is not None:
        return "posted"
    if validation_status == "blocked":
        return "data_error"
    codes = {
        e.get("code") for e in (po_verification_errors or [])
        if e.get("severity", "error") == "error"
    }
    if "official_grn_already_exists" in codes:
        return "grn_already_posted"
    if "official_po_not_found" in codes or "external_po_number_missing" in codes:
        return "po_not_found"
    if "duplicate_grn_in_other_batch" in codes:
        return "duplicate_other_batch"
    if po_verification_status != "verified":
        return "review_required"
    return "ready_to_post"


def _grn_comparison_totals(conn, staged_grn_id):
    """Ordered/received/discrepancy totals for the batch review table's
    summary columns -- computed from get_grn_po_comparison() (normalized
    lines, never raw rows), restricted to rows that actually matched a PO
    line (excludes not_on_po/sku_mismatch rows, which have no real ordered
    baseline to sum against). Returns None if no PO comparison is
    available yet (no official_po_id, or a legacy PO block)."""
    comparison = get_grn_po_comparison(conn, staged_grn_id)
    if not comparison:
        return None
    matched = [r for r in comparison if r["quantity_status"] in ("exact", "short", "over")]
    if not matched:
        return None
    return {
        "total_ordered_qty": sum(r["ordered_qty"] for r in matched),
        "total_computed_discrepancy_qty": sum(r["computed_discrepancy_qty"] for r in matched),
    }


def list_staged_grns(conn, batch_id):
    """Each staged GRN plus its normalized line count, normalized received
    total (NEVER a raw-row sum -- see staged_grn_lines), matched official
    PO number and Drizzl source warehouse (read-only, resolved through the
    PO -- a GRN never has its own source), and derived review_status."""
    rows = conn.execute(
        """
        SELECT g.*, COUNT(l.staged_grn_line_id) AS line_count,
               COALESCE(SUM(l.received_qty), 0) AS total_received_qty,
               po.po_number AS official_po_number,
               loc.name AS official_source_location_name
        FROM staged_grns g
        LEFT JOIN staged_grn_lines l ON l.staged_grn_id = g.staged_grn_id
        LEFT JOIN purchase_orders po ON po.po_id = g.official_po_id
        LEFT JOIN locations loc ON loc.id = po.source_location_id
        WHERE g.batch_id = ?
        GROUP BY g.staged_grn_id, po.po_number, loc.name
        ORDER BY g.staged_grn_id
        """,
        (batch_id,),
    ).fetchall()
    result = []
    for r in rows:
        g = dict(r)
        g["review_status"] = grn_review_status(g["validation_status"], g["po_verification_status"], g["posted_grn_id"])
        g["display_status"] = grn_display_status(
            g["validation_status"], g["po_verification_status"], g["posted_grn_id"],
            g["validation_errors"], g["po_verification_errors"],
        )
        totals = _grn_comparison_totals(conn, g["staged_grn_id"])
        g["total_ordered_qty"] = totals["total_ordered_qty"] if totals else None
        g["total_computed_discrepancy_qty"] = totals["total_computed_discrepancy_qty"] if totals else None
        all_errors = list(g["validation_errors"]) + list(g["po_verification_errors"])
        primary_issue = next((e["message"] for e in all_errors if e.get("severity") == "error"), None)
        g["primary_issue"] = primary_issue
        result.append(g)
    return result


def get_grn_batch_summary(conn, batch_id):
    """Server-derived counts for the batch review header -- never trust
    the browser for this."""
    grns = list_staged_grns(conn, batch_id)
    counts = {
        "ready_to_post": 0, "po_not_found": 0, "grn_already_posted": 0,
        "duplicate_other_batch": 0, "review_required": 0, "data_error": 0,
        "posted": 0, "verified": 0, "quarantined": 0,
    }
    for g in grns:
        counts[g["display_status"]] += 1
        if g["review_status"] in ("verified", "quarantined"):
            counts[g["review_status"]] += 1
    raw_rows = conn.execute("SELECT COUNT(*) AS n FROM grn_import_rows WHERE batch_id = ?", (batch_id,)).fetchone()["n"]
    line_count = sum(g["line_count"] for g in grns)
    return {"grns": len(grns), "raw_rows": raw_rows, "lines": line_count, **counts}


def list_recent_grn_batches(conn, limit=20):
    rows = conn.execute(
        """
        SELECT b.*, c.name AS customer_name
        FROM grn_import_batches b
        JOIN customers c ON c.id = b.customer_id
        ORDER BY b.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    result = []
    for r in rows:
        batch = dict(r)
        batch["summary"] = get_grn_batch_summary(conn, batch["batch_id"])
        result.append(batch)
    return result


def get_staged_grn(conn, staged_grn_id):
    row = conn.execute("SELECT * FROM staged_grns WHERE staged_grn_id = ?", (staged_grn_id,)).fetchone()
    if row is None:
        return None
    grn = dict(row)
    grn["review_status"] = grn_review_status(grn["validation_status"], grn["po_verification_status"], grn["posted_grn_id"])
    grn["display_status"] = grn_display_status(
        grn["validation_status"], grn["po_verification_status"], grn["posted_grn_id"],
        grn["validation_errors"], grn["po_verification_errors"],
    )
    grn["lines"] = get_staged_grn_lines(conn, staged_grn_id)
    return grn


def get_staged_grn_lines(conn, staged_grn_id):
    lines = [
        dict(r) for r in conn.execute(
            """
            SELECT l.*, mp.barcode AS master_barcode, mp.product_name AS master_product_name
            FROM staged_grn_lines l
            LEFT JOIN master_products mp ON mp.product_id = l.product_id
            WHERE l.staged_grn_id = ?
            ORDER BY l.staged_grn_line_id
            """,
            (staged_grn_id,),
        ).fetchall()
    ]
    for line in lines:
        line["source_rows"] = get_line_source_rows(conn, line["staged_grn_line_id"])
    return lines


def get_line_source_rows(conn, staged_grn_line_id):
    """The raw CSV row(s) that produced exactly this one normalized line,
    read-only -- most useful for the collapsed duplicate-DN-representation
    cases (2 raw rows -> 1 line), where the operator needs to see both
    original rows without them ever being summed for anything
    operational."""
    return [
        dict(r) for r in conn.execute(
            """
            SELECT r.row_id, r.source_row_number, r.raw_data
            FROM staged_grn_line_source_rows s
            JOIN grn_import_rows r ON r.row_id = s.raw_row_id
            WHERE s.staged_grn_line_id = ?
            ORDER BY r.source_row_number
            """,
            (staged_grn_line_id,),
        ).fetchall()
    ]


def get_staged_grn_raw_rows(conn, staged_grn_id):
    """Every raw CSV row belonging to this GRN (across all its normalized
    lines), read-only -- for a GRN-level 'view all raw source rows'
    section, mirroring po_csv_staging.get_staged_po_raw_rows()."""
    return [
        dict(r) for r in conn.execute(
            """
            SELECT DISTINCT r.row_id, r.source_row_number, r.raw_data
            FROM staged_grn_lines l
            JOIN staged_grn_line_source_rows s ON s.staged_grn_line_id = l.staged_grn_line_id
            JOIN grn_import_rows r ON r.row_id = s.raw_row_id
            WHERE l.staged_grn_id = ?
            ORDER BY r.source_row_number
            """,
            (staged_grn_id,),
        ).fetchall()
    ]


def _grn_po_comparison_rows(conn, staged_grn_id, po_number):
    """Core of get_grn_po_comparison(): one row per (product_id,
    external_sku) combination found on the PO and/or the (valid) GRN
    lines. Requires the PO to have no legacy (product_id IS NULL) lines --
    callers must check that first (see validate_staged_grn())."""
    po_rows = conn.execute(
        """
        SELECT product_id, external_sku, SUM(qty)::numeric AS ordered_qty
        FROM po_line_items
        WHERE po_number = ?
        GROUP BY product_id, external_sku
        """,
        (po_number,),
    ).fetchall()
    po_by_key = {(r["product_id"], r["external_sku"]): r["ordered_qty"] for r in po_rows}
    po_products = {k[0] for k in po_by_key}

    grn_rows = conn.execute(
        """
        SELECT product_id, external_sku,
               SUM(received_qty) AS received_qty,
               SUM(COALESCE(dn_quantity, 0)) AS dn_quantity
        FROM staged_grn_lines
        WHERE staged_grn_id = ? AND validation_status = 'valid'
        GROUP BY product_id, external_sku
        """,
        (staged_grn_id,),
    ).fetchall()
    grn_by_key = {(r["product_id"], r["external_sku"]): r for r in grn_rows}

    all_keys = set(po_by_key) | set(grn_by_key)
    results = []
    for key in all_keys:
        product_id, external_sku = key
        in_po = key in po_by_key
        in_grn = key in grn_by_key
        ordered_qty = po_by_key.get(key, Decimal(0)) or Decimal(0)
        received_qty = grn_by_key[key]["received_qty"] if in_grn else Decimal(0)
        source_dn_quantity = grn_by_key[key]["dn_quantity"] if in_grn else None

        if not in_po:
            quantity_status = "sku_mismatch" if product_id in po_products else "not_on_po"
        elif not in_grn:
            quantity_status = "short" if ordered_qty > 0 else "exact"
        elif received_qty > ordered_qty:
            quantity_status = "over"
        elif received_qty == ordered_qty:
            quantity_status = "exact"
        else:
            quantity_status = "short"

        mp = conn.execute(
            "SELECT barcode, product_name FROM master_products WHERE product_id = ?", (product_id,)
        ).fetchone()
        results.append({
            "product_id": product_id,
            "barcode": mp["barcode"] if mp else None,
            "product_name": mp["product_name"] if mp else None,
            "external_sku": external_sku,
            "ordered_qty": ordered_qty,
            "received_qty": received_qty,
            "computed_discrepancy_qty": ordered_qty - received_qty,
            "source_dn_quantity": source_dn_quantity,
            "quantity_status": quantity_status,
        })
    results.sort(key=lambda r: (r["product_name"] or "", r["external_sku"] or ""))
    return results


def get_grn_po_comparison(conn, staged_grn_id):
    """Per-(product_id, external_sku) comparison of this staged GRN
    against its already-resolved official_po_id. Returns [] if no
    official PO is resolved yet, or if the PO has any legacy line missing
    product_id (nothing safe to compare against -- see
    legacy_po_line_missing_product_identity)."""
    grn = conn.execute("SELECT official_po_id FROM staged_grns WHERE staged_grn_id = ?", (staged_grn_id,)).fetchone()
    if grn is None or grn["official_po_id"] is None:
        return []
    po = conn.execute("SELECT po_number FROM purchase_orders WHERE po_id = ?", (grn["official_po_id"],)).fetchone()
    if po is None:
        return []
    legacy = conn.execute(
        "SELECT 1 FROM po_line_items WHERE po_number = ? AND product_id IS NULL LIMIT 1", (po["po_number"],)
    ).fetchone()
    if legacy is not None:
        return []
    return _grn_po_comparison_rows(conn, staged_grn_id, po["po_number"])


def validate_staged_grn(conn, staged_grn_id):
    """Recomputes ONLY po_verification_status/po_verification_errors --
    never touches validation_status/validation_errors (the intrinsic
    parsing/normalization findings from staging time). Pure function of
    current data: safe to call repeatedly, including after a previously-
    missing official PO has since been posted. Caller owns commit."""
    staged_grn = conn.execute("SELECT * FROM staged_grns WHERE staged_grn_id = ?", (staged_grn_id,)).fetchone()
    if staged_grn is None:
        raise ValueError(f"Staged GRN id {staged_grn_id} does not exist.")

    # Phase 8: a posted staged GRN is an immutable audit snapshot -- its
    # official_po_id/po_verification_status must never move out from
    # under the official grn_receipts row it already produced (and,
    # concretely, re-running the official_grn_already_exists check below
    # on a posted record would incorrectly flag it against its OWN
    # official GRN). Return the current stored state untouched.
    if staged_grn["posted_grn_id"] is not None:
        return {
            "official_po_id": staged_grn["official_po_id"],
            "po_verification_status": staged_grn["po_verification_status"],
            "po_verification_errors": staged_grn["po_verification_errors"],
        }

    errors = []
    official_po_id = None

    if not staged_grn["external_po_number"]:
        errors.append(_err("external_po_number_missing", "This GRN has no PurchaseOrderNumber to match against."))
    else:
        po = purchase_orders.get_po_by_customer_and_number(conn, staged_grn["customer_id"], staged_grn["external_po_number"])
        if po is None:
            errors.append(_err("official_po_not_found", f"No official PO {staged_grn['external_po_number']!r} exists for this customer."))
        else:
            official_po_id = po["po_id"]
            if po["voided"]:
                errors.append(_err("official_po_voided", "The matched official PO has been voided."))
            if po["source_location_id"] is None:
                errors.append(_err("official_po_source_missing", "The matched official PO has no Drizzl source warehouse assigned."))

            legacy = conn.execute(
                "SELECT 1 FROM po_line_items WHERE po_number = ? AND product_id IS NULL LIMIT 1", (po["po_number"],)
            ).fetchone()
            if legacy is not None:
                errors.append(_err(
                    "legacy_po_line_missing_product_identity",
                    "The matched official PO has legacy line(s) with no canonical product identity -- "
                    "cannot safely verify products/quantities against it.",
                ))
            else:
                if staged_grn["facility_name"] and po["destination_facility_name"] and \
                        _normalize_ci(staged_grn["facility_name"]) != _normalize_ci(po["destination_facility_name"]):
                    errors.append(_err(
                        "destination_facility_mismatch",
                        f"GRN facility {staged_grn['facility_name']!r} does not match PO destination {po['destination_facility_name']!r}.",
                    ))
                if staged_grn["supplier_code"] and po["supplier_code"] and \
                        _normalize_ci(staged_grn["supplier_code"]) != _normalize_ci(po["supplier_code"]):
                    errors.append(_err(
                        "supplier_code_mismatch",
                        f"GRN supplier code {staged_grn['supplier_code']!r} does not match PO supplier code {po['supplier_code']!r}.",
                    ))
                if staged_grn["vendor_name"] and po["vendor_name"] and \
                        _normalize_ci(staged_grn["vendor_name"]) != _normalize_ci(po["vendor_name"]):
                    errors.append(_err(
                        "vendor_name_mismatch",
                        f"GRN vendor name {staged_grn['vendor_name']!r} does not match PO vendor name {po['vendor_name']!r}.",
                    ))

                for row in _grn_po_comparison_rows(conn, staged_grn_id, po["po_number"]):
                    if row["quantity_status"] == "not_on_po":
                        errors.append(_err(
                            "grn_product_not_on_po",
                            f"GRN product {row['product_name'] or row['product_id']} (SKU {row['external_sku']}) "
                            "is not represented on the matched official PO at all.",
                        ))
                    elif row["quantity_status"] == "sku_mismatch":
                        errors.append(_err(
                            "grn_sku_not_on_po",
                            f"GRN SKU {row['external_sku']} for product {row['product_name'] or row['product_id']} "
                            "is not the SKU used on the matched official PO for that product.",
                        ))
                    elif row["quantity_status"] == "over":
                        errors.append(_err(
                            "received_quantity_exceeds_ordered",
                            f"Received {row['received_qty']} of {row['product_name'] or row['product_id']} "
                            f"(SKU {row['external_sku']}) exceeds the ordered {row['ordered_qty']}.",
                        ))

    # AND voided = 0 (Phase 11) -- matches grn_posting.py's own
    # official-GRN conflict check (relaxed in Phase 10 for the same
    # reason): a grn_number whose only history is an already-voided/
    # superseded row is free to be posted normally again. Without this
    # filter, a staged GRN could get permanently stuck BLOCKED with no
    # resolution path -- find_correction_target() only ever offers the
    # Correct/Replace action against a currently ACTIVE conflicting GRN
    # (see grn_posting.py), so a conflict against a merely-voided (never
    # superseded) row would have no active target to correct against.
    existing_official_grn = conn.execute(
        "SELECT 1 FROM grn_receipts WHERE grn_number = ? AND voided = 0", (staged_grn["external_grn_number"],)
    ).fetchone()
    if existing_official_grn is not None:
        errors.append(_err(
            "official_grn_already_exists",
            f"An official GRN {staged_grn['external_grn_number']!r} already exists.",
        ))

    other_batch_dupe = conn.execute(
        """
        SELECT 1 FROM staged_grns
        WHERE customer_id = ? AND external_grn_number = ? AND batch_id != ? AND staged_grn_id != ?
        """,
        (staged_grn["customer_id"], staged_grn["external_grn_number"], staged_grn["batch_id"], staged_grn_id),
    ).fetchone()
    if other_batch_dupe is not None:
        errors.append(_err(
            "duplicate_grn_in_other_batch",
            f"Another staged GRN {staged_grn['external_grn_number']!r} exists in a different import batch.",
        ))

    status = "blocked" if any(e["severity"] == "error" for e in errors) else "verified"
    conn.execute(
        "UPDATE staged_grns SET official_po_id = ?, po_verification_status = ?, po_verification_errors = ? WHERE staged_grn_id = ?",
        (official_po_id, status, json.dumps(errors), staged_grn_id),
    )
    return {"official_po_id": official_po_id, "po_verification_status": status, "po_verification_errors": errors}


def revalidate_grn_batch(conn, batch_id):
    """Re-runs validate_staged_grn() for every staged GRN in a batch --
    e.g. after a previously-missing official PO has since been posted.
    Caller owns commit."""
    staged_grn_ids = [r["staged_grn_id"] for r in conn.execute(
        "SELECT staged_grn_id FROM staged_grns WHERE batch_id = ? ORDER BY staged_grn_id", (batch_id,)
    ).fetchall()]
    return [validate_staged_grn(conn, sid) for sid in staged_grn_ids]

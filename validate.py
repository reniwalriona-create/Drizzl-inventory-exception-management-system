"""
Sanity checks run on every parsed document before it's trusted. None of
these block storage -- a document that fails a check still gets saved
(see ingest.py), it just also gets logged to ingestion_flags so a human
can go verify it instead of bad numbers entering silently.
"""


def _close_enough(a, b, min_tolerance=1.0, relative_tolerance=0.01):
    if a is None or b is None:
        return True
    tolerance = max(min_tolerance, relative_tolerance * max(abs(a), abs(b)))
    return abs(a - b) <= tolerance


def validate_po(parsed):
    issues = []
    if not parsed.get("po_number"):
        issues.append("Missing po_number")
    if not parsed.get("vendor_name"):
        issues.append("Missing vendor_name")
    if not parsed.get("po_date"):
        issues.append("Missing po_date")

    items = parsed.get("line_items") or []
    if not items:
        issues.append("No line items found")

    grand_total = parsed.get("grand_total")
    items_sum = sum(i.get("total") or 0 for i in items)
    if items and not _close_enough(grand_total, items_sum):
        issues.append(f"grand_total ({grand_total}) does not match sum of line item totals ({items_sum:.2f})")

    return issues


def validate_grn(parsed):
    issues = []
    if not parsed.get("grn_number"):
        issues.append("Missing grn_number")

    items = parsed.get("line_items") or []
    if not items:
        issues.append("No line items found")

    for i, item in enumerate(items, start=1):
        if not item.get("sku_code"):
            issues.append(f"Line item {i}: missing sku_code")
        if item.get("received_qty") is None:
            issues.append(f"Line item {i}: missing received_qty")

        unit_price, qty, taxable_value = item.get("unit_price"), item.get("received_qty"), item.get("taxable_value")
        if unit_price is not None and qty is not None:
            expected_taxable = unit_price * qty
            if not _close_enough(expected_taxable, taxable_value, relative_tolerance=0.02):
                issues.append(
                    f"Line item {i} (SKU {item.get('sku_code')}): unit_price x qty "
                    f"({expected_taxable:.2f}) does not match taxable_value ({taxable_value})"
                )

        expected_qty, received_qty = item.get("expected_qty"), item.get("received_qty")
        if expected_qty is not None and received_qty is not None and received_qty > expected_qty:
            issues.append(
                f"Line item {i} (SKU {item.get('sku_code')}): received_qty ({received_qty:.0f}) "
                f"exceeds expected_qty ({expected_qty:.0f}) -- more arrived than the delivery said to expect"
            )

    return issues


def validate_discrepancy_note(parsed):
    issues = []
    if not parsed.get("dn_number"):
        issues.append("Missing dn_number")

    items = parsed.get("line_items") or []
    if not items:
        issues.append("No line items found")

    dn_amt = parsed.get("dn_amt")
    items_amt_sum = sum(i.get("total") or 0 for i in items)
    if items and not _close_enough(dn_amt, items_amt_sum):
        issues.append(f"dn_amt ({dn_amt}) does not match sum of line item totals ({items_amt_sum:.2f})")

    total_dn_qty = parsed.get("total_dn_qty")
    items_qty_sum = sum(i.get("dn_qty") or 0 for i in items)
    if items and total_dn_qty is not None and total_dn_qty != items_qty_sum:
        issues.append(f"total_dn_qty ({total_dn_qty}) does not match sum of line item dn_qty ({items_qty_sum})")

    return issues


def validate_debit_note(parsed):
    issues = []
    if not parsed.get("note_number"):
        issues.append("Missing note_number")

    sub_total = parsed.get("sub_total")
    tax_amount = parsed.get("tax_amount")
    total_amount = parsed.get("total_amount")
    if sub_total is not None and tax_amount is not None:
        expected = sub_total + tax_amount
        if not _close_enough(expected, total_amount):
            issues.append(f"sub_total + tax_amount ({expected:.2f}) does not match total_amount ({total_amount})")

    items = parsed.get("line_items") or []
    items_amt_sum = sum(i.get("amount") or 0 for i in items)
    if items and not _close_enough(sub_total, items_amt_sum):
        issues.append(f"sub_total ({sub_total}) does not match sum of line item amounts ({items_amt_sum:.2f})")

    return issues


def record_flags(conn, document_type, document_id, issues, source_file=None):
    for issue in issues:
        conn.execute(
            """
            INSERT INTO ingestion_flags (document_type, document_id, issue, source_file)
            VALUES (?, ?, ?, ?)
            """,
            (document_type, document_id, issue, source_file),
        )

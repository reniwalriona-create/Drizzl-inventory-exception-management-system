"""Verifies debit_note_parser.py against a caller-supplied synthetic PDF."""
from debit_note_parser import parse_debit_note_pdf

EXPECTED = {
    "note_number": "SYN-DN-PDF-1001",
    "reference_number": "SYN-REF-PDF-1001",
    "discrepancy_type": "QDN",
    "note_date": "2026-07-11",
    "sub_total": 1080.0,
    "total_amount": 1512.0,
    "po_number": "SYN-PO-PDF-1001",
    "invoice_number": "SYN-INV-PDF-1001",
}

EXPECTED_ITEMS = [
    {"qty": 3.0, "rate": 360.0, "amount": 1080.0},
]


def run(path):
    result = parse_debit_note_pdf(path)
    failures = []

    for key, expected_val in EXPECTED.items():
        actual = result.get(key)
        if actual != expected_val:
            failures.append(f"  {key}: expected {expected_val!r}, got {actual!r}")

    items = result.get("line_items", [])
    if len(items) != len(EXPECTED_ITEMS):
        failures.append(f"  line_items count: expected {len(EXPECTED_ITEMS)}, got {len(items)}")
    else:
        for i, (actual_item, expected_item) in enumerate(zip(items, EXPECTED_ITEMS)):
            for key, expected_val in expected_item.items():
                if actual_item.get(key) != expected_val:
                    failures.append(f"  item[{i}].{key}: expected {expected_val!r}, got {actual_item.get(key)!r}")

    if failures:
        print(f"FAILED ({len(failures)} mismatch(es)):")
        print("\n".join(failures))
    else:
        print(f"PASSED — all {len(EXPECTED)} header fields and {len(EXPECTED_ITEMS)} line items match the source PDF exactly.")
    return not failures


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python verify_debit_note_parser.py <synthetic-debit-note.pdf>")
    path = sys.argv[1]
    ok = run(path)
    sys.exit(0 if ok else 1)

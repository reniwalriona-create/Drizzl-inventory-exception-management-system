"""Verifies po_parser.py against a caller-supplied synthetic PO PDF."""
from po_parser import parse_po_pdf

EXPECTED = {
    "po_number": "SYN-PO-PDF-1001",
    "po_date": "2026-08-06",
    "expected_delivery_date": "2026-08-21",
    "po_expiry_date": "2026-08-23",
    "vendor_name": "DRIZZL DEMO VENDOR",
    "vendor_gstin": "00SYNTHETIC0000",
    "facility_name": "DEMO FACILITY A",
    "grand_total": 10214.40,
}

EXPECTED_ITEMS = [
    {"item_code": "DEMO-SKU-001", "qty": 72.0, "mrp": 120.0, "total": 6048.0},
    {"item_code": "DEMO-SKU-003", "qty": 24.0, "mrp": 128.0, "total": 2150.4},
    {"item_code": "DEMO-SKU-002", "qty": 24.0, "mrp": 120.0, "total": 2016.0},
]

def run(path):
    result = parse_po_pdf(path)
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
        raise SystemExit("Usage: python verify_po_parser.py <synthetic-po.pdf>")
    path = sys.argv[1]
    ok = run(path)
    sys.exit(0 if ok else 1)

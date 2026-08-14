"""
Verifies grn_parser.py against the known correct values from the source GRN PDF
(DEMO000001), read manually. Run this after any change to grn_parser.py.
"""
from grn_parser import parse_grn_pdf

EXPECTED = {
    "grn_number": "DEMO000001",
    "grn_date": "2026-08-07",
    "po_number": "MBLPO426246",
    "po_date": "2026-07-28",
    "inbound_no": "MBL000551544",
    "create_date": "2026-08-07",
    "invoice_no": "GTA/00120/26-27",
    "invoice_date": "2026-07-28",
    "vendor_name": "DRIZZL DEMO VENDOR",
    "facility_name": "ECOM EXPRESS LIMITED",
}

EXPECTED_ITEMS = [
    {"sku_code": "DEMO-SKU-001", "lot_no": "MBL002770472", "expected_qty": 144.0, "received_qty": 144.0, "total": 12096.0},
    {"sku_code": "DEMO-SKU-003", "lot_no": "MBL002770477", "expected_qty": 72.0, "received_qty": 72.0, "total": 6048.0},
    {"sku_code": "DEMO-SKU-005", "lot_no": "MBL002770482", "expected_qty": 216.0, "received_qty": 216.0, "total": 18144.0},
    {"sku_code": "DEMO-SKU-002", "lot_no": "MBL002770487", "expected_qty": 48.0, "received_qty": 48.0, "total": 4032.0},
    {"sku_code": "DEMO-SKU-006", "lot_no": "MBL002770496", "expected_qty": 24.0, "received_qty": 24.0, "total": 1512.0},
]


def run(path):
    result = parse_grn_pdf(path)
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
    path = sys.argv[1] if len(sys.argv) > 1 else "GRN_DEMO000001 (1).pdf"
    ok = run(path)
    sys.exit(0 if ok else 1)

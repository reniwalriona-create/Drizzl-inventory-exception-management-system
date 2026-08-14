"""
Verifies discrepancy_note_parser.py against the known correct values from the
source PDF (DEMO-PR-1001), read manually. Run after any change to the parser.
"""
from discrepancy_note_parser import parse_discrepancy_note_pdf

EXPECTED = {
    "dn_number": "DEMO-PR-1001",
    "dn_date": "2026-08-09",
    "grn_number": "JCE000177512",
    "po_number": "JCEPO187170",
    "invoice_number": "GTA/00119/26-27",
    "grn_qty": 262.0,
    "grn_amt": 20013.0,
    "total_dn_qty": 2.0,
    "dn_amt": 147.0,
    "vendor_name": "DRIZZL DEMO VENDOR",
}

EXPECTED_ITEMS = [
    {"sku_code": "106-DEMO-SKU-006", "reason": "Damaged", "remarks": "DP WORLD-DAMAGE", "exp_qty": 96.0, "dn_qty": 1.0, "total": 63.0},
    {"sku_code": "106-DEMO-SKU-002", "reason": "Damaged", "remarks": "DP WORLD-DAMAGE", "exp_qty": 144.0, "dn_qty": 1.0, "total": 84.0},
]


def run(path):
    result = parse_discrepancy_note_pdf(path)
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
    path = sys.argv[1] if len(sys.argv) > 1 else "demo_discrepancy_note.pdf"
    ok = run(path)
    sys.exit(0 if ok else 1)

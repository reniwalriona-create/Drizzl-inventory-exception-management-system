"""
Phase 12: the one command that answers "is this project healthy?"

Runs every verify_*.py suite that's runnable in this environment as-is,
then verify_system_integrity.py's read-only audit of the live database,
and prints one final PASS/FAIL summary. Does not duplicate any suite's
own logic -- each one is just run as a subprocess and its exit code is
what decides pass/fail here.

Deliberately excludes verify_po_parser.py / verify_grn_parser.py /
verify_debit_note_parser.py because the public repository contains no PDF
fixtures. Run them manually with caller-supplied synthetic PDFs after touching
a parser.

Usage:
    ./.venv/bin/python verify_all.py

Exits 0 if every suite passed, 1 if any suite failed -- suitable as a
pre-deploy/CI gate.
"""
import subprocess
import sys
import time
from pathlib import Path

VERIFY_SUITES = [
    "verify_product_identity.py",
    "verify_po_identity.py",
    "verify_po_csv_staging.py",
    "verify_po_review_ui.py",
    "verify_po_posting.py",
    "verify_canonical_manual_movements.py",
    "verify_grn_csv_staging.py",
    "verify_grn_review_ui.py",
    "verify_grn_posting.py",
    "verify_synthetic_fixture_workflow.py",
    "verify_official_discrepancies.py",
    "verify_grn_correction.py",
    "verify_security.py",
    "verify_visualization_filters.py",
]

FINAL_INTEGRITY_CHECK = "verify_system_integrity.py"

ROOT = Path(__file__).parent
PYTHON = sys.executable


def run_one(script):
    start = time.time()
    result = subprocess.run([PYTHON, script], cwd=ROOT, capture_output=True, text=True)
    return result.returncode == 0, time.time() - start, result.stdout, result.stderr


def main():
    print("=" * 72)
    print("Drizzl Inventory -- full verification run")
    print("=" * 72)

    results = []
    for script in VERIFY_SUITES + [FINAL_INTEGRITY_CHECK]:
        print(f"\n>>> {script}")
        ok, elapsed, stdout, stderr = run_one(script)
        results.append((script, ok, elapsed))
        print(f"    {'PASS' if ok else 'FAIL'} ({elapsed:.1f}s)")
        if not ok:
            for line in (stdout + stderr).splitlines():
                print(f"    | {line}")

    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for script, ok, elapsed in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {script} ({elapsed:.1f}s)")

    failed = [s for s, ok, _ in results if not ok]
    print()
    if failed:
        print(f"FAILED: {len(failed)}/{len(results)} suite(s): {', '.join(failed)}")
        sys.exit(1)
    print(f"ALL {len(results)} SUITES PASSED.")
    print(
        "\nNote: verify_po_parser.py / verify_grn_parser.py / verify_debit_note_parser.py "
        "are not included above -- run them manually with caller-supplied synthetic PDFs "
        "after touching a parser."
    )
    sys.exit(0)


if __name__ == "__main__":
    main()

"""Stage Demo Commerce PR/discrepancy CSVs and classify existing GRN shortfall losses.

Classification never creates or changes movement quantities. The GRN posting
path already removed the full PO quantity; this module only records why the
shortfall existed.
"""
import csv
import hashlib
import json
from decimal import Decimal, InvalidOperation
from datetime import datetime
from pathlib import Path

REQUIRED = {"PrNumber", "PoNumber", "GrnNumber", "SkuCode", "TotalRejectedQty", "RejectedReasons"}


class FatalImportError(ValueError):
    pass


def _text(value):
    return (value or "").strip()


def _number(value):
    try:
        return Decimal(_text(value) or "0")
    except InvalidOperation:
        return None


def _cause(value):
    parts = [_text(part).capitalize() for part in _text(value).split(",") if _text(part)]
    return ", ".join(parts) or "Unspecified"


def _date(value):
    raw = _text(value)
    if not raw:
        return None
    for pattern in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def _resolve(conn, customer_id, row):
    sku = _text(row.get("SkuCode"))
    mapping = conn.execute(
        """SELECT cps.product_id, mp.active FROM customer_product_skus cps
           JOIN master_products mp ON mp.product_id=cps.product_id
           WHERE cps.customer_id=? AND cps.external_sku=? AND cps.active=TRUE""",
        (customer_id, sku),
    ).fetchone()
    if mapping is None:
        return None, None, None, "ignored", "Discontinued or unmapped SKU"

    grn = conn.execute(
        """SELECT grn_id, po_number FROM grn_receipts
           WHERE grn_number=? AND customer_id=? AND voided=0""",
        (_text(row.get("GrnNumber")), customer_id),
    ).fetchone()
    if grn is None:
        return mapping["product_id"], None, None, "blocked", "Official GRN not found"
    if _text(row.get("PoNumber")) and grn["po_number"] != _text(row.get("PoNumber")):
        return mapping["product_id"], grn["grn_id"], None, "blocked", "PO does not match official GRN"

    # The discrepancy workflow is document-led and independent from stock
    # posting internals. A matching official PO line and GRN are enough to
    # review the CSV. Newer GRNs may also have a linked unresolved-loss
    # movement; retain it so classification can label that movement, but do
    # not block older GRNs merely because they predate that ledger feature.
    po_line = conn.execute(
        """SELECT 1 FROM po_line_items
           WHERE po_number=? AND product_id=? AND external_sku=? LIMIT 1""",
        (grn["po_number"], mapping["product_id"], sku),
    ).fetchone()
    if po_line is None:
        return mapping["product_id"], grn["grn_id"], None, "blocked", "SKU is not on the matched PO"

    movements = conn.execute(
        """SELECT id, quantity FROM inventory_movements
           WHERE reference_type='grn_discrepancy' AND source_grn_id=?
             AND product_id=? AND voided=0""",
        (grn["grn_id"], mapping["product_id"]),
    ).fetchall()
    movement_id = movements[0]["id"] if len(movements) == 1 else None
    return mapping["product_id"], grn["grn_id"], movement_id, "ready", None


def _validate_quantities(conn, batch_id):
    """Block ready groups whose CSV DN total differs from PO minus GRN."""
    groups = conn.execute(
        """SELECT official_grn_id, product_id, external_sku,
                  SUM(rejected_qty) AS csv_qty
           FROM staged_discrepancy_lines
           WHERE batch_id=? AND review_status='ready'
           GROUP BY official_grn_id,product_id,external_sku""",
        (batch_id,),
    ).fetchall()
    for group in groups:
        expected_raw = conn.execute(
            """SELECT
                 COALESCE((SELECT SUM(pli.qty) FROM po_line_items pli
                           JOIN grn_receipts g ON g.po_number=pli.po_number
                           WHERE g.grn_id=? AND pli.product_id=? AND pli.external_sku=?),0)
                 -
                 COALESCE((SELECT SUM(gli.received_qty) FROM grn_line_items gli
                           WHERE gli.grn_id=? AND gli.product_id=? AND gli.external_sku=?),0)
                 AS qty""",
            (group["official_grn_id"], group["product_id"], group["external_sku"],
             group["official_grn_id"], group["product_id"], group["external_sku"]),
        ).fetchone()["qty"]
        expected = Decimal(str(expected_raw or 0))
        if expected <= 0 or abs(group["csv_qty"] - expected) > Decimal("0.000001"):
            message = (
                f"CSV rejected total {group['csv_qty']:g} does not match "
                f"the PO–GRN shortfall {expected:g}"
            )
            conn.execute(
                """UPDATE staged_discrepancy_lines
                   SET review_status='blocked', review_message=?
                   WHERE batch_id=? AND official_grn_id=? AND product_id=? AND external_sku=?""",
                (message, batch_id, group["official_grn_id"], group["product_id"], group["external_sku"]),
            )


def stage_csv(conn, file_path, customer_id, filename=None):
    path = Path(file_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    existing = conn.execute(
        "SELECT batch_id FROM discrepancy_import_batches WHERE customer_id=? AND file_sha256=?",
        (customer_id, digest),
    ).fetchone()
    if existing:
        return {"batch_id": existing["batch_id"], "reused": True}

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not REQUIRED.issubset(set(reader.fieldnames)):
            missing = sorted(REQUIRED - set(reader.fieldnames or []))
            raise FatalImportError("Missing required discrepancy columns: " + ", ".join(missing))
        rows = list(reader)
    if not rows:
        raise FatalImportError("The discrepancy CSV has no data rows.")

    batch_id = conn.execute(
        """INSERT INTO discrepancy_import_batches
           (customer_id, source_filename, file_sha256) VALUES (?, ?, ?) RETURNING batch_id""",
        (customer_id, filename or path.name, digest),
    ).fetchone()["batch_id"]
    for number, row in enumerate(rows, 2):
        rejected = _number(row.get("TotalRejectedQty"))
        rejected_amount = _number(row.get("TotalRejectedAmount"))
        completed_date = _date(row.get("CompletedDate"))
        product_id, grn_id, movement_id, status, message = _resolve(conn, customer_id, row)
        if rejected is None or rejected < 0:
            status, message = "blocked", "Rejected quantity is invalid"
        elif rejected == 0:
            status, message = "ignored", "No rejected units"
        elif _text(row.get("TotalRejectedAmount")) and (rejected_amount is None or rejected_amount < 0):
            status, message = "blocked", "Rejected amount is invalid"
        elif _text(row.get("CompletedDate")) and completed_date is None:
            status, message = "blocked", "Completed date is invalid"
        conn.execute(
            """INSERT INTO staged_discrepancy_lines
               (batch_id, source_row_number, raw_data, pr_number, po_number,
                grn_number, external_sku, product_id, accepted_qty, rejected_qty,
                rejected_amount, completed_date,
                rejected_reason, official_grn_id, discrepancy_movement_id,
                review_status, review_message)
               VALUES (?, ?, ?::jsonb, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, number, json.dumps(row), _text(row.get("PrNumber")), _text(row.get("PoNumber")),
             _text(row.get("GrnNumber")), _text(row.get("SkuCode")), product_id,
             _number(row.get("AcceptedQty")), rejected, rejected_amount, completed_date,
             _cause(row.get("RejectedReasons")),
             grn_id, movement_id, status, message),
        )

    # Compare the CSV's DN/rejected quantity directly with the official
    # document shortfall (PO ordered minus GRN received). This works for
    # both old and new GRNs and never requires a stock movement.
    _validate_quantities(conn, batch_id)
    return {"batch_id": batch_id, "reused": False}


def revalidate_batch(conn, batch_id):
    batch = conn.execute(
        "SELECT customer_id FROM discrepancy_import_batches WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if batch is None:
        raise ValueError("This discrepancy batch does not exist.")
    lines = conn.execute(
        "SELECT staged_line_id,raw_data FROM staged_discrepancy_lines WHERE batch_id=? AND classified_at IS NULL",
        (batch_id,),
    ).fetchall()
    for line in lines:
        row = line["raw_data"]
        rejected = _number(row.get("TotalRejectedQty"))
        rejected_amount = _number(row.get("TotalRejectedAmount"))
        completed_date = _date(row.get("CompletedDate"))
        product_id, grn_id, movement_id, status, message = _resolve(conn, batch["customer_id"], row)
        if rejected is None or rejected < 0:
            status, message = "blocked", "Rejected quantity is invalid"
        elif rejected == 0:
            status, message = "ignored", "No rejected units"
        elif _text(row.get("TotalRejectedAmount")) and (rejected_amount is None or rejected_amount < 0):
            status, message = "blocked", "Rejected amount is invalid"
        elif _text(row.get("CompletedDate")) and completed_date is None:
            status, message = "blocked", "Completed date is invalid"
        conn.execute(
            """UPDATE staged_discrepancy_lines
               SET product_id=?,official_grn_id=?,discrepancy_movement_id=?,
                   rejected_amount=?,completed_date=?,review_status=?,review_message=?
               WHERE staged_line_id=?""",
            (product_id, grn_id, movement_id, rejected_amount, completed_date,
             status, message, line["staged_line_id"]),
        )
    _validate_quantities(conn, batch_id)
    return len(lines)


def list_batches(conn):
    return conn.execute(
        """SELECT b.*, c.name AS customer_name,
           COUNT(l.staged_line_id) AS lines,
           COUNT(*) FILTER (WHERE l.review_status='ready') AS ready,
           COUNT(*) FILTER (WHERE l.review_status='blocked') AS blocked,
           COUNT(*) FILTER (WHERE l.review_status='ignored') AS ignored,
           COUNT(*) FILTER (WHERE l.classified_at IS NOT NULL) AS classified
           FROM discrepancy_import_batches b JOIN customers c ON c.id=b.customer_id
           LEFT JOIN staged_discrepancy_lines l ON l.batch_id=b.batch_id
           GROUP BY b.batch_id,c.name ORDER BY b.batch_id DESC"""
    ).fetchall()


def get_batch(conn, batch_id):
    batch = conn.execute(
        """SELECT b.*,c.name AS customer_name FROM discrepancy_import_batches b
           JOIN customers c ON c.id=b.customer_id WHERE b.batch_id=?""", (batch_id,)
    ).fetchone()
    lines = conn.execute(
        """SELECT l.*,mp.product_name FROM staged_discrepancy_lines l
           LEFT JOIN master_products mp ON mp.product_id=l.product_id
           WHERE l.batch_id=? ORDER BY l.source_row_number""", (batch_id,)
    ).fetchall()
    return batch, lines


def classify_ready(conn, batch_id):
    lines = conn.execute(
        """SELECT * FROM staged_discrepancy_lines
           WHERE batch_id=? AND review_status='ready' AND classified_at IS NULL
           ORDER BY staged_line_id FOR UPDATE""", (batch_id,)
    ).fetchall()
    by_movement = {}
    for line in lines:
        if line["discrepancy_movement_id"] is not None:
            by_movement.setdefault(line["discrepancy_movement_id"], []).append(line)
    for movement_id, grouped in by_movement.items():
        causes = sorted({line["rejected_reason"] for line in grouped})
        conn.execute(
            "UPDATE inventory_movements SET notes=? WHERE id=? AND reference_type='grn_discrepancy'",
            (", ".join(causes), movement_id),
        )
    # The staged classification is the durable discrepancy record even for
    # older GRNs that have no linked loss movement.
    conn.execute(
        """UPDATE staged_discrepancy_lines SET classified_at=CURRENT_TIMESTAMP
           WHERE batch_id=? AND review_status='ready' AND classified_at IS NULL""",
        (batch_id,),
    )
    return len(lines)

"""
Flask web app: upload page for PO/GRN/Discrepancy/Debit Note documents,
a dashboard built on reconcile.py's reports, and a manual movement form
-- the only way to capture undocumented events (flea markets, transfers,
production) that no document exists for.
"""
import os
import secrets
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

import po_csv_staging
import po_posting
import reconcile
from activity_log import log_activity, recent_activity
from db import get_connection
from discrepancy_note_parser import parse_discrepancy_note_pdf
from grn_parser import parse_grn_pdf
from ingest import (
    assign_grn_source_location,
    assign_po_source_location,
    record_inventory_flag,
    record_movement,
    unvoid_discrepancy_note,
    unvoid_grn,
    unvoid_movement,
    unvoid_po,
    upsert_discrepancy_note,
    upsert_grn,
    upsert_po,
    void_discrepancy_note,
    void_grn,
    void_movement,
    void_po,
)
from po_parser import parse_po_pdf

# Debit Notes and Appointment slots are deliberately NOT wired into the web
# app for now (MVP is PO -> GRN -> discrepancy -> Discrepancy Note only).
# The parser/ingest functions for both still exist and work from the
# command line (`python3 ingest.py debit-note|appointments-csv <file>`) if
# needed later.

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret-change-before-deploy")

# (label, accepted file extension) -- drives both the upload form's
# dropdown and which parser/ingest function handles the file.
DOC_TYPES = {
    "po": ("Purchase Order (PDF)", ".pdf"),
    "grn": ("GRN (PDF)", ".pdf"),
    "discrepancy-note": ("Discrepancy Note (PDF)", ".pdf"),
}

MOVEMENT_TYPES = ["production", "opening_balance", "transfer", "sale", "loss"]
LOCATION_TYPES = ["own_facility", "consignment_partner", "market_event"]


def _build_chart_data(conn, location, sku_code, damaged_by_cause):
    """Pivots reconcile.py's flat query results into the label/dataset
    shape Chart.js wants. Kept as plain dicts/lists (not sqlite3.Row) so
    Jinja's |tojson filter can serialize them directly."""
    stock_rows = reconcile.stock_by_flavor(conn, location=location, sku_code=sku_code)
    stock_flavors = sorted({r["flavor"] for r in stock_rows})
    stock_locations = sorted({r["location"] for r in stock_rows})
    stock_chart = {
        "labels": stock_locations,
        "datasets": [
            {
                "label": flavor,
                "data": [
                    next((r["qty_on_hand"] for r in stock_rows if r["location"] == loc and r["flavor"] == flavor), 0)
                    for loc in stock_locations
                ],
            }
            for flavor in stock_flavors
        ],
    }

    po_facility_rows = reconcile.po_quantity_by_facility(conn, sku_code=sku_code)
    po_facility_chart = {
        "labels": [r["facility"] for r in po_facility_rows],
        "data": [r["total_qty"] for r in po_facility_rows],
    }

    damage_trend_rows = reconcile.damage_trend_over_time(conn, sku_code=sku_code, location=location)
    damage_trend_chart = {
        "labels": [r["date"] for r in damage_trend_rows],
        "data": [r["qty"] for r in damage_trend_rows],
    }

    damage_cause_chart = {
        "labels": [r["cause"] for r in damaged_by_cause],
        "data": [r["total_damaged"] for r in damaged_by_cause],
    }

    flavor_rows = reconcile.po_quantity_by_flavor(conn)
    flavor_popularity_chart = {
        "labels": [r["flavor"] for r in flavor_rows],
        "data": [r["total_qty"] for r in flavor_rows],
    }

    return {
        "stock": stock_chart,
        "po_by_facility": po_facility_chart,
        "damage_trend": damage_trend_chart,
        "damage_cause": damage_cause_chart,
        "flavor_popularity": flavor_popularity_chart,
    }


@app.route("/")
def dashboard():
    conn = get_connection()
    try:
        location = request.args.get("location") or None
        sku_code = request.args.get("sku") or None
        facility = request.args.get("facility") or None

        # Never filtered, never hidden -- a negative balance is a real
        # bookkeeping problem regardless of what the location/SKU filter
        # above happens to be set to.
        negatives = [
            {**dict(n), "recent": reconcile.movements_for_location_sku(conn, n["location"], n["sku_code"], limit=5)}
            for n in reconcile.negative_balances(conn)
        ]

        return render_template(
            "dashboard.html",
            stock=reconcile.stock_by_location(conn, location=location, sku_code=sku_code),
            damaged_by_sku=reconcile.damaged_units_by_sku(conn, sku_code=sku_code, location=location),
            damaged_by_cause=reconcile.damaged_units_by_cause(conn, sku_code=sku_code, location=location),
            shortfall=reconcile.po_vs_received_shortfall(conn, sku_code=sku_code),
            grn_discrepancies=reconcile.grn_discrepancies(conn, sku_code=sku_code),
            flags=reconcile.unresolved_flags(conn),
            negative_balances=negatives,
            inventory_flags=reconcile.unresolved_inventory_flags(conn),
            voided_entries=reconcile.voided_entries(conn),
            purchase_orders=reconcile.purchase_orders_by_facility(conn, facility=facility),
            unallocated_commitments=reconcile.unallocated_commitments(conn),
            all_locations=conn.execute("SELECT name FROM locations ORDER BY name").fetchall(),
            all_products=conn.execute("SELECT sku_code, sku_desc FROM products ORDER BY sku_code").fetchall(),
            all_facilities=conn.execute(
                "SELECT DISTINCT facility_name FROM purchase_orders WHERE facility_name IS NOT NULL ORDER BY facility_name"
            ).fetchall(),
            selected_location=location,
            selected_sku=sku_code,
            selected_facility=facility,
        )
    finally:
        conn.close()


@app.route("/visualizations")
def visualizations():
    conn = get_connection()
    try:
        location = request.args.get("location") or None
        sku_code = request.args.get("sku") or None
        damaged_by_cause = reconcile.damaged_units_by_cause(conn, sku_code=sku_code, location=location)
        return render_template(
            "visualizations.html",
            all_locations=conn.execute("SELECT name FROM locations ORDER BY name").fetchall(),
            all_products=conn.execute("SELECT sku_code, sku_desc FROM products ORDER BY sku_code").fetchall(),
            selected_location=location,
            selected_sku=sku_code,
            charts=_build_chart_data(conn, location, sku_code, damaged_by_cause),
        )
    finally:
        conn.close()


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        doc_type = request.form.get("doc_type")
        file = request.files.get("file")

        if doc_type not in DOC_TYPES:
            flash("Choose a document type.", "error")
            return redirect(url_for("upload"))
        if not file or file.filename == "":
            flash("Choose a file to upload.", "error")
            return redirect(url_for("upload"))

        filename = secure_filename(file.filename)
        expected_ext = DOC_TYPES[doc_type][1]
        if not filename.lower().endswith(expected_ext):
            flash(f"{DOC_TYPES[doc_type][0]} expects a {expected_ext} file.", "error")
            return redirect(url_for("upload"))

        dest = UPLOAD_DIR / filename
        file.save(dest)

        conn = get_connection()
        try:
            before = {r["id"] for r in conn.execute("SELECT id FROM ingestion_flags").fetchall()}

            if doc_type == "po":
                result = upsert_po(conn, parse_po_pdf(str(dest)), source_file=filename)
                msg = f"Stored PO {result}."
                log_activity(conn, "po_upload", f"Uploaded PO {result} ({filename})", "po", result)
            elif doc_type == "grn":
                result = upsert_grn(conn, parse_grn_pdf(str(dest)), source_file=filename)
                msg = f"Stored GRN {result}."
                log_activity(conn, "grn_upload", f"Uploaded GRN {result} ({filename})", "grn", result)
            elif doc_type == "discrepancy-note":
                result = upsert_discrepancy_note(conn, parse_discrepancy_note_pdf(str(dest)), source_file=filename)
                msg = f"Stored Discrepancy Note {result}."
                log_activity(conn, "discrepancy_note_upload", f"Uploaded Discrepancy Note {result} ({filename})", "discrepancy_note", result)

            conn.commit()
            flash(msg, "success")

            new_flags = conn.execute(
                "SELECT issue FROM ingestion_flags WHERE id NOT IN ({}) ".format(
                    ",".join(str(i) for i in before) or "0"
                )
            ).fetchall()
            for f in new_flags:
                flash(f"Flagged for review: {f['issue']}", "warning")
        except Exception as e:
            conn.rollback()
            flash(f"Failed to process {filename}: {e}", "error")
        finally:
            conn.close()

        return redirect(url_for("upload"))

    return render_template("upload.html", doc_types=DOC_TYPES)


@app.route("/movements/new", methods=["GET", "POST"])
def new_movement():
    conn = get_connection()
    try:
        pending = None
        if request.method == "POST":
            try:
                sku_code = request.form["sku_code"].strip()
                quantity = float(request.form["quantity"])
                if quantity <= 0:
                    raise ValueError("Quantity must be greater than zero")

                movement_type = request.form["movement_type"]
                location_from = request.form.get("location_from") or None
                location_to = request.form.get("location_to") or None

                # A movement with no location on either side is invisible to
                # stock_by_location() -- it gets saved but silently counts
                # nowhere. Require whichever location(s) each type actually
                # needs so that can't happen.
                if movement_type in ("production", "opening_balance") and not location_to:
                    raise ValueError(f"{movement_type.replace('_', ' ').capitalize()} needs a 'To location' -- where this stock physically is.")
                if movement_type == "transfer" and not (location_from and location_to):
                    raise ValueError("Transfer needs both a 'From location' and a 'To location'.")
                if movement_type in ("sale", "loss") and not location_from:
                    raise ValueError(f"{movement_type.capitalize()} needs a 'From location' -- where the stock left from.")

                # A movement with a location on the WRONG side is worse than
                # invisible -- it double-counts (an inflow at "To" and an
                # outflow at "From" for the same movement), which can net to
                # zero or partially cancel in stock_by_location() without
                # any error. This actually happened: a production entry
                # with From == To == "Drizzl Demo Warehouse" recorded 5000
                # units that vanished from the dashboard. Forbid the side
                # that shouldn't exist for each type, and forbid From == To
                # outright for transfer (a transfer to the same place isn't
                # a real movement).
                if movement_type in ("production", "opening_balance") and location_from:
                    raise ValueError(f"{movement_type.replace('_', ' ').capitalize()} shouldn't have a 'From location' -- it has no source.")
                if movement_type in ("sale", "loss") and location_to:
                    raise ValueError(f"{movement_type.capitalize()} shouldn't have a 'To location' -- the stock leaves Drizzl's books entirely.")
                if location_from and location_to and location_from == location_to:
                    raise ValueError("From and To locations can't be the same -- that movement would cancel itself out on the dashboard.")

                # Manual transfer/sale/loss all remove stock from
                # location_from -- unlike a GRN (a real document, never
                # blocked, see ingest.py's upsert_grn), a manual entry
                # could just be a typo or a missing earlier entry, so it
                # gets a warning + explicit confirmation instead of saving
                # straight through. production/opening_balance never
                # remove stock, so they skip this entirely.
                #
                # Two severities, checked in order, never collapsed into
                # one generic warning:
                #   1. Physical: would on-hand itself go negative? (the
                #      original, stronger check -- unchanged)
                #   2. Commitment: on-hand stays >= 0, but would the
                #      movement eat into stock already reserved for an
                #      open, unresolved PO allocated to this location?
                #      (see reconcile.committed_at_location()) -- only
                #      checked at all once #1 has already cleared.
                available = resulting = None
                committed = uncommitted_resulting = None
                needs_negative_check = movement_type in ("transfer", "sale", "loss")
                if needs_negative_check:
                    available = reconcile.current_balance(conn, location_from, sku_code)
                    resulting = available - quantity
                    if resulting >= 0:
                        committed = reconcile.committed_at_location(conn, location_from, sku_code)
                        uncommitted_resulting = resulting - committed

                is_negative = needs_negative_check and resulting < 0
                is_commitment_shortfall = uncommitted_resulting is not None and uncommitted_resulting < 0

                confirmed_override = request.form.get("confirmed_override") == "1"
                confirmed_severity = request.form.get("severity")

                if is_negative and not (confirmed_override and confirmed_severity == "negative"):
                    pending = {
                        "severity": "negative",
                        "movement_date": request.form["movement_date"],
                        "sku_code": sku_code,
                        "sku_desc": request.form.get("sku_desc") or "",
                        "quantity": quantity,
                        "movement_type": movement_type,
                        "location_from": location_from or "",
                        "location_to": location_to or "",
                        "location_from_type": request.form.get("location_from_type") or "own_facility",
                        "location_to_type": request.form.get("location_to_type") or "own_facility",
                        "reason": request.form.get("reason") or "",
                        "notes": request.form.get("notes") or "",
                        "available": available,
                        "resulting": resulting,
                    }
                elif is_commitment_shortfall and not (confirmed_override and confirmed_severity == "commitment"):
                    pending = {
                        "severity": "commitment",
                        "movement_date": request.form["movement_date"],
                        "sku_code": sku_code,
                        "sku_desc": request.form.get("sku_desc") or "",
                        "quantity": quantity,
                        "movement_type": movement_type,
                        "location_from": location_from or "",
                        "location_to": location_to or "",
                        "location_from_type": request.form.get("location_from_type") or "own_facility",
                        "location_to_type": request.form.get("location_to_type") or "own_facility",
                        "reason": request.form.get("reason") or "",
                        "notes": request.form.get("notes") or "",
                        "available": available,
                        "committed": committed,
                        "resulting": resulting,
                        "shortfall": -uncommitted_resulting,
                    }
                else:
                    negative_override_reason = None
                    commitment_override_reason = None
                    if is_negative:
                        override_reason = (request.form.get("override_reason") or "").strip()
                        if not override_reason:
                            raise ValueError("Continuing past a negative-inventory warning needs a reason.")
                        negative_override_reason = override_reason
                    elif is_commitment_shortfall:
                        override_reason = (request.form.get("override_reason") or "").strip()
                        if not override_reason:
                            raise ValueError("Continuing past a commitment warning needs a reason.")
                        commitment_override_reason = override_reason

                    movement_id = record_movement(
                        conn,
                        movement_date=request.form["movement_date"],
                        sku_code=sku_code,
                        movement_type=movement_type,
                        quantity=quantity,
                        location_from=location_from,
                        location_to=location_to,
                        location_from_type=request.form.get("location_from_type") or "own_facility",
                        location_to_type=request.form.get("location_to_type") or "own_facility",
                        reason=request.form.get("reason") or None,
                        reference_type="manual",
                        notes=request.form.get("notes") or None,
                        sku_desc=request.form.get("sku_desc") or None,
                        negative_override_reason=negative_override_reason,
                        commitment_override_reason=commitment_override_reason,
                    )
                    if negative_override_reason:
                        record_inventory_flag(
                            conn, sku_code=sku_code, location_name=location_from,
                            source="manual_override", available_before=available,
                            requested_qty=quantity, resulting_balance=resulting,
                            movement_id=movement_id, reason=negative_override_reason,
                        )
                    if commitment_override_reason:
                        record_inventory_flag(
                            conn, sku_code=sku_code, location_name=location_from,
                            source="commitment_override",
                            available_before=(available - committed) if available is not None else None,
                            requested_qty=quantity, resulting_balance=uncommitted_resulting,
                            movement_id=movement_id, reason=commitment_override_reason,
                        )
                    where = " -> ".join(p for p in (location_from, location_to) if p)
                    log_activity(
                        conn, "movement",
                        f"Logged {movement_type} of {quantity:g} x {sku_code}" + (f" ({where})" if where else ""),
                        "movement",
                    )
                    conn.commit()
                    if negative_override_reason:
                        flash(
                            f"Movement recorded -- {sku_code} at {location_from} is now at {resulting:g} units. "
                            f"Flagged for investigation.",
                            "warning",
                        )
                    elif commitment_override_reason:
                        flash(
                            f"Movement recorded -- {sku_code} at {location_from} now has a commitment shortfall of "
                            f"{-uncommitted_resulting:g} units against an open PO. Flagged for investigation.",
                            "warning",
                        )
                    else:
                        flash("Movement recorded.", "success")
                    return redirect(url_for("new_movement"))
            except Exception as e:
                conn.rollback()
                flash(f"Failed to record movement: {e}", "error")

        products = conn.execute(
            "SELECT sku_code, sku_desc FROM products ORDER BY sku_code"
        ).fetchall()
        locations = conn.execute("SELECT name, type FROM locations ORDER BY name").fetchall()

        filter_location = request.args.get("location") or None
        filter_sku = request.args.get("sku") or None
        recent_query = """
            SELECT m.*, lf.name AS from_name, lt.name AS to_name
            FROM inventory_movements m
            LEFT JOIN locations lf ON lf.id = m.location_from_id
            LEFT JOIN locations lt ON lt.id = m.location_to_id
        """
        conditions, params = [], []
        if filter_location:
            conditions.append("(lf.name = ? OR lt.name = ?)")
            params.extend([filter_location, filter_location])
        if filter_sku:
            conditions.append("m.sku_code = ?")
            params.append(filter_sku)
        if conditions:
            recent_query += " WHERE " + " AND ".join(conditions)
        recent_query += " ORDER BY m.id DESC LIMIT 25"
        recent = conn.execute(recent_query, params).fetchall()

        return render_template(
            "new_movement.html",
            products=products,
            locations=locations,
            movement_types=MOVEMENT_TYPES,
            location_types=LOCATION_TYPES,
            recent=recent,
            selected_location=filter_location,
            selected_sku=filter_sku,
            pending=pending,
        )
    finally:
        conn.close()


@app.route("/flags/<int:flag_id>/resolve", methods=["POST"])
def resolve_flag(flag_id):
    conn = get_connection()
    flag = conn.execute("SELECT document_type, document_id FROM ingestion_flags WHERE id = ?", (flag_id,)).fetchone()
    conn.execute("UPDATE ingestion_flags SET resolved = 1 WHERE id = ?", (flag_id,))
    if flag:
        log_activity(
            conn, "flag_resolved",
            f"Marked flag resolved: {flag['document_type']} {flag['document_id']} (flag #{flag_id})",
            "ingestion_flag", str(flag_id),
        )
    conn.commit()
    conn.close()
    flash("Flag marked resolved.", "success")
    return redirect(url_for("dashboard"))


@app.route("/inventory-flags/<int:flag_id>/resolve", methods=["POST"])
def resolve_inventory_flag(flag_id):
    conn = get_connection()
    flag = conn.execute("SELECT sku_code, location_name FROM inventory_flags WHERE id = ?", (flag_id,)).fetchone()
    conn.execute("UPDATE inventory_flags SET resolved = 1 WHERE id = ?", (flag_id,))
    if flag:
        log_activity(
            conn, "flag_resolved",
            f"Marked negative-inventory flag resolved: {flag['sku_code']} at {flag['location_name']} (flag #{flag_id})",
            "inventory_flag", str(flag_id),
        )
    conn.commit()
    conn.close()
    flash("Inventory flag marked resolved.", "success")
    return redirect(url_for("dashboard"))


@app.route("/lookup")
def lookup():
    conn = get_connection()
    try:
        query = request.args.get("q", "").strip()
        result = reconcile.lookup_document(conn, query) if query else None
        return render_template(
            "lookup.html", query=query, result=result,
            all_locations=conn.execute("SELECT name FROM locations ORDER BY name").fetchall(),
        )
    finally:
        conn.close()


@app.route("/po/<po_number>/assign-location", methods=["POST"])
def assign_po_location_route(po_number):
    conn = get_connection()
    try:
        location_name = (request.form.get("source_location") or "").strip()
        if not location_name:
            raise ValueError("Choose a Drizzl location to allocate this PO to.")
        assign_po_source_location(conn, po_number, location_name)
        log_activity(conn, "po_location_assigned", f"Assigned PO {po_number}'s source location to {location_name}", "po", po_number)
        conn.commit()
        flash(f"PO {po_number} allocated to {location_name}.", "success")
    except ValueError as e:
        conn.rollback()
        flash(str(e), "error")
    finally:
        conn.close()
    return redirect(url_for("lookup", q=po_number))


@app.route("/grn/<grn_number>/assign-location", methods=["POST"])
def assign_grn_location_route(grn_number):
    conn = get_connection()
    try:
        location_name = (request.form.get("source_location") or "").strip()
        if not location_name:
            raise ValueError("Choose a Drizzl location to allocate this GRN to.")
        assign_grn_source_location(conn, grn_number, location_name)
        log_activity(conn, "grn_location_assigned", f"Assigned GRN {grn_number}'s source location to {location_name}", "grn", grn_number)
        conn.commit()
        flash(f"GRN {grn_number} allocated to {location_name}. Any pending sale movement(s) have been created.", "success")
    except ValueError as e:
        conn.rollback()
        flash(str(e), "error")
    finally:
        conn.close()
    return redirect(url_for("lookup", q=grn_number))


@app.route("/po/<po_number>/void", methods=["POST"])
def void_po_route(po_number):
    conn = get_connection()
    try:
        reason = (request.form.get("reason") or "").strip()
        if not reason:
            raise ValueError("Voiding needs a reason.")
        void_po(conn, po_number, reason)
        log_activity(conn, "po_voided", f"Voided PO {po_number}: {reason}", "po", po_number)
        conn.commit()
        flash(f"PO {po_number} voided. It stays visible here and on the dashboard's Voided entries panel, with a Restore option if this was a mistake.", "warning")
    except ValueError as e:
        conn.rollback()
        flash(str(e), "error")
    finally:
        conn.close()
    return redirect(url_for("lookup", q=po_number))


@app.route("/po/<po_number>/restore", methods=["POST"])
def restore_po_route(po_number):
    conn = get_connection()
    try:
        unvoid_po(conn, po_number)
        log_activity(conn, "po_restored", f"Restored PO {po_number}", "po", po_number)
        conn.commit()
        flash(f"PO {po_number} restored.", "success")
    finally:
        conn.close()
    return redirect(url_for("lookup", q=po_number))


@app.route("/grn/<grn_number>/void", methods=["POST"])
def void_grn_route(grn_number):
    conn = get_connection()
    try:
        reason = (request.form.get("reason") or "").strip()
        if not reason:
            raise ValueError("Voiding needs a reason.")
        void_grn(conn, grn_number, reason)
        log_activity(conn, "grn_voided", f"Voided GRN {grn_number} (and its sale movement(s)): {reason}", "grn", grn_number)
        conn.commit()
        flash(f"GRN {grn_number} voided, along with the sale movement(s) it created. Restore it from the dashboard's Voided entries panel if this was a mistake.", "warning")
    except ValueError as e:
        conn.rollback()
        flash(str(e), "error")
    finally:
        conn.close()
    return redirect(url_for("lookup", q=grn_number))


@app.route("/grn/<grn_number>/restore", methods=["POST"])
def restore_grn_route(grn_number):
    conn = get_connection()
    try:
        unvoid_grn(conn, grn_number)
        log_activity(conn, "grn_restored", f"Restored GRN {grn_number} (and its sale movement(s))", "grn", grn_number)
        conn.commit()
        flash(f"GRN {grn_number} restored.", "success")
    finally:
        conn.close()
    return redirect(url_for("lookup", q=grn_number))


@app.route("/discrepancy-note/<dn_number>/void", methods=["POST"])
def void_discrepancy_note_route(dn_number):
    conn = get_connection()
    try:
        reason = (request.form.get("reason") or "").strip()
        if not reason:
            raise ValueError("Voiding needs a reason.")
        void_discrepancy_note(conn, dn_number, reason)
        log_activity(conn, "discrepancy_note_voided", f"Voided Discrepancy Note {dn_number}: {reason}", "discrepancy_note", dn_number)
        conn.commit()
        flash(f"Discrepancy Note {dn_number} voided.", "warning")
    except ValueError as e:
        conn.rollback()
        flash(str(e), "error")
    finally:
        conn.close()
    return redirect(url_for("lookup", q=dn_number))


@app.route("/discrepancy-note/<dn_number>/restore", methods=["POST"])
def restore_discrepancy_note_route(dn_number):
    conn = get_connection()
    try:
        unvoid_discrepancy_note(conn, dn_number)
        log_activity(conn, "discrepancy_note_restored", f"Restored Discrepancy Note {dn_number}", "discrepancy_note", dn_number)
        conn.commit()
        flash(f"Discrepancy Note {dn_number} restored.", "success")
    finally:
        conn.close()
    return redirect(url_for("lookup", q=dn_number))


@app.route("/movements/<int:movement_id>/void", methods=["POST"])
def void_movement_route(movement_id):
    conn = get_connection()
    try:
        reason = (request.form.get("reason") or "").strip()
        if not reason:
            raise ValueError("Voiding needs a reason.")
        row = void_movement(conn, movement_id, reason)
        log_activity(
            conn, "movement_voided",
            f"Voided {row['movement_type']} of {row['quantity']:g} x {row['sku_code']} (movement #{movement_id}): {reason}",
            "movement", str(movement_id),
        )
        conn.commit()
        flash("Movement voided. Restore it from the dashboard's Voided entries panel if this was a mistake.", "warning")
    except ValueError as e:
        conn.rollback()
        flash(str(e), "error")
    finally:
        conn.close()
    return redirect(url_for("new_movement"))


@app.route("/movements/<int:movement_id>/restore", methods=["POST"])
def restore_movement_route(movement_id):
    conn = get_connection()
    try:
        row = unvoid_movement(conn, movement_id)
        log_activity(
            conn, "movement_restored",
            f"Restored {row['movement_type']} of {row['quantity']:g} x {row['sku_code']} (movement #{movement_id})",
            "movement", str(movement_id),
        )
        conn.commit()
        flash("Movement restored.", "success")
    except ValueError as e:
        conn.rollback()
        flash(str(e), "error")
    finally:
        conn.close()
    return redirect(url_for("new_movement"))


@app.route("/activity")
def activity():
    conn = get_connection()
    try:
        action_type = request.args.get("action_type") or None
        return render_template(
            "activity.html",
            entries=recent_activity(conn, action_type=action_type),
            action_type=action_type,
        )
    finally:
        conn.close()


@app.route("/po-import", methods=["GET", "POST"])
def po_import():
    conn = get_connection()
    try:
        if request.method == "POST":
            file = request.files.get("file")
            if not file or file.filename == "":
                flash("Choose a CSV file to upload.", "error")
                return redirect(url_for("po_import"))

            original_name = secure_filename(file.filename)
            if not original_name.lower().endswith(".csv"):
                flash("Purchase Order CSV import expects a .csv file.", "error")
                return redirect(url_for("po_import"))

            # Collision-safe stored filename -- the original name is never
            # trusted as a unique filesystem key. The human-readable
            # original name is still what gets shown/stored as
            # source_filename, via stage_po_csv's filename= override.
            stored_name = f"po_csv_{secrets.token_hex(8)}_{original_name}"
            dest = UPLOAD_DIR / stored_name
            file.save(dest)

            try:
                result = po_csv_staging.stage_po_csv(conn, str(dest), filename=original_name)
                if result["reused_existing_batch"]:
                    flash("This file was already imported. Opening the existing staged batch.", "warning")
                else:
                    summary = po_csv_staging.batch_summary(conn, result["batch_id"])
                    log_activity(
                        conn, "po_csv_upload",
                        f"Staged PO CSV {original_name} ({summary['orders']} orders, {summary['lines']} lines)",
                        "po_import_batch", str(result["batch_id"]),
                    )
                    flash(
                        f"Staged {summary['orders']} purchase order(s), {summary['lines']} line(s) from {original_name}. "
                        "Nothing has been posted to inventory -- review and assign a Drizzl source warehouse below.",
                        "success",
                    )
                conn.commit()
                return redirect(url_for("po_import_review", batch_id=result["batch_id"]))
            except po_csv_staging.FatalImportError as e:
                conn.rollback()
                flash(f"Could not import {original_name}: {e}", "error")
                return redirect(url_for("po_import"))
            except Exception:
                conn.rollback()
                flash(f"Could not import {original_name} -- an unexpected error occurred.", "error")
                return redirect(url_for("po_import"))

        return render_template("po_import.html", batches=po_csv_staging.list_recent_batches(conn))
    finally:
        conn.close()


@app.route("/po-import/<int:batch_id>")
def po_import_review(batch_id):
    conn = get_connection()
    try:
        batch = po_csv_staging.get_import_batch(conn, batch_id)
        if batch is None:
            abort(404, description="This batch does not exist.")
        return render_template(
            "po_import_review.html",
            batch=batch,
            staged_pos=po_csv_staging.list_staged_pos(conn, batch_id),
            summary=po_csv_staging.batch_summary(conn, batch_id),
            all_locations=conn.execute("SELECT id, name FROM locations ORDER BY name").fetchall(),
        )
    finally:
        conn.close()


@app.route("/po-import/<int:batch_id>/po/<int:staged_po_id>")
def staged_po_detail(batch_id, staged_po_id):
    conn = get_connection()
    try:
        batch = po_csv_staging.get_import_batch(conn, batch_id)
        if batch is None:
            abort(404, description="This batch does not exist.")
        staged_po = po_csv_staging.get_staged_po(conn, staged_po_id)
        if staged_po is None or staged_po["batch_id"] != batch_id:
            abort(404, description="This staged PO does not belong to this batch.")
        return render_template(
            "staged_po_detail.html",
            batch=batch,
            po=staged_po,
            raw_rows=po_csv_staging.get_staged_po_raw_rows(conn, staged_po_id),
            all_locations=conn.execute("SELECT id, name FROM locations ORDER BY name").fetchall(),
        )
    finally:
        conn.close()


@app.route("/po-import/<int:batch_id>/assign-source", methods=["POST"])
def assign_staged_source(batch_id):
    conn = get_connection()
    try:
        batch = po_csv_staging.get_import_batch(conn, batch_id)
        if batch is None:
            abort(404, description="This batch does not exist.")

        try:
            staged_po_ids = [int(i) for i in request.form.getlist("staged_po_ids")]
        except ValueError:
            flash("Invalid purchase order selection.", "error")
            return redirect(url_for("po_import_review", batch_id=batch_id))

        raw_location = request.form.get("source_location_id")
        try:
            source_location_id = int(raw_location) if raw_location else None
        except ValueError:
            source_location_id = None
        if source_location_id is None:
            flash("Choose a Drizzl source warehouse.", "error")
            return redirect(url_for("po_import_review", batch_id=batch_id))

        try:
            n = po_csv_staging.assign_source_location(conn, batch_id, staged_po_ids, source_location_id)
            location_name = conn.execute(
                "SELECT name FROM locations WHERE id = ?", (source_location_id,)
            ).fetchone()["name"]
            log_activity(
                conn, "po_staged_source_assigned",
                f"Assigned Drizzl source {location_name} to {n} staged PO(s) in batch {batch_id}",
                "po_import_batch", str(batch_id),
            )
            conn.commit()
            flash(f"Assigned {location_name} to {n} purchase order(s).", "success")
        except ValueError as e:
            conn.rollback()
            flash(str(e), "error")
    finally:
        conn.close()
    return redirect(url_for("po_import_review", batch_id=batch_id))


@app.route("/po-import/<int:batch_id>/post", methods=["POST"])
def post_staged_pos(batch_id):
    """Phase 5: posts the selected staged POs into the official ledger.
    A human must explicitly choose which orders to post -- this never
    happens automatically on source assignment or page load. Selected
    posting is all-or-nothing: see po_posting.post_staged_purchase_orders()."""
    conn = get_connection()
    try:
        batch = po_csv_staging.get_import_batch(conn, batch_id)
        if batch is None:
            abort(404, description="This batch does not exist.")

        try:
            staged_po_ids = [int(i) for i in request.form.getlist("staged_po_ids")]
        except ValueError:
            flash("Invalid purchase order selection.", "error")
            return redirect(url_for("po_import_review", batch_id=batch_id))
        if not staged_po_ids:
            flash("Choose at least one purchase order to post.", "error")
            return redirect(url_for("po_import_review", batch_id=batch_id))

        try:
            result = po_posting.post_staged_purchase_orders(conn, batch_id, staged_po_ids)
        except po_posting.PostingError as e:
            conn.rollback()
            flash(str(e), "error")
            return redirect(url_for("po_import_review", batch_id=batch_id))

        if result["rejected"]:
            conn.rollback()
            for staged_po_id, reasons in result["rejected"].items():
                flash(f"Staged PO id {staged_po_id} was not posted: {' '.join(reasons)}", "error")
            flash(
                "No purchase orders were posted -- posting a selection is all-or-nothing, and "
                "at least one selected order was not ready.",
                "error",
            )
            return redirect(url_for("po_import_review", batch_id=batch_id))

        if result["posted"]:
            po_numbers = ", ".join(p["po_number"] for p in result["posted"])
            log_activity(
                conn, "po_posted",
                f"Posted {len(result['posted'])} purchase order(s) to the official ledger from "
                f"batch {batch_id}: {po_numbers}",
                "po_import_batch", str(batch_id),
            )
        conn.commit()

        if result["posted"]:
            flash(
                f"Posted {len(result['posted'])} purchase order(s) to the official ledger. "
                "This creates official PO records and commitments -- it does not move physical "
                "inventory.",
                "success",
            )
        if result["already_posted"]:
            flash(
                f"{len(result['already_posted'])} selected order(s) were already posted -- no "
                "changes made.",
                "warning",
            )
    finally:
        conn.close()
    return redirect(url_for("po_import_review", batch_id=batch_id))


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5001)))

"""
Flask web app: staged CSV intake for POs/GRNs,
a dashboard built on reconcile.py's reports, and a Master-Product-only manual movement form
-- the only way to capture undocumented events (flea markets, transfers,
production) that no document exists for.
"""
import logging
import os
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf import CSRFProtect
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

import config
import catalog
import discrepancy_csv_staging
import grn_csv_staging
import grn_posting
import po_csv_staging
import po_posting
import reconcile
from activity_log import log_activity, recent_activity
from db import get_connection
from ingest import (
    assign_grn_source_location,
    assign_po_source_location,
    correct_po_source_location,
    record_inventory_flag,
    record_movement,
    unvoid_grn,
    unvoid_movement,
    unvoid_po,
    void_grn,
    void_movement,
    void_po,
)

# Debit Notes and Appointment slots are deliberately NOT wired into the web
# app for now (MVP is PO -> GRN -> canonical PO-vs-GRN discrepancy
# reporting; see reconcile.py's official_discrepancies()). The parser/
# ingest functions for both still exist and work from the command line
# (`python3 ingest.py debit-note|appointments-csv <file>`) if needed
# later. The old Discrepancy Note PDF workflow (upload/parse/void) was
# removed entirely in Phase 9 -- discrepancy is now computed, never
# uploaded as a separate document.

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
# SECRET_KEY: config.py already refuses to start with APP_ENV=production
# and no real SECRET_KEY set -- there is deliberately no insecure
# fallback reachable in production here, only config's own clearly-
# labeled dev-only default (Phase 12).
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_BYTES


@app.template_filter("product_label")
def product_label(value):
    """Short UI-only Master Product name; canonical data stays unchanged."""
    label = (value or "").strip()
    if label.lower().startswith("drizzl "):
        label = label[7:]
    sparkling_prefix = "Probiotic Sparkling Water - "
    if label.startswith(sparkling_prefix):
        label = f"{label[len(sparkling_prefix):]} Sparkling Water"
    return label


@app.template_filter("compact_product_text")
def compact_product_text(value):
    """Remove product-brand, barcode and size noise from mixed UI text."""
    text = value or ""
    text = re.sub(r"\bDrizzl\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"Probiotic Sparkling Water\s*-\s*([A-Za-z &]+)",
        lambda match: f"{match.group(1).strip()} Sparkling Water",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*\(?\b\d{12,13}\b\)?", "", text)
    text = re.sub(r"\s*[—|-]?\s*\d+(?:\.\d+)?\s*ml\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", text).strip()

# CSRF protection (Phase 12) -- Flask-WTF's standard app-wide guard.
# Every POST/PUT/PATCH/DELETE request must carry a valid csrf_token
# (every <form method="post"> in templates/ already includes one as a
# hidden field). This is in addition to, never a replacement for,
# server-side validation -- every route still re-checks its own
# business rules regardless of whether the CSRF token was valid.
csrf = CSRFProtect(app)

# Authentication (Phase 12) -- minimal, single-role session login backed
# by the users table (id/username/password_hash), which has existed
# since Phase 1 specifically so this didn't need a schema change. No
# self-registration, no password reset -- accounts are created out of
# band with create_user.py (see README). Werkzeug's own password hashing
# (already a dependency) is used for verification; plaintext passwords
# are never stored or logged.
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."
login_manager.login_message_category = "warning"


class AppUser(UserMixin):
    def __init__(self, row):
        self.id = str(row["id"])
        self.username = row["username"]


@login_manager.user_loader
def load_user(user_id):
    conn = get_connection()
    try:
        row = conn.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
        return AppUser(row) if row else None
    finally:
        conn.close()

MOVEMENT_TYPES = ["production", "opening_balance", "transfer", "sale", "loss"]
LOCATION_TYPES = ["own_facility", "consignment_partner", "market_event"]


def _date_filter_args():
    preset = request.args.get("period", "all")
    today = date.today()
    if preset == "7d":
        return preset, (today - timedelta(days=6)).isoformat(), today.isoformat()
    if preset == "30d":
        return preset, (today - timedelta(days=29)).isoformat(), today.isoformat()
    if preset == "month":
        return preset, today.replace(day=1).isoformat(), today.isoformat()
    if preset == "custom":
        values = []
        for name in ("date_from", "date_to"):
            raw = request.args.get(name, "")
            try:
                values.append(datetime.strptime(raw, "%Y-%m-%d").date().isoformat() if raw else None)
            except ValueError:
                values.append(None)
        return preset, values[0], values[1]
    return "all", None, None


def _build_chart_data(conn, location, product_id, damaged_by_cause, date_from=None, date_to=None):
    """Pivots reconcile.py's flat query results into the label/dataset
    shape Chart.js wants. Kept as plain dicts/lists (not sqlite3.Row) so
    Jinja's |tojson filter can serialize them directly."""
    stock_rows = reconcile.stock_by_flavor(conn, location=location, product_id=product_id)
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

    po_facility_rows = reconcile.po_quantity_by_facility(conn, product_id=product_id, date_from=date_from, date_to=date_to)
    po_facility_chart = {
        "labels": [r["facility"] for r in po_facility_rows],
        "data": [r["total_qty"] for r in po_facility_rows],
    }

    damage_trend_rows = reconcile.damage_trend_over_time(conn, product_id=product_id, location=location, date_from=date_from, date_to=date_to)
    damage_trend_chart = {
        "labels": [r["date"] for r in damage_trend_rows],
        "data": [r["qty"] for r in damage_trend_rows],
    }

    damage_cause_chart = {
        "labels": [r["cause"] for r in damaged_by_cause],
        "data": [r["total_damaged"] for r in damaged_by_cause],
    }

    flavor_rows = reconcile.po_quantity_by_flavor(conn, date_from=date_from, date_to=date_to)
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


# Endpoints reachable without a session -- everything else is gated by
# the before_request hook below. Fail-secure by default: a new route
# added later is automatically protected unless explicitly listed here.
_PUBLIC_ENDPOINTS = {"login", "health", "static"}


@app.before_request
def _require_login():
    if request.endpoint in _PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if not current_user.is_authenticated:
        return login_manager.unauthorized()


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id, username, password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
        finally:
            conn.close()
        # Same generic message whether the username doesn't exist or the
        # password is wrong -- never reveal which one it was.
        if row is None or not check_password_hash(row["password_hash"], password):
            flash("Invalid username or password.", "error")
            return render_template("login.html"), 401
        login_user(AppUser(row))
        flash(f"Welcome back, {row['username']}.", "success")
        next_url = request.args.get("next")
        return redirect(next_url or url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Logged out.", "success")
    return redirect(url_for("login"))


@app.route("/health")
def health():
    """Liveness/readiness probe -- deliberately reveals nothing about
    schema, configuration, or secrets, just whether the process is up
    and can reach the database. No auth required (so a load balancer/
    orchestrator can call it), but that's also why it must stay this
    minimal."""
    try:
        conn = get_connection()
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        return jsonify(status="ok"), 200
    except Exception:
        log.exception("Health check failed")
        return jsonify(status="unavailable"), 503


@app.route("/")
def dashboard():
    conn = get_connection()
    try:
        location = request.args.get("location") or None
        product_id = request.args.get("product") or None
        if product_id:
            try:
                product_id = int(product_id)
            except ValueError:
                product_id = None
        facility = request.args.get("facility") or None

        # Never filtered, never hidden -- a negative balance is a real
        # bookkeeping problem regardless of what the location/product filter
        # above happens to be set to.
        negatives = []
        for n in reconcile.negative_balances(conn):
            recent = (
                reconcile.movements_for_location_product(conn, n["location"], n["product_id"], limit=5)
                if n["product_id"] is not None
                else reconcile.movements_for_location_sku(conn, n["location"], n["sku_code"], limit=5)
            )
            negatives.append({**dict(n), "recent": recent})

        stock_rows = reconcile.stock_by_location(conn, location=location, product_id=product_id)

        fulfillment_rows = reconcile.po_grn_fulfillment(conn)
        po_counts = {
            "total": sum(r["fulfillment_status"] != "voided" for r in fulfillment_rows),
            "fulfilled": sum(r["fulfillment_status"] in {"grn_posted", "grn_posted_discrepancy"} for r in fulfillment_rows),
            "awaiting": sum(r["fulfillment_status"] == "awaiting_grn" for r in fulfillment_rows),
        }

        return render_template(
            "dashboard.html",
            stock=stock_rows,
            damaged_by_cause=reconcile.damaged_units_by_cause(conn, product_id=product_id, location=location),
            official_discrepancies=reconcile.official_discrepancies(conn, product_id=product_id),
            flags=reconcile.unresolved_flags(conn),
            negative_balances=negatives,
            inventory_flags=reconcile.unresolved_inventory_flags(conn),
            voided_entries=reconcile.voided_entries(conn),
            purchase_orders=reconcile.purchase_orders_by_facility(conn, facility=facility),
            unallocated_commitments=reconcile.unallocated_commitments(conn),
            po_counts=po_counts,
            all_locations=conn.execute("SELECT name FROM locations ORDER BY name").fetchall(),
            all_products=conn.execute(
                "SELECT product_id, barcode, product_name FROM master_products WHERE active = TRUE ORDER BY product_name"
            ).fetchall(),
            all_facilities=conn.execute(
                "SELECT DISTINCT facility_name FROM purchase_orders WHERE facility_name IS NOT NULL ORDER BY facility_name"
            ).fetchall(),
            selected_location=location,
            selected_product_id=product_id,
            selected_facility=facility,
        )
    finally:
        conn.close()


@app.route("/visualizations")
def visualizations():
    conn = get_connection()
    try:
        location = request.args.get("location") or None
        product_id = request.args.get("product") or None
        if product_id:
            try:
                product_id = int(product_id)
            except ValueError:
                product_id = None
        period, date_from, date_to = _date_filter_args()
        damaged_by_cause = reconcile.damaged_units_by_cause(
            conn, product_id=product_id, location=location, date_from=date_from, date_to=date_to
        )
        return render_template(
            "visualizations.html",
            all_locations=conn.execute("SELECT name FROM locations ORDER BY name").fetchall(),
            all_products=conn.execute(
                "SELECT product_id, barcode, product_name FROM master_products WHERE active = TRUE ORDER BY product_name"
            ).fetchall(),
            selected_location=location,
            selected_product_id=product_id,
            period=period, date_from=date_from, date_to=date_to,
            charts=_build_chart_data(conn, location, product_id, damaged_by_cause, date_from, date_to),
        )
    finally:
        conn.close()


@app.route("/po-grn-tracker")
def po_grn_tracker():
    allowed = {"awaiting_grn", "grn_posted", "grn_posted_discrepancy", "voided"}
    selected_status = request.args.get("status") or None
    if selected_status not in allowed:
        selected_status = None
    conn = get_connection()
    try:
        all_rows = reconcile.po_grn_fulfillment(conn)
        counts = {key: sum(r["fulfillment_status"] == key for r in all_rows) for key in allowed}
        rows = all_rows if selected_status is None else [r for r in all_rows if r["fulfillment_status"] == selected_status]
        return render_template(
            "po_grn_tracker.html", rows=rows, counts=counts,
            selected_status=selected_status, total_rows=len(all_rows),
        )
    finally:
        conn.close()


@app.route("/movements/new", methods=["GET", "POST"])
def new_movement():
    conn = get_connection()
    try:
        pending = None
        if request.method == "POST":
            try:
                try:
                    product_id = int(request.form["product_id"])
                except (KeyError, TypeError, ValueError):
                    raise ValueError("Choose a Master Product.")
                product = catalog.get_master_product_by_id(conn, product_id, active_only=True)
                if product is None:
                    raise ValueError("That Master Product does not exist or is inactive.")
                sku_code = product["barcode"]
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
                    location_row = conn.execute(
                        "SELECT id FROM locations WHERE name = ?", (location_from,)
                    ).fetchone()
                    location_id = location_row["id"] if location_row else None
                    available = reconcile.current_balance_by_product(conn, location_id, product_id)
                    resulting = available - quantity
                    if resulting >= 0:
                        committed = reconcile.committed_at_location_product(conn, location_from, product_id)
                        uncommitted_resulting = resulting - committed

                is_negative = needs_negative_check and resulting < 0
                is_commitment_shortfall = uncommitted_resulting is not None and uncommitted_resulting < 0

                confirmed_override = request.form.get("confirmed_override") == "1"
                confirmed_severity = request.form.get("severity")

                if is_negative and not (confirmed_override and confirmed_severity == "negative"):
                    pending = {
                        "severity": "negative",
                        "movement_date": request.form["movement_date"],
                        "product_id": product_id,
                        "product_name": product["product_name"],
                        "sku_code": sku_code,
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
                        "product_id": product_id,
                        "product_name": product["product_name"],
                        "sku_code": sku_code,
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
                        product_id=product_id,
                        movement_type=movement_type,
                        quantity=quantity,
                        location_from=location_from,
                        location_to=location_to,
                        location_from_type=request.form.get("location_from_type") or "own_facility",
                        location_to_type=request.form.get("location_to_type") or "own_facility",
                        reason=request.form.get("reason") or None,
                        reference_type="manual",
                        notes=request.form.get("notes") or None,
                        negative_override_reason=negative_override_reason,
                        commitment_override_reason=commitment_override_reason,
                    )
                    if negative_override_reason:
                        record_inventory_flag(
                            conn, sku_code=sku_code, location_name=location_from,
                            source="manual_override", available_before=available,
                            requested_qty=quantity, resulting_balance=resulting,
                            movement_id=movement_id, reason=negative_override_reason,
                            product_id=product_id,
                        )
                    if commitment_override_reason:
                        record_inventory_flag(
                            conn, sku_code=sku_code, location_name=location_from,
                            source="commitment_override",
                            available_before=(available - committed) if available is not None else None,
                            requested_qty=quantity, resulting_balance=uncommitted_resulting,
                            movement_id=movement_id, reason=commitment_override_reason,
                            product_id=product_id,
                        )
                    where = " -> ".join(p for p in (location_from, location_to) if p)
                    log_activity(
                        conn, "movement",
                        f"Logged {movement_type} of {quantity:g} x {product['product_name']} ({sku_code})"
                        + (f" ({where})" if where else ""),
                        "movement", str(movement_id),
                    )
                    conn.commit()
                    if negative_override_reason:
                        flash(
                            f"Movement recorded -- {product['product_name']} at {location_from} is now at {resulting:g} units. "
                            f"Flagged for investigation.",
                            "warning",
                        )
                    elif commitment_override_reason:
                        flash(
                            f"Movement recorded -- {product['product_name']} at {location_from} now has a commitment shortfall of "
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
            "SELECT product_id, barcode, product_name, unit_size FROM master_products "
            "WHERE active = TRUE ORDER BY product_name"
        ).fetchall()
        locations = conn.execute("SELECT name, type FROM locations ORDER BY name").fetchall()

        filter_location = request.args.get("location") or None
        filter_product_id = request.args.get("product") or None
        if filter_product_id:
            try:
                filter_product_id = int(filter_product_id)
            except ValueError:
                filter_product_id = None
        recent_query = """
            SELECT m.*, lf.name AS from_name, lt.name AS to_name,
                   mp.product_name, mp.barcode
            FROM inventory_movements m
            LEFT JOIN locations lf ON lf.id = m.location_from_id
            LEFT JOIN locations lt ON lt.id = m.location_to_id
            LEFT JOIN master_products mp ON mp.product_id = m.product_id
        """
        conditions, params = [], []
        if filter_location:
            conditions.append("(lf.name = ? OR lt.name = ?)")
            params.extend([filter_location, filter_location])
        if filter_product_id:
            conditions.append("m.product_id = ?")
            params.append(filter_product_id)
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
            selected_product_id=filter_product_id,
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


@app.route("/grn/<int:grn_id>/restore", methods=["POST"])
def restore_grn_route(grn_id):
    """Phase 10: keyed by grn_id (the real, unambiguous identity), not
    grn_number -- a voided GRN's grn_number can be shared with its
    active replacement in a supersede chain (see ingest.unvoid_grn())."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT grn_number FROM grn_receipts WHERE grn_id = ?", (grn_id,)).fetchone()
        grn_number = row["grn_number"] if row else None
        unvoid_grn(conn, grn_id)
        log_activity(conn, "grn_restored", f"Restored GRN {grn_number} (grn_id {grn_id}) (and its sale movement(s))", "grn", grn_number)
        conn.commit()
        flash(f"GRN {grn_number} restored.", "success")
    except ValueError as e:
        conn.rollback()
        flash(str(e), "error")
    finally:
        conn.close()
    return redirect(url_for("lookup", q=grn_number) if grn_number else url_for("dashboard"))


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
        period, date_from, date_to = _date_filter_args()
        return render_template(
            "activity.html",
            entries=recent_activity(conn, action_type=action_type, date_from=date_from, date_to=date_to),
            action_type=action_type,
            period=period, date_from=date_from, date_to=date_to,
        )
    finally:
        conn.close()


@app.route("/discrepancy-import", methods=["GET", "POST"])
def discrepancy_import():
    """Stage a Scootsy discrepancy/PR CSV. This classifies an existing
    GRN shortfall loss; it never creates another inventory movement."""
    conn = get_connection()
    try:
        if request.method == "POST":
            try:
                customer_id = int(request.form.get("customer_id", ""))
            except ValueError:
                customer_id = None
            if customer_id is None or conn.execute(
                "SELECT 1 FROM customers WHERE id=?", (customer_id,)
            ).fetchone() is None:
                flash("Choose a customer before uploading.", "error")
                return redirect(url_for("discrepancy_import"))

            file = request.files.get("file")
            if not file or not file.filename:
                flash("Choose a CSV file to upload.", "error")
                return redirect(url_for("discrepancy_import"))
            original_name = secure_filename(file.filename)
            if not original_name.lower().endswith(".csv"):
                flash("Discrepancy import expects a .csv file.", "error")
                return redirect(url_for("discrepancy_import"))

            dest = UPLOAD_DIR / f"discrepancy_csv_{secrets.token_hex(8)}_{original_name}"
            file.save(dest)
            try:
                result = discrepancy_csv_staging.stage_csv(
                    conn, str(dest), customer_id, filename=original_name
                )
                if result["reused"]:
                    flash("This discrepancy file was already imported. Opening its review.", "warning")
                else:
                    log_activity(
                        conn, "discrepancy_csv_upload",
                        f"Staged discrepancy CSV {original_name}",
                        "discrepancy_import_batch", str(result["batch_id"]),
                    )
                    flash("Discrepancy file staged. Review the rows before classifying them.", "success")
                conn.commit()
                return redirect(url_for("discrepancy_import_review", batch_id=result["batch_id"]))
            except discrepancy_csv_staging.FatalImportError as exc:
                conn.rollback()
                flash(f"Could not import {original_name}: {exc}", "error")
                return redirect(url_for("discrepancy_import"))
            except Exception:
                conn.rollback()
                log.exception("Unexpected error importing discrepancy CSV %s", original_name)
                flash(f"Could not import {original_name} — an unexpected error occurred.", "error")
                return redirect(url_for("discrepancy_import"))

        return render_template(
            "discrepancy_import.html",
            batches=discrepancy_csv_staging.list_batches(conn),
            customers=conn.execute("SELECT id,name FROM customers ORDER BY name").fetchall(),
        )
    finally:
        conn.close()


@app.route("/discrepancy-import/<int:batch_id>")
def discrepancy_import_review(batch_id):
    conn = get_connection()
    try:
        batch, lines = discrepancy_csv_staging.get_batch(conn, batch_id)
        if batch is None:
            abort(404, description="This batch does not exist.")
        counts = {name: sum(1 for line in lines if line["review_status"] == name)
                  for name in ("ready", "blocked", "ignored")}
        counts["classified"] = sum(1 for line in lines if line["classified_at"] is not None)
        return render_template(
            "discrepancy_import_review.html", batch=batch, lines=lines, counts=counts
        )
    finally:
        conn.close()


@app.route("/discrepancy-import/<int:batch_id>/classify", methods=["POST"])
def classify_discrepancy_batch(batch_id):
    conn = get_connection()
    try:
        batch, _ = discrepancy_csv_staging.get_batch(conn, batch_id)
        if batch is None:
            abort(404, description="This batch does not exist.")
        count = discrepancy_csv_staging.classify_ready(conn, batch_id)
        log_activity(
            conn, "discrepancy_classified", f"Classified {count} discrepancy row(s)",
            "discrepancy_import_batch", str(batch_id),
        )
        conn.commit()
        flash(f"Posted {count} discrepancy row(s) to reporting. Stock quantities were not changed.", "success")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return redirect(url_for("discrepancy_import_review", batch_id=batch_id))


@app.route("/discrepancy-import/<int:batch_id>/revalidate", methods=["POST"])
def revalidate_discrepancy_batch(batch_id):
    conn = get_connection()
    try:
        count = discrepancy_csv_staging.revalidate_batch(conn, batch_id)
        conn.commit()
        flash(f"Rechecked {count} discrepancy row(s) against posted POs and GRNs.", "success")
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), "error")
    finally:
        conn.close()
    return redirect(url_for("discrepancy_import_review", batch_id=batch_id))


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
                log.exception("Unexpected error importing PO CSV %s", original_name)
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


@app.route("/po-import/<int:batch_id>/revalidate", methods=["POST"])
def revalidate_po_batch_route(batch_id):
    conn = get_connection()
    try:
        count = po_csv_staging.revalidate_product_mappings(conn, batch_id)
        log_activity(
            conn,
            "po_batch_revalidated",
            f"Revalidated Master Product mappings for {count} staged PO line(s) in batch {batch_id}",
            "po_import_batch",
            str(batch_id),
        )
        conn.commit()
        flash(f"Revalidated {count} staged PO line(s).", "success")
    except ValueError as e:
        conn.rollback()
        flash(str(e), "error")
    finally:
        conn.close()
    return redirect(url_for("po_import_review", batch_id=batch_id))


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
        if result.get("skipped_existing"):
            exact = sum(p["status"] == "exact_duplicate" for p in result["skipped_existing"])
            review = len(result["skipped_existing"]) - exact
            flash(
                f"Skipped {len(result['skipped_existing'])} order(s) already in the official ledger "
                f"({exact} exact duplicate(s), {review} review item(s)). New orders were not blocked.",
                "warning",
            )
    finally:
        conn.close()
    return redirect(url_for("po_import_review", batch_id=batch_id))


@app.route("/po-import/<int:batch_id>/po/<int:staged_po_id>/duplicate-decision", methods=["POST"])
def duplicate_po_decision(batch_id, staged_po_id):
    conn = get_connection()
    try:
        staged_po = po_csv_staging.get_staged_po(conn, staged_po_id)
        if staged_po is None or staged_po["batch_id"] != batch_id:
            abort(404, description="This staged PO does not belong to this batch.")
        disposition = request.form.get("disposition", "")
        reason = request.form.get("reason", "")
        try:
            official_po_id = po_posting.record_duplicate_decision(conn, staged_po_id, disposition, reason)
            label = "Keep Existing PO" if disposition == "keep_existing" else "Treat as Duplicate"
            log_activity(
                conn, "po_duplicate_reviewed",
                f"{label} for staged PO {staged_po['external_po_number']} against official PO {official_po_id}: {reason.strip()}",
                "staged_purchase_order", str(staged_po_id),
            )
            conn.commit()
            flash(f"Saved decision: {label}. The official PO was not changed.", "success")
        except po_posting.PostingError as e:
            conn.rollback()
            flash(str(e), "error")
    finally:
        conn.close()
    return redirect(url_for("staged_po_detail", batch_id=batch_id, staged_po_id=staged_po_id))


@app.route("/grn-import", methods=["GET", "POST"])
def grn_import():
    """Phase 7: GRN CSV upload. Unlike the PO CSV, this export never
    identifies the customer/buyer -- the operator must explicitly choose
    one, never inferred/defaulted (see grn_csv_staging.py)."""
    conn = get_connection()
    try:
        if request.method == "POST":
            raw_customer_id = request.form.get("customer_id")
            try:
                customer_id = int(raw_customer_id) if raw_customer_id else None
            except ValueError:
                customer_id = None
            if customer_id is None:
                flash("Choose a customer before uploading.", "error")
                return redirect(url_for("grn_import"))
            if conn.execute("SELECT 1 FROM customers WHERE id = ?", (customer_id,)).fetchone() is None:
                flash("That customer no longer exists.", "error")
                return redirect(url_for("grn_import"))

            file = request.files.get("file")
            if not file or file.filename == "":
                flash("Choose a CSV file to upload.", "error")
                return redirect(url_for("grn_import"))

            original_name = secure_filename(file.filename)
            if not original_name.lower().endswith(".csv"):
                flash("GRN CSV import expects a .csv file.", "error")
                return redirect(url_for("grn_import"))

            stored_name = f"grn_csv_{secrets.token_hex(8)}_{original_name}"
            dest = UPLOAD_DIR / stored_name
            file.save(dest)

            try:
                result = grn_csv_staging.stage_grn_csv(conn, str(dest), customer_id, filename=original_name)
                if result["reused_existing_batch"]:
                    flash("This GRN file was already imported for this customer. Opening the existing batch.", "warning")
                else:
                    summary = grn_csv_staging.get_grn_batch_summary(conn, result["batch_id"])
                    log_activity(
                        conn, "grn_csv_upload",
                        f"Staged GRN CSV {original_name} ({summary['grns']} GRNs, {summary['lines']} normalized "
                        f"lines from {summary['raw_rows']} raw rows)",
                        "grn_import_batch", str(result["batch_id"]),
                    )
                    flash(
                        f"Staged {summary['grns']} GRN(s), {summary['lines']} normalized line(s) from "
                        f"{summary['raw_rows']} raw row(s) in {original_name}. Nothing has affected inventory "
                        "or commitments -- review and verify below.",
                        "success",
                    )
                conn.commit()
                return redirect(url_for("grn_import_review", batch_id=result["batch_id"]))
            except grn_csv_staging.FatalImportError as e:
                conn.rollback()
                flash(f"Could not import {original_name}: {e}", "error")
                return redirect(url_for("grn_import"))
            except Exception:
                conn.rollback()
                log.exception("Unexpected error importing GRN CSV %s", original_name)
                flash(f"Could not import {original_name} -- an unexpected error occurred.", "error")
                return redirect(url_for("grn_import"))

        return render_template(
            "grn_import.html",
            batches=grn_csv_staging.list_recent_grn_batches(conn),
            customers=conn.execute("SELECT id, name FROM customers ORDER BY name").fetchall(),
        )
    finally:
        conn.close()


@app.route("/grn-import/<int:batch_id>")
def grn_import_review(batch_id):
    conn = get_connection()
    try:
        batch = grn_csv_staging.get_grn_import_batch(conn, batch_id)
        if batch is None:
            abort(404, description="This batch does not exist.")
        return render_template(
            "grn_import_review.html",
            batch=batch,
            staged_grns=grn_csv_staging.list_staged_grns(conn, batch_id),
            summary=grn_csv_staging.get_grn_batch_summary(conn, batch_id),
        )
    finally:
        conn.close()


@app.route("/grn-import/<int:batch_id>/grn/<int:staged_grn_id>")
def staged_grn_detail(batch_id, staged_grn_id):
    conn = get_connection()
    try:
        batch = grn_csv_staging.get_grn_import_batch(conn, batch_id)
        if batch is None:
            abort(404, description="This batch does not exist.")
        grn = grn_csv_staging.get_staged_grn(conn, staged_grn_id)
        if grn is None or grn["batch_id"] != batch_id:
            abort(404, description="This staged GRN does not belong to this batch.")

        official_po = None
        official_source_location_name = None
        if grn["official_po_id"]:
            official_po = conn.execute(
                "SELECT * FROM purchase_orders WHERE po_id = ?", (grn["official_po_id"],)
            ).fetchone()
            if official_po and official_po["source_location_id"]:
                loc = conn.execute(
                    "SELECT name FROM locations WHERE id = ?", (official_po["source_location_id"],)
                ).fetchone()
                official_source_location_name = loc["name"] if loc else None

        inventory_effect = []
        official_grn = None
        if grn["posted_grn_id"]:
            official_grn = conn.execute(
                "SELECT * FROM grn_receipts WHERE grn_id = ?", (grn["posted_grn_id"],)
            ).fetchone()
            # Scoped via source_grn_line_item_id -> grn_line_items.grn_id
            # (this staged record's OWN posted_grn_id), not reference_id
            # text -- if the GRN this staged record posted has since
            # been superseded by a correction, a plain grn_number match
            # would incorrectly show the REPLACEMENT's movements here
            # instead of this record's own (now-voided) ones.
            inventory_effect = conn.execute(
                """
                SELECT m.quantity, m.product_id, mp.barcode, mp.product_name, lf.name AS from_location, m.voided
                FROM inventory_movements m
                LEFT JOIN master_products mp ON mp.product_id = m.product_id
                LEFT JOIN locations lf ON lf.id = m.location_from_id
                WHERE m.reference_type = 'grn' AND m.source_grn_line_item_id IN (
                    SELECT id FROM grn_line_items WHERE grn_id = ?
                )
                ORDER BY m.id
                """,
                (grn["posted_grn_id"],),
            ).fetchall()

        # Phase 10: offer Correct/Replace only when this staged GRN is
        # not yet posted itself and genuinely conflicts with a currently
        # active official GRN (grn_posting._conflict_failures()'s
        # official_grn_already_exists case) -- never shown otherwise.
        correction_target = None
        if grn["posted_grn_id"] is None:
            correction_target = grn_posting.find_correction_target(conn, staged_grn_id)

        return render_template(
            "staged_grn_detail.html",
            batch=batch,
            grn=grn,
            official_po=official_po,
            official_source_location_name=official_source_location_name,
            official_grn=official_grn,
            inventory_effect=inventory_effect,
            comparison=grn_csv_staging.get_grn_po_comparison(conn, staged_grn_id),
            raw_rows=grn_csv_staging.get_staged_grn_raw_rows(conn, staged_grn_id),
            correction_target=correction_target,
        )
    finally:
        conn.close()


@app.route("/grn-import/<int:batch_id>/revalidate", methods=["POST"])
def revalidate_grn_batch_route(batch_id):
    conn = get_connection()
    try:
        batch = grn_csv_staging.get_grn_import_batch(conn, batch_id)
        if batch is None:
            abort(404, description="This batch does not exist.")
        results = grn_csv_staging.revalidate_grn_batch(conn, batch_id)
        log_activity(
            conn, "grn_batch_revalidated",
            f"Revalidated {len(results)} staged GRN(s) in batch {batch_id}",
            "grn_import_batch", str(batch_id),
        )
        conn.commit()
        flash("GRN verification recalculated for the batch.", "success")
    finally:
        conn.close()
    return redirect(url_for("grn_import_review", batch_id=batch_id))


@app.route("/grn-import/<int:batch_id>/grn/<int:staged_grn_id>/revalidate", methods=["POST"])
def revalidate_single_grn_route(batch_id, staged_grn_id):
    conn = get_connection()
    try:
        batch = grn_csv_staging.get_grn_import_batch(conn, batch_id)
        if batch is None:
            abort(404, description="This batch does not exist.")
        grn = grn_csv_staging.get_staged_grn(conn, staged_grn_id)
        if grn is None or grn["batch_id"] != batch_id:
            abort(404, description="This staged GRN does not belong to this batch.")
        grn_csv_staging.validate_staged_grn(conn, staged_grn_id)
        log_activity(
            conn, "grn_revalidated",
            f"Revalidated staged GRN {grn['external_grn_number']} in batch {batch_id}",
            "staged_grn", str(staged_grn_id),
        )
        conn.commit()
        flash("GRN verification recalculated.", "success")
    finally:
        conn.close()
    return redirect(url_for("staged_grn_detail", batch_id=batch_id, staged_grn_id=staged_grn_id))


@app.route("/grn-import/<int:batch_id>/post", methods=["POST"])
def post_staged_grns_route(batch_id):
    """Phase 8: posts the selected staged GRNs into the official ledger --
    creates official grn_receipts/grn_line_items, canonical SALE
    inventory movements, and closes the matched PO's full commitment. A
    human must explicitly choose which GRNs to post -- this never happens
    automatically on verification/revalidation. Selected posting is
    all-or-nothing: see grn_posting.post_staged_grns()."""
    conn = get_connection()
    try:
        batch = grn_csv_staging.get_grn_import_batch(conn, batch_id)
        if batch is None:
            abort(404, description="This batch does not exist.")

        try:
            staged_grn_ids = [int(i) for i in request.form.getlist("staged_grn_ids")]
        except ValueError:
            flash("Invalid GRN selection.", "error")
            return redirect(url_for("grn_import_review", batch_id=batch_id))
        if not staged_grn_ids:
            flash("Choose at least one verified GRN to post.", "error")
            return redirect(url_for("grn_import_review", batch_id=batch_id))

        try:
            result = grn_posting.post_staged_grns(conn, batch_id, staged_grn_ids)
        except grn_posting.PostingError as e:
            conn.rollback()
            flash(str(e), "error")
            return redirect(url_for("grn_import_review", batch_id=batch_id))

        if result["rejected"]:
            conn.rollback()
            for staged_grn_id, reasons in result["rejected"].items():
                flash(f"Staged GRN id {staged_grn_id} was not posted: {' '.join(reasons)}", "error")
            flash(
                "No GRNs were posted -- posting a selection is all-or-nothing, and at least one "
                "selected GRN was not ready.",
                "error",
            )
            return redirect(url_for("grn_import_review", batch_id=batch_id))

        if result["posted"]:
            grn_numbers = ", ".join(p["grn_number"] for p in result["posted"])
            log_activity(
                conn, "grn_posted",
                f"Posted {len(result['posted'])} GRN(s) to the official ledger from batch {batch_id}: {grn_numbers}",
                "grn_import_batch", str(batch_id),
            )
        conn.commit()

        if result["posted"]:
            flash(
                f"Posted {len(result['posted'])} GRN(s) to the official ledger. This created official "
                "receipt records, reduced inventory by the normalized received quantities, and closed "
                "the related PO commitments.",
                "success",
            )
        if result["already_posted"]:
            flash(
                f"{len(result['already_posted'])} selected GRN(s) were already posted -- no changes made.",
                "warning",
            )
    finally:
        conn.close()
    return redirect(url_for("grn_import_review", batch_id=batch_id))


@app.route("/grn-import/<int:batch_id>/grn/<int:staged_grn_id>/correct", methods=["GET", "POST"])
def correct_grn(batch_id, staged_grn_id):
    """Phase 10: the explicit Correct/Replace workflow for a staged GRN
    that's quarantined because it duplicates an already-posted official
    GRN's grn_number -- never automatic, never inferred from filename/
    timestamp. GET shows a side-by-side comparison and requires a
    reason; POST performs the atomic void-old/post-corrected replacement
    (grn_posting.replace_posted_grn())."""
    conn = get_connection()
    try:
        staged_grn = grn_csv_staging.get_staged_grn(conn, staged_grn_id)
        if staged_grn is None or staged_grn["batch_id"] != batch_id:
            abort(404, description="This staged GRN does not belong to this batch.")

        target = grn_posting.find_correction_target(conn, staged_grn_id)
        if target is None:
            flash("No active official GRN conflicts with this staged GRN -- nothing to correct.", "error")
            return redirect(url_for("staged_grn_detail", batch_id=batch_id, staged_grn_id=staged_grn_id))

        if request.method == "POST":
            reason = (request.form.get("reason") or "").strip()
            try:
                result = grn_posting.replace_posted_grn(conn, target["grn_id"], staged_grn_id, reason)
                log_activity(
                    conn, "grn_replaced",
                    f"Replaced GRN {target['grn_number']} (grn_id {target['grn_id']}) with corrected GRN "
                    f"{result['grn_number']} (grn_id {result['grn_id']}): {reason}",
                    "grn", result["grn_number"],
                )
                conn.commit()
                flash(
                    f"GRN {target['grn_number']} replaced with corrected GRN {result['grn_number']}. The "
                    "original stays in history as VOIDED/SUPERSEDED.",
                    "success",
                )
                return redirect(url_for("lookup", q=result["grn_number"]))
            except (ValueError, grn_posting.CorrectionError) as e:
                conn.rollback()
                flash(str(e), "error")
                return redirect(url_for("correct_grn", batch_id=batch_id, staged_grn_id=staged_grn_id))

        # Scoped by grn_id, not grn_number text -- grn_number alone can't
        # disambiguate once a supersede chain exists (Phase 10).
        old_lines = conn.execute(
            "SELECT * FROM grn_line_items WHERE grn_id = ? ORDER BY sku_code", (target["grn_id"],)
        ).fetchall()
        old_source_location = reconcile.resolve_grn_source_location(conn, target["grn_number"])

        return render_template(
            "grn_correction_confirm.html",
            batch_id=batch_id,
            staged_grn=staged_grn,
            staged_grn_id=staged_grn_id,
            old_grn=target,
            old_lines=old_lines,
            old_source_location=old_source_location,
            comparison=grn_csv_staging.get_grn_po_comparison(conn, staged_grn_id),
        )
    finally:
        conn.close()


@app.route("/po/<po_number>/correct-source", methods=["POST"])
def correct_po_source_route(po_number):
    """Phase 10: the explicit, audited way to change a PO's Drizzl
    source warehouse once it's already been assigned -- blocked
    server-side (see ingest.correct_po_source_location()) if an active
    official GRN already exists against this PO, since that GRN's SALE
    movement(s) were recorded from the current warehouse and changing
    the PO's source alone would not move that history."""
    conn = get_connection()
    try:
        location_name = (request.form.get("source_location") or "").strip()
        reason = (request.form.get("reason") or "").strip()
        if not location_name:
            raise ValueError("Choose a new Drizzl location.")
        correct_po_source_location(conn, po_number, location_name, reason)
        log_activity(
            conn, "po_source_corrected",
            f"Corrected PO {po_number}'s source warehouse to {location_name}: {reason}",
            "po", po_number,
        )
        conn.commit()
        flash(f"PO {po_number}'s source warehouse corrected to {location_name}.", "success")
    except ValueError as e:
        conn.rollback()
        flash(str(e), "error")
    finally:
        conn.close()
    return redirect(url_for("lookup", q=po_number))


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Page not found."), 404


@app.errorhandler(403)
def forbidden(e):
    # CSRFProtect raises a 400 (CSRFError, a subclass of BadRequest) for
    # a missing/invalid token, not 403 -- this handler covers explicit
    # abort(403) calls elsewhere, kept for completeness/consistency.
    return render_template("error.html", code=403, message="You don't have permission to do that."), 403


@app.errorhandler(413)
def too_large(e):
    return render_template("error.html", code=413, message="That file is too large to upload."), 413


@app.errorhandler(400)
def bad_request(e):
    # Covers Flask-WTF's CSRFError among other 400s -- never echoes the
    # underlying reason (e.g. "The CSRF token has expired") verbatim to
    # avoid hinting at exploitable detail; the generic message is enough
    # for a real user (whose session just needs a page reload) and the
    # server log has the specifics.
    log.warning("400 Bad Request on %s: %s", request.path, e)
    return render_template("error.html", code=400, message="That request could not be processed -- please try again."), 400


@app.errorhandler(500)
def server_error(e):
    log.exception("Unhandled server error on %s", request.path)
    return render_template("error.html", code=500, message="Something went wrong on our end."), 500


@app.errorhandler(Exception)
def unhandled_exception(e):
    # Last-resort catch-all so an exception type nobody anticipated
    # still renders the same no-detail-leaked error page instead of
    # Flask's default (which, outside debug mode, is already generic,
    # but this guarantees OUR page/logging, not a framework default
    # that could change) and never the interactive debugger/traceback.
    if isinstance(e, HTTPException):
        return e
    log.exception("Unhandled exception on %s", request.path)
    return render_template("error.html", code=500, message="Something went wrong on our end."), 500


if __name__ == "__main__":
    # debug=True only outside production -- config.DEBUG is False
    # whenever APP_ENV=production, so the interactive debugger and
    # tracebacks can never reach a production browser through this
    # entrypoint. For a real deployment, run behind gunicorn instead
    # of python app.py -- see README ("Running the server").
    app.run(debug=config.DEBUG, port=int(os.environ.get("PORT", 5001)))

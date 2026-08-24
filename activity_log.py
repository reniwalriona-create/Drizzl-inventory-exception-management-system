"""A single running log of everything done on the web app -- document
uploads, manual movements, flag resolutions -- so there's one place to
see what happened without cross-referencing separate tables."""


def log_activity(conn, action_type, description, reference_type=None, reference_id=None):
    actor_username = None
    try:
        from flask import has_request_context
        from flask_login import current_user
        if has_request_context() and current_user.is_authenticated:
            actor_username = current_user.username
    except (ImportError, RuntimeError, AttributeError):
        pass
    conn.execute(
        """
        INSERT INTO activity_log
            (action_type, description, reference_type, reference_id, actor_username)
        VALUES (?, ?, ?, ?, ?)
        """,
        (action_type, description, reference_type, reference_id, actor_username),
    )


def recent_activity(conn, action_type=None, limit=200, date_from=None, date_to=None):
    query = """
        SELECT a.id, a.action_type, a.description, a.reference_type,
               a.reference_id, a.actor_username, a.created_at,
               m.movement_type, m.quantity, m.reason AS movement_reason,
               COALESCE(mp.product_name, p.sku_desc, m.sku_code) AS product_name,
               lf.name AS from_name, lt.name AS to_name
        FROM activity_log a
        LEFT JOIN inventory_movements m ON m.id = CASE
            WHEN a.reference_type = 'movement' AND a.reference_id ~ '^[0-9]+$'
            THEN a.reference_id::INTEGER END
        LEFT JOIN master_products mp ON mp.product_id = m.product_id
        LEFT JOIN products p ON p.sku_code = m.sku_code AND m.product_id IS NULL
        LEFT JOIN locations lf ON lf.id = m.location_from_id
        LEFT JOIN locations lt ON lt.id = m.location_to_id
    """
    params = []
    conditions = []
    if action_type:
        conditions.append("a.action_type = ?")
        params.append(action_type)
    if date_from:
        conditions.append("a.created_at::date >= ?::date")
        params.append(date_from)
    if date_to:
        conditions.append("a.created_at::date <= ?::date")
        params.append(date_to)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY a.created_at DESC, a.id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    action_labels = {
        "movement": "Movement logged", "movement_voided": "Movement voided",
        "movement_restored": "Movement restored", "po_csv_upload": "PO file uploaded",
        "grn_csv_upload": "GRN file uploaded", "po_posted": "POs posted",
        "grn_posted": "GRNs posted", "po_voided": "PO voided",
        "po_restored": "PO restored", "grn_voided": "GRN voided",
        "grn_restored": "GRN restored", "flag_resolved": "Flag resolved",
        "po_location_assigned": "PO source set", "grn_location_assigned": "GRN source set",
        "po_staged_source_assigned": "Batch source set",
        "po_batch_revalidated": "PO batch checked", "grn_batch_revalidated": "GRN batch checked",
        "discrepancy_csv_upload": "Discrepancy file uploaded",
        "discrepancy_classified": "Discrepancies classified",
    }
    result = []
    for raw in rows:
        row = dict(raw)
        row["action_label"] = action_labels.get(row["action_type"], row["action_type"].replace("_", " ").title())
        if row["movement_type"]:
            route = " → ".join(x for x in (row["from_name"], row["to_name"]) if x)
            row["short_details"] = f"{row['quantity']:g} × {row['product_name'] or 'Product'}" + (f" · {route}" if route else "")
            row["short_reason"] = row["movement_reason"] or ""
        else:
            row["short_details"] = (
                f"{row['reference_type'].replace('_', ' ').title()} {row['reference_id']}"
                if row["reference_type"] and row["reference_id"] else row["description"]
            )
            row["short_reason"] = ""
        result.append(row)
    return result

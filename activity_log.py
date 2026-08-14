"""A single running log of everything done on the web app -- document
uploads, manual movements, flag resolutions -- so there's one place to
see what happened without cross-referencing separate tables."""


def log_activity(conn, action_type, description, reference_type=None, reference_id=None):
    conn.execute(
        """
        INSERT INTO activity_log (action_type, description, reference_type, reference_id)
        VALUES (?, ?, ?, ?)
        """,
        (action_type, description, reference_type, reference_id),
    )


def recent_activity(conn, action_type=None, limit=200):
    query = "SELECT id, action_type, description, reference_type, reference_id, created_at FROM activity_log"
    params = []
    if action_type:
        query += " WHERE action_type = ?"
        params.append(action_type)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(query, params).fetchall()

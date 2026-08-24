-- Preserve who performed each web action. Username is stored as an audit
-- snapshot so the log remains readable even if a user account changes later.
ALTER TABLE activity_log
    ADD COLUMN IF NOT EXISTS actor_username TEXT;

"""
Central place for environment-driven configuration (Phase 12). Nothing
here has a business-logic opinion -- it just decides where secrets and
the database connection string come from, and enforces that production
can't silently run with a placeholder secret.

APP_ENV controls the split:
  - "production": SECRET_KEY and DATABASE_URL are REQUIRED -- missing
    either raises at import time (fail loudly at startup, not with a
    confusing runtime error later, and never by silently falling back
    to something insecure).
  - anything else (unset, "development", "dev"): safe to run locally
    without a .env file -- SECRET_KEY falls back to a fixed, clearly-
    labeled development-only value (never used if APP_ENV=production),
    and DATABASE_URL falls back to the local Postgres database name
    db.py has always used.

Real secrets are never committed -- see .env.example for the documented
list of variables and start_server.sh / the README for how to set them.
"""
import os
import sys

APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"

_DEV_SECRET_KEY = "dev-only-secret-never-used-in-production"

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    if IS_PRODUCTION:
        sys.exit(
            "FATAL: SECRET_KEY is not set. Refusing to start with APP_ENV=production and no "
            "SECRET_KEY -- set it in the environment (see .env.example). A guessable session "
            "secret in production would let an attacker forge login sessions and CSRF tokens."
        )
    SECRET_KEY = _DEV_SECRET_KEY

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    if IS_PRODUCTION:
        sys.exit(
            "FATAL: DATABASE_URL is not set. Refusing to start with APP_ENV=production and no "
            "DATABASE_URL -- set it in the environment (see .env.example)."
        )
    # Local dev convenience -- matches db.py's long-standing default of
    # connecting to a local Postgres database named drizzl_inventory via
    # peer/trust auth, no password needed.
    DATABASE_URL = "dbname=drizzl_inventory"

# MAX_CONTENT_LENGTH: generous but bounded upload size (Phase 12) -- real
# GRN/PO documents (PDF or CSV) are at most a few MB; this exists to stop
# an accidental or malicious huge upload from exhausting disk/memory, not
# to constrain legitimate use.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

DEBUG = not IS_PRODUCTION

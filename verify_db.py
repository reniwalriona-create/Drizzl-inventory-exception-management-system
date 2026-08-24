"""Disposable PostgreSQL database helpers for verification suites.

Automated verification must never connect to the development database.  Each
suite gets a dedicated throwaway database, built from the same fresh-install
schema and seed path as the application, and drops it in a ``finally`` block.
"""
import psycopg2

import config
import db as db_module


def _admin_connection():
    conn = psycopg2.connect(dbname="postgres")
    conn.autocommit = True
    return conn


def create_database(name):
    admin = _admin_connection()
    try:
        cur = admin.cursor()
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        cur.execute(f'CREATE DATABASE "{name}"')
        cur.close()
    finally:
        admin.close()


def drop_database(name):
    admin = _admin_connection()
    try:
        cur = admin.cursor()
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        cur.close()
    finally:
        admin.close()


def point_app_at(name):
    """Route every later db.get_connection() call to the throwaway DB."""
    config.DATABASE_URL = f"dbname={name}"


def bootstrap_connection(name):
    """Create the current schema and reference seed in a fresh test DB."""
    point_app_at(name)
    return db_module.get_connection()

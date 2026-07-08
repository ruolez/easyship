import os

import psycopg2
import psycopg2.extras
from flask import g
from werkzeug.security import generate_password_hash

import config

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


def connect():
    return psycopg2.connect(**config.POSTGRES)


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, params=None, one=False):
    with get_db().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        if cur.description is None:
            rows = None
        else:
            rows = cur.fetchall()
    get_db().commit()
    if rows is None:
        return None
    return (rows[0] if rows else None) if one else rows


def execute(sql, params=None, returning=False):
    with get_db().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        row = cur.fetchone() if returning else None
    get_db().commit()
    return row


def get_setting(key, default=None):
    row = query("SELECT value FROM settings WHERE key = %s", (key,), one=True)
    return row["value"] if row and row["value"] is not None else default


def set_setting(key, value):
    execute(
        """INSERT INTO settings (key, value) VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        (key, value),
    )


def run_migrations():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       filename TEXT PRIMARY KEY,
                       applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                   )"""
            )
            cur.execute("SELECT filename FROM schema_migrations")
            applied = {r[0] for r in cur.fetchall()}
            for fname in sorted(os.listdir(MIGRATIONS_DIR)):
                if not fname.endswith(".sql") or fname in applied:
                    continue
                with open(os.path.join(MIGRATIONS_DIR, fname)) as f:
                    cur.execute(f.read())
                cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (fname,))
        conn.commit()
        _seed_admin(conn)
    finally:
        conn.close()


def _seed_admin(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM users")
        if cur.fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, 'admin')",
                ("admin", generate_password_hash(config.ADMIN_INITIAL_PASSWORD)),
            )
    conn.commit()

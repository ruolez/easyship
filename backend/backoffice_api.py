import pymssql
from flask import Blueprint, jsonify, request

import db
from auth import admin_required, login_required
from backoffice import (
    BackofficeError,
    find_invoice_id_by_number,
    get_invoice,
    list_open_invoices,
)
from util import api_error, audit

bp = Blueprint("backoffice", __name__, url_prefix="/api")

MASK = "••••••••"


# ---------- BackOffice database connections (admin CRUD) ----------

@bp.get("/backoffice-dbs")
@login_required
def list_dbs():
    rows = db.query(
        "SELECT id, name, host, port, db_name, username, prefix, is_active FROM backoffice_dbs ORDER BY id"
    )
    return jsonify(rows)


@bp.post("/backoffice-dbs")
@admin_required
def create_db():
    data = request.get_json(silent=True) or {}
    required = ["name", "host", "db_name", "username", "password"]
    if not all((data.get(k) or "").strip() for k in required):
        return api_error("Name, host, database, username and password are required")
    row = db.execute(
        """INSERT INTO backoffice_dbs (name, host, port, db_name, username, password, prefix)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (
            data["name"].strip(), data["host"].strip(),
            (data.get("port") or "1433").strip(), data["db_name"].strip(),
            data["username"].strip(), data["password"],
            (data.get("prefix") or "").strip(),
        ),
        returning=True,
    )
    audit("backoffice_db.create", {"name": data["name"]})
    return jsonify({"id": row["id"]})


@bp.put("/backoffice-dbs/<int:db_id>")
@admin_required
def update_db(db_id):
    data = request.get_json(silent=True) or {}
    row = db.query("SELECT * FROM backoffice_dbs WHERE id = %s", (db_id,), one=True)
    if not row:
        return api_error("Database not found", 404)
    password = data.get("password") or ""
    if not password or password == MASK:
        password = row["password"]
    prefix = data.get("prefix")
    prefix = prefix.strip() if prefix is not None else row["prefix"]
    db.execute(
        """UPDATE backoffice_dbs SET name=%s, host=%s, port=%s, db_name=%s,
           username=%s, password=%s, prefix=%s, is_active=%s WHERE id=%s""",
        (
            (data.get("name") or row["name"]).strip(),
            (data.get("host") or row["host"]).strip(),
            (data.get("port") or row["port"] or "1433").strip(),
            (data.get("db_name") or row["db_name"]).strip(),
            (data.get("username") or row["username"]).strip(),
            password,
            prefix,
            bool(data.get("is_active", row["is_active"])),
            db_id,
        ),
    )
    audit("backoffice_db.update", {"id": db_id})
    return jsonify({"ok": True})


@bp.delete("/backoffice-dbs/<int:db_id>")
@admin_required
def delete_db(db_id):
    used = db.query(
        "SELECT 1 FROM shipments WHERE backoffice_db_id = %s LIMIT 1", (db_id,), one=True
    )
    if used:
        db.execute("UPDATE backoffice_dbs SET is_active = FALSE WHERE id = %s", (db_id,))
    else:
        db.execute("DELETE FROM backoffice_dbs WHERE id = %s", (db_id,))
    audit("backoffice_db.delete", {"id": db_id})
    return jsonify({"ok": True})


@bp.post("/backoffice-dbs/<int:db_id>/test")
@admin_required
def test_db(db_id):
    row = db.query("SELECT * FROM backoffice_dbs WHERE id = %s", (db_id,), one=True)
    if not row:
        return api_error("Database not found", 404)
    try:
        conn = pymssql.connect(
            server=row["host"], port=int(row["port"] or 1433), database=row["db_name"],
            user=row["username"], password=row["password"], timeout=10, login_timeout=10,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT TOP 1 InvoiceID FROM Invoices_tbl ORDER BY InvoiceID DESC")
            cur.fetchone()
        conn.close()
    except Exception as e:
        return api_error(f"Connection failed: {e}")
    return jsonify({"ok": True})


# ---------- Invoices ----------

@bp.get("/backoffice/<int:db_id>/invoices")
@login_required
def invoices(db_id):
    days = request.args.get("days", default=14, type=int)
    q = (request.args.get("q") or "").strip()
    try:
        return jsonify(list_open_invoices(db_id, days=days, q=q))
    except BackofficeError as e:
        return api_error(str(e), 502)


@bp.get("/backoffice/<int:db_id>/invoices/<int:invoice_id>")
@login_required
def invoice_detail(db_id, invoice_id):
    try:
        return jsonify(get_invoice(db_id, invoice_id))
    except BackofficeError as e:
        return api_error(str(e), 502)


@bp.get("/backoffice/<int:db_id>/lookup")
@login_required
def lookup(db_id):
    number = (request.args.get("number") or "").strip()
    if not number:
        return api_error("number is required")
    try:
        invoice_id = find_invoice_id_by_number(db_id, number)
        if not invoice_id:
            return api_error(f"Invoice {number} not found", 404)
        return jsonify(get_invoice(db_id, invoice_id))
    except BackofficeError as e:
        return api_error(str(e), 502)

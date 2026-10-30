"""DB_Admin: the SQL Server database that knows which BackOffice database
each quotation lives in.

QuotationsStatus has a row per quotation: SourceDB is the BackOffice
database's nickname and Dop1 its QuotationID there. AdminDBs_admin has the
connection for every nickname (Nick). After verifying, Checker gets the
EasyShip username.
"""
import time

import pymssql

import db

SETTING_KEYS = {k: f"dbadmin_{k}" for k in ("host", "port", "db", "user", "password")}
CLOSED_STATUSES = ("CONVERTED", "DELETED")
SOURCES_TTL_SECONDS = 300

_sources_cache = {"at": 0.0, "rows": None}


class DbAdminError(Exception):
    pass


def get_config():
    cfg = {k: (db.get_setting(key) or "").strip() for k, key in SETTING_KEYS.items()}
    if not (cfg["host"] and cfg["db"] and cfg["user"]):
        return None
    return cfg


def connect_with(cfg, timeout=15):
    try:
        return pymssql.connect(
            server=cfg["host"], port=int(cfg["port"] or 1433), database=cfg["db"],
            user=cfg["user"], password=cfg["password"], timeout=timeout, login_timeout=10,
        )
    except Exception as e:
        raise DbAdminError(f"DB_Admin connection failed: {e}")


def _connect():
    cfg = get_config()
    if cfg is None:
        raise DbAdminError("DB_Admin is not configured — set it up in Settings › Verify")
    return connect_with(cfg)


def find_quotation(number):
    """The open QuotationsStatus row for a quotation number, or None."""
    conn = _connect()
    try:
        with conn.cursor(as_dict=True) as cur:
            cur.execute(
                """SELECT TOP 1 QuotationNumber, SourceDB, Dop1, BusinessName, Status
                   FROM QuotationsStatus
                   WHERE LTRIM(RTRIM(QuotationNumber)) = %s
                     AND (Status IS NULL OR UPPER(LTRIM(RTRIM(Status))) NOT IN (%s, %s))
                   ORDER BY id DESC""",
                ((number or "").strip(), *CLOSED_STATUSES),
            )
            return cur.fetchone()
    finally:
        conn.close()


def forget_sources():
    _sources_cache["rows"] = None


def _source_rows():
    now = time.monotonic()
    if _sources_cache["rows"] is None or now - _sources_cache["at"] > SOURCES_TTL_SECONDS:
        conn = _connect()
        try:
            with conn.cursor(as_dict=True) as cur:
                cur.execute("SELECT Nick, ipAddress, ShareName, Port, NameDB, Username, Password FROM AdminDBs_admin")
                _sources_cache["rows"] = cur.fetchall()
                _sources_cache["at"] = now
        finally:
            conn.close()
    return _sources_cache["rows"]


def source_config(row):
    """An AdminDBs_admin row → pymssql arguments. With a port the instance
    name (ShareName) is not needed; without one it names the instance."""
    host = (row.get("ipAddress") or "").strip()
    instance = (row.get("ShareName") or "").strip()
    port = str(row.get("Port") or "").strip()
    return {
        "server": host if port or not instance else f"{host}\\{instance}",
        "port": int(port) if port.isdigit() else 1433,
        "database": (row.get("NameDB") or "").strip(),
        "user": (row.get("Username") or "").strip(),
        "password": row.get("Password") or "",
    }


def connect_source(nick):
    rows = [r for r in _source_rows() if (r.get("Nick") or "").strip() == (nick or "").strip()]
    if not rows:
        _sources_cache["rows"] = None
        raise DbAdminError(f"BackOffice database “{nick}” is not listed in DB_Admin AdminDBs_admin")
    cfg = source_config(rows[0])
    try:
        return pymssql.connect(timeout=15, login_timeout=10, **cfg)
    except Exception as e:
        raise DbAdminError(f"BackOffice connection failed ({nick}): {e}")


def read_quotation(nick, quotation_id, number):
    """The quotation's header and lines from its BackOffice database. The
    number is checked too, so a stale Dop1 can never load another quotation."""
    conn = connect_source(nick)
    try:
        with conn.cursor(as_dict=True) as cur:
            cur.execute(
                """SELECT QuotationID, QuotationNumber, BusinessName, AutoOrderNo FROM Quotations_tbl
                   WHERE QuotationID = %s AND LTRIM(RTRIM(QuotationNumber)) = %s""",
                (quotation_id, (number or "").strip()),
            )
            header = cur.fetchone()
            if not header:
                raise DbAdminError(f"Quotation {number} was not found in {nick}")
            cur.execute(
                """SELECT ProductUPC, ProductSKU, ProductDescription, Qty FROM QuotationsDetails_tbl
                   WHERE QuotationID = %s ORDER BY LineID""",
                (quotation_id,),
            )
            return header, cur.fetchall()
    finally:
        conn.close()


def write_checker(number, username):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE QuotationsStatus SET Checker = %s
                   WHERE LTRIM(RTRIM(QuotationNumber)) = %s
                     AND (Status IS NULL OR UPPER(LTRIM(RTRIM(Status))) NOT IN (%s, %s))""",
                (username, (number or "").strip(), *CLOSED_STATUSES),
            )
            if cur.rowcount == 0:
                raise DbAdminError(f"Quotation {number} has no open QuotationsStatus row")
        conn.commit()
    finally:
        conn.close()

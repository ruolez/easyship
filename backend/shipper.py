import pymssql

import backoffice
import db


class ShipperError(Exception):
    pass


def get_config():
    cfg = {
        k: (db.get_setting(f"shipper_{k}") or "").strip()
        for k in ("host", "port", "db", "user", "password")
    }
    if not (cfg["host"] and cfg["db"] and cfg["user"]):
        return None
    return cfg


def _connect(cfg):
    try:
        return pymssql.connect(
            server=cfg["host"], port=int(cfg["port"] or 1433), database=cfg["db"],
            user=cfg["user"], password=cfg["password"], timeout=5, login_timeout=5,
        )
    except Exception as e:
        raise ShipperError(f"Shipper connection failed: {e}")


def _check_parcel(cfg, candidates):
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            placeholders = ", ".join(["%s"] * len(candidates))
            cur.execute(
                f"""
                SELECT TOP 1 check_completed_at FROM parcels
                WHERE LTRIM(RTRIM(order_number)) IN ({placeholders})
                ORDER BY CASE WHEN check_completed_at IS NOT NULL THEN 0 ELSE 1 END,
                         created_at DESC
                """,
                tuple(candidates),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return {"status": "not_found", "checked_at": None}
    if row[0] is None:
        return {"status": "unverified", "checked_at": None}
    return {"status": "verified", "checked_at": row[0].isoformat()}


def check_shopify(order_name):
    cfg = get_config()
    if cfg is None:
        return {"status": "not_configured", "checked_at": None}
    bare = (order_name or "").strip().lstrip("#")
    if not bare:
        raise ShipperError("Order name is required")
    return _check_parcel(cfg, [f"#{bare}", bare])


def check_backoffice(db_id, number):
    cfg = get_config()
    if cfg is None:
        return {"status": "not_configured", "checked_at": None}
    conn = backoffice._connect(db_id)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT TOP 1 LTRIM(RTRIM(QuotationNumber)) FROM Quotations_tbl
                WHERE LTRIM(RTRIM(AutoOrderNo)) = %s
                ORDER BY QuotationID DESC
                """,
                ((number or "").strip(),),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return {"status": "not_found", "checked_at": None}
    return _check_parcel(cfg, [row[0]])

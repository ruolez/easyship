"""End-of-day manifests (USPS SCAN forms).

A manifest covers already-purchased labels: the packer selects today's USPS
parcels, the provider issues one barcode document for the whole pickup, and
the PDF is stored locally (the provider's URLs are signed and expire).
Shipments are claimed atomically (shipments.manifest_id) before the provider
call so two stations can't manifest the same label, and un-claimed again if
the call fails.
"""
import json
import os

from flask import Blueprint, jsonify, request, send_file, session

import config
import db
import providers
from auth import login_required
from providers.base import ProviderError
from shipments_api import LIST_SELECT, _row_to_json
from util import api_error, audit, central_time

bp = Blueprint("manifests", __name__, url_prefix="/api/manifests")

# Which labeled shipments may go on a USPS manifest: today's (Central) USPS
# labels for the chosen instance, not yet on a manifest. 'stamps' covers
# ShipStation accounts whose USPS connection is named Stamps.com; 'endicia'
# the Endicia-via-ShipStation instances, whose labels carry the alias.
ELIGIBLE_WHERE = """
    s.provider = %s
    AND s.status IN ('label_created', 'fulfilled')
    AND s.manifest_id IS NULL
    AND s.easyship_shipment_id IS NOT NULL
    AND (s.courier_umbrella_name ILIKE '%%usps%%'
         OR s.courier_umbrella_name ILIKE '%%stamps%%'
         OR s.courier_umbrella_name ILIKE '%%endicia%%'
         OR s.courier_name ILIKE '%%usps%%')
    AND (s.label_created_at AT TIME ZONE 'America/Chicago')::date
        = (now() AT TIME ZONE 'America/Chicago')::date
"""

MANIFEST_SELECT = """
    SELECT m.*, u.username AS created_by_username, pi.label AS provider_current_label
    FROM manifests m
    LEFT JOIN users u ON u.id = m.created_by
    LEFT JOIN provider_instances pi ON pi.key = m.provider
"""


def _user_provider(name):
    """The named instance if this user may ship with it, else an error response."""
    if not name:
        return None, api_error("provider is required")
    allowed = providers.enabled_for_user(session["user_id"], session.get("role"))
    provider = next((p for p in allowed if p.name == name), None)
    if provider is None:
        return None, api_error(f"Shipping provider '{name}' is not enabled for your account", 403)
    return provider, None


def _manifest_json(row):
    return {
        "id": row["id"],
        "provider": row["provider"],
        # Live alias while the instance exists, the snapshot after it's deleted.
        "provider_label": (row.get("provider_current_label") or row.get("provider_label")
                           or row["provider"]),
        "carrier": row.get("carrier"),
        "provider_manifest_id": row.get("provider_manifest_id"),
        "ref_number": row.get("ref_number"),
        "shipment_count": row.get("shipment_count") or 0,
        "status": row["status"],
        "error_message": row.get("error_message"),
        "has_document": bool(row.get("document_path")),
        "created_by": row.get("created_by_username") or row.get("created_by"),
        "created_at": central_time(row.get("created_at")),
    }


@bp.get("/eligible")
@login_required
def eligible():
    name = (request.args.get("provider") or "").strip()
    provider, err = _user_provider(name)
    if err:
        return err
    if not provider.supports_manifests():
        return jsonify({"supported": False, "shipments": []})
    rows = db.query(
        LIST_SELECT + " WHERE " + ELIGIBLE_WHERE
        + " ORDER BY s.label_created_at DESC, s.box_number ASC",
        (name,),
    )
    return jsonify({"supported": True, "shipments": [_row_to_json(r) for r in rows]})


@bp.get("")
@login_required
def list_manifests():
    limit = min(int(request.args.get("limit") or 100), 500)
    rows = db.query(MANIFEST_SELECT + " ORDER BY m.created_at DESC LIMIT %s", (limit,))
    return jsonify([_manifest_json(r) for r in rows])


@bp.post("")
@login_required
def create_manifest():
    data = request.get_json(silent=True) or {}
    name = (data.get("provider") or "").strip()
    provider, err = _user_provider(name)
    if err:
        return err
    if not provider.supports_manifests():
        return api_error(f"{provider.label} does not support USPS manifests")
    try:
        requested = sorted({int(i) for i in data.get("shipment_ids") or []})
    except (TypeError, ValueError):
        return api_error("shipment_ids must be a list of shipment ids")
    if not requested:
        return api_error("Select at least one parcel to manifest")

    rows = db.query(
        "SELECT id, easyship_shipment_id FROM shipments s WHERE s.id = ANY(%s) AND " + ELIGIBLE_WHERE,
        (requested, name),
    )
    found = {r["id"] for r in rows}
    missing = [f"#{i}" for i in requested if i not in found]
    if missing:
        return api_error(
            "Not eligible for a manifest (already manifested, not USPS, no label, "
            "or not from today): " + ", ".join(missing)
        )

    # A multi-box ShipStation group shares one label id across its rows — pull
    # in every sibling so a label is never half-manifested.
    provider_ids = sorted({r["easyship_shipment_id"] for r in rows})
    claim_ids = sorted({r["id"] for r in db.query(
        """SELECT id FROM shipments
           WHERE provider = %s AND easyship_shipment_id = ANY(%s) AND manifest_id IS NULL""",
        (name, provider_ids),
    )})

    mrow = db.execute(
        """INSERT INTO manifests (provider, provider_label, carrier, shipment_count, status, created_by)
           VALUES (%s, %s, 'USPS', %s, 'creating', %s) RETURNING id""",
        (name, provider.label, len(claim_ids), session["user_id"]),
        returning=True,
    )
    mid = mrow["id"]

    # Atomic claim — a row another station already claimed stays claimed, and a
    # shortfall means a race: undo and let the packer refresh.
    claimed = db.query(
        """UPDATE shipments SET manifest_id = %s, updated_at = now()
           WHERE id = ANY(%s) AND manifest_id IS NULL
           RETURNING id, easyship_shipment_id""",
        (mid, claim_ids),
    )
    if len(claimed) < len(claim_ids):
        _rollback_manifest(mid)
        return api_error(
            "Another station just manifested some of these parcels — refresh and try again", 409)

    try:
        results = provider.create_manifest(sorted({r["easyship_shipment_id"] for r in claimed}))
        if not results:
            raise ProviderError("The provider returned no manifest")
    except ProviderError as e:
        db.execute(
            "UPDATE shipments SET manifest_id = NULL, updated_at = now() WHERE manifest_id = %s",
            (mid,))
        db.execute(
            "UPDATE manifests SET status = 'failed', error_message = %s, updated_at = now() WHERE id = %s",
            (str(e), mid))
        return api_error(str(e), 502)

    manifest_ids = _store_results(mid, name, provider.label, results, claimed)
    audit("manifest.create", {
        "manifest_ids": manifest_ids,
        "provider": name,
        "shipments": len(claimed),
        "shipment_ids": [r["id"] for r in claimed],
    })
    out = db.query(MANIFEST_SELECT + " WHERE m.id = ANY(%s) ORDER BY m.id", (manifest_ids,))
    return jsonify({"manifests": [_manifest_json(r) for r in out]})


def _rollback_manifest(mid):
    db.execute(
        "UPDATE shipments SET manifest_id = NULL, updated_at = now() WHERE manifest_id = %s", (mid,))
    db.execute("DELETE FROM manifests WHERE id = %s", (mid,))


def _store_results(mid, name, provider_label, results, claimed):
    """Persist the provider's manifest(s): the first fills the pre-created row;
    extras (a platform splitting by carrier account) get their own rows, with
    the covered shipments re-pointed at them."""
    os.makedirs(config.MANIFESTS_DIR, exist_ok=True)
    ids = []
    for i, result in enumerate(results):
        covered_pids = set(result.provider_shipment_ids or [])
        covered_rows = [r["id"] for r in claimed if r["easyship_shipment_id"] in covered_pids]
        if i == 0:
            row_id = mid
        else:
            row = db.execute(
                """INSERT INTO manifests (provider, provider_label, carrier, status, created_by)
                   VALUES (%s, %s, 'USPS', 'creating', %s) RETURNING id""",
                (name, provider_label, session["user_id"]),
                returning=True,
            )
            row_id = row["id"]
            if covered_rows:
                db.execute(
                    "UPDATE shipments SET manifest_id = %s, updated_at = now() WHERE id = ANY(%s)",
                    (row_id, covered_rows))
        document_path = None
        if result.document and result.document[0]:
            document_path = os.path.join(config.MANIFESTS_DIR, f"{row_id}.pdf")
            with open(document_path, "wb") as f:
                f.write(result.document[0])
        db.execute(
            """UPDATE manifests SET status = 'ready', provider_manifest_id = %s, ref_number = %s,
               shipment_count = %s, document_path = %s, raw = %s, error_message = NULL,
               updated_at = now() WHERE id = %s""",
            (result.provider_manifest_id, result.ref_number,
             len(covered_rows) or result.shipment_count, document_path,
             json.dumps(result.raw, default=str), row_id),
        )
        ids.append(row_id)
    return ids


@bp.get("/<int:manifest_id>/document")
@login_required
def get_document(manifest_id):
    row = db.query("SELECT * FROM manifests WHERE id = %s", (manifest_id,), one=True)
    if not row or not row["document_path"]:
        return api_error("No document stored for this manifest", 404)
    if not os.path.exists(row["document_path"]):
        return api_error("Manifest file missing from storage", 410)
    response = send_file(
        row["document_path"],
        mimetype="application/pdf",
        download_name=f"manifest-{manifest_id}.pdf",
        as_attachment=False,
    )
    response.headers["Content-Disposition"] = f'inline; filename="manifest-{manifest_id}.pdf"'
    return response

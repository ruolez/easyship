import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp",
    MANIFESTS_DIR="/tmp"))

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import manifests_api  # noqa: E402
import providers  # noqa: E402
from providers.base import ManifestResult  # noqa: E402
from providers.speedee import SpeedeeProvider  # noqa: E402
from providers.shipstation import ShipStationProvider  # noqa: E402


class SheetSpeedee(SpeedeeProvider):
    def __init__(self):
        super().__init__()
        self.manifested = []

    def create_manifest(self, provider_shipment_ids):
        self.manifested.append(provider_shipment_ids)
        return [ManifestResult(provider_manifest_id=None, ref_number="11/02/2026 · 2 pkg", shipment_count=2,
                               provider_shipment_ids=provider_shipment_ids, document=None)]


class ManifestsApiTest(unittest.TestCase):
    """Spee-Dee manifests every label (not just USPS), names its carrier, and
    claims both boxes of one order even when only one was selected."""

    def setUp(self):
        self.addCleanup(setattr, auth, "user_ships", auth.user_ships)
        auth.user_ships = lambda user_id, role: True
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(manifests_api.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "user"
        self.queries, self.executed = [], []
        self.provider = SheetSpeedee()
        db = manifests_api.db
        orig = (db.query, db.execute, providers.enabled_for_user, manifests_api.audit)
        self.addCleanup(self._restore, orig)
        db.query = self.query
        db.execute = self.execute
        providers.enabled_for_user = lambda user_id, role: [self.provider]
        manifests_api.audit = lambda *a, **k: None

    def _restore(self, orig):
        db = manifests_api.db
        db.query, db.execute, providers.enabled_for_user, manifests_api.audit = orig

    def query(self, sql, params=None, one=False):
        self.queries.append((sql, params))
        if "s.id = ANY" in sql:
            return [{"id": 11, "easyship_shipment_id": "SD3041#1"}]
        if "split_part" in sql:
            return [{"id": 11}, {"id": 12}]
        if sql.startswith("UPDATE shipments SET manifest_id"):
            return [{"id": 11, "easyship_shipment_id": "SD3041#1"},
                    {"id": 12, "easyship_shipment_id": "SD3041#2"}]
        if "FROM manifests m" in sql:
            return [{"id": 5, "provider": "speedee", "carrier": "Spee-Dee", "status": "ready"}]
        return []

    def execute(self, sql, params=None, returning=False):
        self.executed.append((sql, params))
        return {"id": 5} if returning else None

    def test_eligible_reports_carrier_and_lifts_the_usps_filter(self):
        res = self.client.get("/api/manifests/eligible?provider=speedee")
        self.assertEqual((res.status_code, res.get_json()),
                         (200, {"supported": True, "carrier": "Spee-Dee", "shipments": []}))
        self.assertEqual(self.queries[0][1], ("speedee", True))

    def test_usps_platforms_keep_the_usps_filter(self):
        self.assertEqual((ShipStationProvider.manifest_carrier, ShipStationProvider.manifests_every_label),
                         ("USPS", False))

    def test_create_claims_every_box_of_the_order_and_stores_the_carrier(self):
        res = self.client.post("/api/manifests", json={"provider": "speedee", "shipment_ids": [11]})
        self.assertEqual(res.status_code, 200)
        sibling_sql, sibling_params = next(q for q in self.queries if "split_part" in q[0])
        self.assertEqual(sibling_params, ("speedee", ["SD3041"]))
        insert_sql, insert_params = next(e for e in self.executed if e[0].strip().startswith("INSERT INTO manifests"))
        self.assertEqual(insert_params, ("speedee", "Spee-Dee", "Spee-Dee", 2, 1))
        self.assertEqual(self.provider.manifested, [["SD3041#1", "SD3041#2"]])


if __name__ == "__main__":
    unittest.main()

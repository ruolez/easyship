"""Easyship implementation of the ShippingProvider interface.

A thin adapter over the existing `easyship_client` module: it keeps all the
tested HTTP/throttle/retry/unit-conversion code and just normalizes Easyship's
shapes into the provider-agnostic types.
"""
import requests

import config
import easyship_client as ec
from providers.base import (
    DraftShipment,
    LabelStatus,
    ManifestResult,
    ProviderError,
    Rate,
    ShipmentState,
    ShippingProvider,
    origin_descriptor,
)

# Easyship label_state values that mean the label is bought and printable.
_READY_STATES = {"generated", "printed", "shipping_document_generated"}


def _shipment_courier_id(shipment):
    """A courier (account) id if the raw shipment payload carries one — the
    field name varies across payload shapes, so try the known spots."""
    courier = shipment.get("courier") or {}
    service = shipment.get("courier_service") or {}
    for candidate in (courier.get("id"), service.get("courier_id"), shipment.get("courier_id")):
        if candidate:
            return candidate
    return None


def _label_status(shipment):
    ls = shipment.get("label_state")
    if ls in _READY_STATES:
        return LabelStatus.READY
    if ls == "failed":
        return LabelStatus.FAILED
    if ls in (None, "not_created"):
        return LabelStatus.NOT_CREATED
    return LabelStatus.PENDING


def _per_box_cost(shipment, service_id):
    """The chosen service's charge for this single box, if Easyship quoted it."""
    if not service_id:
        return None
    for r in shipment.get("rates") or []:
        if (r.get("courier_service") or {}).get("id") == service_id:
            return r.get("total_charge")
    return None


def _to_state(shipment, service_id=None):
    courier = shipment.get("courier_service") or {}
    return ShipmentState(
        provider_shipment_id=shipment.get("easyship_shipment_id"),
        label_status=_label_status(shipment),
        tracking_numbers=ec.extract_tracking_numbers(shipment),
        courier_name=courier.get("name"),
        courier_umbrella_name=courier.get("umbrella_name"),
        cost=_per_box_cost(shipment, service_id),
        raw=shipment,
    )


def _combine_rates(es_list, provider="easyship"):
    """One quote list across per-box shipments: only couriers that can serve
    EVERY box, price = sum across boxes. `provider` is the instance key."""
    rate_maps = [
        {r["courier_service"]["id"]: r for r in (s.get("rates") or [])}
        for s in es_list
    ]
    common = set(rate_maps[0])
    for m in rate_maps[1:]:
        common &= set(m)
    combined = []
    for cid in common:
        rs = [m[cid] for m in rate_maps]
        combined.append(Rate(
            provider=provider,
            provider_service_id=cid,
            courier_name=rs[0]["courier_service"].get("name"),
            umbrella_name=rs[0]["courier_service"].get("umbrella_name"),
            total_charge=round(sum(r.get("total_charge") or 0 for r in rs), 2),
            currency=rs[0].get("currency"),
            min_delivery_time=max((r.get("min_delivery_time") or 0) for r in rs) or None,
            max_delivery_time=max((r.get("max_delivery_time") or 0) for r in rs) or None,
            value_for_money_rank=rs[0].get("value_for_money_rank"),
        ))
    return sorted(combined, key=lambda r: r.total_charge)


class EasyshipProvider(ShippingProvider):
    platform = "easyship"
    label = "Easyship"
    modes = ("sandbox", "production")

    # ---- rating / drafting ----
    def create_draft_shipments(self, destination, parcels, items, options=None):
        es_list, warnings = ec.create_shipments(
            destination, parcels, items, options, key=self.name, origin=self.origin())
        drafts = [DraftShipment(es["easyship_shipment_id"]) for es in es_list]
        return drafts, _combine_rates(es_list, provider=self.name), warnings

    def get_excluded_service_ids(self):
        return ec.get_excluded_service_ids(self.name)

    def set_excluded_service_ids(self, ids):
        return ec.set_excluded_service_ids(ids, self.name)

    # ---- label lifecycle ----
    def buy_labels(self, provider_shipment_ids, service_id):
        results = ec.buy_labels(provider_shipment_ids, service_id, key=self.name)
        return {sid: (res if isinstance(res, ProviderError) else _to_state(res, service_id))
                for sid, res in results.items()}

    def poll_shipments(self, provider_shipment_ids, service_id=None):
        results = ec.get_shipments(provider_shipment_ids, key=self.name)
        return {sid: (res if isinstance(res, ProviderError) else _to_state(res, service_id))
                for sid, res in results.items()}

    def fetch_labels(self, state):
        docs = ec.extract_label_documents(state.raw)
        if not docs:
            # Some couriers only expose the label as a rendered 4x6 PDF.
            try:
                docs = ec.extract_label_documents(
                    ec.get_shipment(state.provider_shipment_id, pdf_4x6=True, key=self.name)
                )
            except ProviderError:
                pass
        return docs

    def cancel_all(self, provider_shipment_ids):
        return ec.cancel_all(provider_shipment_ids, key=self.name)

    def get_raw_shipment(self, provider_shipment_id):
        return ec.get_shipment(provider_shipment_id, key=self.name)

    # ---- manifests ----
    def supports_manifests(self):
        return True

    def create_manifest(self, provider_shipment_ids):
        ids = [i for i in dict.fromkeys(provider_shipment_ids) if i]
        if not ids:
            raise ProviderError("No shipments to manifest")
        manifest = ec.create_manifest(self._usps_courier_id(ids[0]), ids, key=self.name)
        return [ManifestResult(
            provider_manifest_id=manifest.get("id"),
            ref_number=manifest.get("ref_number"),
            shipment_count=manifest.get("shipments_count") or len(ids),
            provider_shipment_ids=ids,
            document=self._manifest_document(manifest),
            raw=manifest,
        )]

    def _usps_courier_id(self, sample_shipment_id):
        """The courier account id the manifest is created for. The shipment's
        own courier id is authoritative when its payload carries one; otherwise
        a lone USPS courier on the account settles it."""
        try:
            cid = _shipment_courier_id(ec.get_shipment(sample_shipment_id, key=self.name))
        except ProviderError:
            cid = None
        if cid:
            return cid
        usps = [c for c in ec.list_couriers(self.name) if "usps" in c["umbrella_name"].lower()]
        if len(usps) == 1:
            return usps[0]["id"]
        if not usps:
            raise ProviderError("No USPS courier is connected to this Easyship account")
        names = ", ".join(c["name"] or c["id"] for c in usps)
        raise ProviderError(
            f"Several USPS couriers are connected to this Easyship account ({names}) "
            "— could not determine which one to manifest against"
        )

    def _manifest_document(self, manifest):
        url = (manifest.get("document") or {}).get("url")
        if not url:
            return None
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code in (401, 403):
                # Some document URLs are served by the API itself and need the token.
                _, token = ec._auth(self.name)
                resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
            if resp.ok and resp.content:
                return (resp.content, "pdf")
        except requests.RequestException:
            pass
        return None

    # ---- settings surface ----
    def list_item_categories(self):
        return ec.list_item_categories(self.name)

    def list_courier_services(self):
        return ec.list_courier_services(self.name)

    def active_mode(self):
        return self.setting("mode") or "sandbox"

    def test_connection(self, mode=None, token=None):
        mode = mode or self.active_mode()
        if not token or token == "••••••••":
            token = self.setting(f"{mode}_token")
        if not token:
            raise ProviderError(f"No {mode} token configured")
        # NB: Easyship's /account endpoint 500s unconditionally, so we validate
        # the token against /item_categories — a lightweight authenticated call.
        try:
            resp = requests.get(
                f"{config.EASYSHIP_BASE_URLS[mode]}/item_categories",
                headers={"Authorization": f"Bearer {token}"},
                params={"perPage": 1},
                timeout=15,
            )
        except requests.RequestException as e:
            raise ProviderError(f"Connection failed: {e}")
        if resp.status_code == 200:
            return {"ok": True, "mode": mode, "account": "connected"}
        if resp.status_code in (401, 403):
            raise ProviderError(f"Token rejected ({resp.status_code}) — check the {mode} token")
        raise ProviderError(f"Easyship returned {resp.status_code}: {resp.text[:300]}")

    def descriptor(self):
        return {
            "name": self.name,
            "key": self.name,
            "platform": self.platform,
            "platform_label": self.platform_label,
            "label": self.label,
            "enabled": self.setting("enabled") == "true",
            "enabled_key": self.setting_key("enabled"),
            "mode_key": self.setting_key("mode"),
            "mode": self.active_mode(),
            "modes": [
                {"value": "sandbox", "label": "Sandbox (test)"},
                {"value": "production", "label": "Production (live — labels cost money)"},
            ],
            "fields": [
                {"key": f"{self.name}_sandbox_token", "label": "Sandbox access token",
                 "type": "secret", "mode": "sandbox"},
                {"key": f"{self.name}_production_token", "label": "Production access token",
                 "type": "secret", "mode": "production"},
                {"key": self.setting_key("default_item_category"), "label": "Default item category (customs)",
                 "type": "select", "options_endpoint": f"/api/providers/{self.name}/item-categories",
                 "hint": "Applied to shipment items — Easyship requires one per item"},
            ],
            "test_endpoint": f"/api/providers/{self.name}/test",
            "supports": {"service_exclusions": True},
            "services_endpoint": f"/api/providers/{self.name}/services",
            "excluded_endpoint": f"/api/providers/{self.name}/excluded-services",
            **origin_descriptor(self.name),
        }

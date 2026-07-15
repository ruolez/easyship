"""Easyship implementation of the ShippingProvider interface.

A thin adapter over the existing `easyship_client` module: it keeps all the
tested HTTP/throttle/retry/unit-conversion code and just normalizes Easyship's
shapes into the provider-agnostic types.
"""
import requests

import config
import db
import easyship_client as ec
from providers import labels
from providers.base import (
    DraftShipment,
    LabelStatus,
    ProviderError,
    Rate,
    ShipmentState,
    ShippingProvider,
    provider_setting,
)

# Easyship label_state values that mean the label is bought and printable.
_READY_STATES = {"generated", "printed", "shipping_document_generated"}


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


def _combine_rates(es_list):
    """One quote list across per-box shipments: only couriers that can serve
    EVERY box, price = sum across boxes."""
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
            provider="easyship",
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
    name = "easyship"
    label = "Easyship"
    modes = ("sandbox", "production")

    # ---- rating / drafting ----
    def create_draft_shipments(self, destination, parcels, items):
        es_list = ec.create_shipments(destination, parcels, items)
        drafts = [DraftShipment(es["easyship_shipment_id"]) for es in es_list]
        return drafts, _combine_rates(es_list)

    def get_excluded_service_ids(self):
        return ec.get_excluded_service_ids()

    def set_excluded_service_ids(self, ids):
        return ec.set_excluded_service_ids(ids)

    # ---- label lifecycle ----
    def buy_labels(self, provider_shipment_ids, service_id):
        results = ec.buy_labels(provider_shipment_ids, service_id)
        return {sid: (res if isinstance(res, ProviderError) else _to_state(res, service_id))
                for sid, res in results.items()}

    def poll_shipments(self, provider_shipment_ids, service_id=None):
        results = ec.get_shipments(provider_shipment_ids)
        return {sid: (res if isinstance(res, ProviderError) else _to_state(res, service_id))
                for sid, res in results.items()}

    def fetch_labels(self, state):
        docs = ec.extract_label_documents(state.raw)
        if not docs:
            # Some couriers only expose the label as a rendered 4x6 PDF.
            try:
                docs = ec.extract_label_documents(
                    ec.get_shipment(state.provider_shipment_id, pdf_4x6=True)
                )
            except ProviderError:
                pass
        return docs

    def cancel_all(self, provider_shipment_ids):
        return ec.cancel_all(provider_shipment_ids)

    def get_raw_shipment(self, provider_shipment_id):
        return ec.get_shipment(provider_shipment_id)

    # ---- settings surface ----
    def list_item_categories(self):
        return ec.list_item_categories()

    def list_courier_services(self):
        return ec.list_courier_services()

    def active_mode(self):
        return provider_setting(self.name, "mode") or "sandbox"

    def test_connection(self, mode=None, token=None):
        mode = mode or self.active_mode()
        if not token or token == "••••••••":
            token = provider_setting(self.name, f"{mode}_token")
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
            "label": self.label,
            "enabled": provider_setting(self.name, "enabled") == "true",
            "enabled_key": f"{self.name}_enabled",
            "mode_key": f"{self.name}_mode",
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
                {"key": "default_item_category", "label": "Default item category (customs)",
                 "type": "select", "options_endpoint": f"/api/providers/{self.name}/item-categories",
                 "hint": "Applied to shipment items — Easyship requires one per item"},
            ],
            "test_endpoint": f"/api/providers/{self.name}/test",
            "supports": {"service_exclusions": True},
            "services_endpoint": f"/api/providers/{self.name}/services",
            "excluded_endpoint": f"/api/providers/{self.name}/excluded-services",
        }

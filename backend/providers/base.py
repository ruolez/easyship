"""Provider-agnostic shipping interface.

`shipments_api` and `settings_api` speak only to `ShippingProvider` and the
normalized types below — never to a provider's raw API shapes. A new platform
(e.g. GoShippo) is added by implementing this interface and registering it in
`providers/__init__.py`; no changes to the routes or the UI contract are needed.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

import db


class ProviderError(Exception):
    """A shipping-provider request failed. Providers raise this (or a subclass)
    so callers can handle every platform uniformly."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status

    @property
    def recoverable(self):
        """Timeouts and gateway 5xx — the request may still have succeeded on
        the provider's side, so a caller may safely re-check rather than retry."""
        return self.status is None or self.status >= 500


class LabelStatus(Enum):
    """Normalized label lifecycle, hiding each provider's own state strings."""

    READY = "ready"              # label bought, documents available
    PENDING = "pending"          # accepted, still generating
    FAILED = "failed"            # provider rejected generation
    NOT_CREATED = "not_created"  # no label attempt has landed — re-buy candidate


@dataclass
class Rate:
    """A quote the UI can render. `provider` tags which platform produced it so
    a buy can be dispatched back to the same one."""

    provider: str
    provider_service_id: str
    courier_name: str
    umbrella_name: str
    total_charge: float
    currency: str
    min_delivery_time: int | None
    max_delivery_time: int | None
    value_for_money_rank: int | None

    def to_ui(self):
        """The exact rate shape the frontend consumes, plus `provider`."""
        return {
            "provider": self.provider,
            "courier_service_id": self.provider_service_id,
            "courier_name": self.courier_name,
            "umbrella_name": self.umbrella_name,
            "total_charge": self.total_charge,
            "currency": self.currency,
            "min_delivery_time": self.min_delivery_time,
            "max_delivery_time": self.max_delivery_time,
            "value_for_money_rank": self.value_for_money_rank,
        }


@dataclass
class DraftShipment:
    """A per-box shipment created to obtain rates, before any label is bought."""

    provider_shipment_id: str


@dataclass
class ShipmentState:
    """Live state of one box's shipment during and after label purchase."""

    provider_shipment_id: str
    label_status: LabelStatus
    tracking_numbers: list[str] = field(default_factory=list)
    courier_name: str | None = None
    courier_umbrella_name: str | None = None
    cost: float | None = None  # per-box charge for the chosen service, if known
    raw: dict = field(default_factory=dict)  # provider payload, for label fetch/diagnostics


# A label document is a plain (bytes, format) tuple; format in {"pdf","png","zpl"}.


def provider_setting(name, key, default=None):
    """Read a provider-scoped setting. Providers keep their own namespaced keys
    (e.g. `easyship_mode`, `easyship_sandbox_token`)."""
    return db.get_setting(key, default) if key.startswith(f"{name}_") else db.get_setting(f"{name}_{key}", default)


class ShippingProvider(ABC):
    """Everything the shipping routes need from a platform. All methods may raise
    ProviderError; parallel helpers return per-id ProviderError instead."""

    name: str
    label: str
    modes: tuple = ()  # e.g. ("sandbox", "production"); empty if the provider has no environments

    # ---- rating / drafting (POST /rates) ----
    @abstractmethod
    def create_draft_shipments(self, destination, parcels, items):
        """Returns (list[DraftShipment] in box order, list[Rate] valid for every
        box). Hides per-box shipment creation and cross-box rate intersection."""

    @abstractmethod
    def get_excluded_service_ids(self):
        """Set of service-id strings hidden from the rate list."""

    @abstractmethod
    def set_excluded_service_ids(self, ids):
        ...

    # ---- label lifecycle (group buy) ----
    @abstractmethod
    def buy_labels(self, provider_shipment_ids, service_id):
        """Purchase labels for all boxes. Returns {id: ShipmentState | ProviderError}."""

    @abstractmethod
    def poll_shipments(self, provider_shipment_ids, service_id=None):
        """Re-fetch shipment state. Returns {id: ShipmentState | ProviderError}."""

    @abstractmethod
    def fetch_labels(self, state):
        """All label documents for one box as [(bytes, format)]; handles any
        provider-specific re-fetch (e.g. a 4x6 fallback) internally."""

    @abstractmethod
    def cancel_all(self, provider_shipment_ids):
        """Cancel shipments/labels; returns a list of error strings."""

    def get_raw_shipment(self, provider_shipment_id):
        """Optional diagnostic: the raw provider payload for a shipment."""
        raise ProviderError("Raw shipment view not supported by this provider")

    # ---- settings surface ----
    @abstractmethod
    def list_item_categories(self):
        ...

    @abstractmethod
    def list_courier_services(self):
        ...

    @abstractmethod
    def active_mode(self):
        """Current environment name (e.g. 'sandbox'/'production'), or '' if none."""

    def is_test_mode(self):
        """True when running against a non-live environment (drives the nav badge)."""
        return self.active_mode() == "sandbox"

    @abstractmethod
    def test_connection(self, mode=None, token=None):
        """Validate credentials; returns a dict (e.g. {'ok': True})."""

    @abstractmethod
    def descriptor(self):
        """UI-rendering metadata: fields, modes, capabilities. Never includes secrets."""

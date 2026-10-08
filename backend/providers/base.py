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
    error_message: str | None = None  # provider's reason when label_status is FAILED
    raw: dict = field(default_factory=dict)  # provider payload, for label fetch/diagnostics


# A label document is a plain (bytes, format) tuple; format in {"pdf","png","zpl"}.


@dataclass
class ManifestResult:
    """One end-of-day manifest (USPS SCAN form) issued by the carrier."""

    provider_manifest_id: str | None
    ref_number: str | None  # the number under the barcode, when the platform returns one
    shipment_count: int
    provider_shipment_ids: list[str]  # the ids this manifest actually covered
    document: tuple | None  # (bytes, "pdf") downloaded at creation — the URLs expire
    raw: dict = field(default_factory=dict)


# Ship-from address fields, in the order the Settings page shows them.
ORIGIN_FIELDS = (
    ("company", "Company"),
    ("contact", "Contact name"),
    ("address1", "Address 1"),
    ("address2", "Address 2"),
    ("city", "City"),
    ("state", "State"),
    ("zip", "ZIP"),
    ("phone", "Phone"),
    ("email", "Email"),
)


def origin_override_key(key):
    return f"{key}_origin_override"


def origin_settings(key):
    """The ship-from address one instance ships with, keyed like the global
    settings (`origin_company`, ...): the instance's own `{key}_origin_*` values
    when its override flag is on, otherwise the global origin."""
    own = db.get_setting(origin_override_key(key)) == "true"
    prefix = f"{key}_origin_" if own else "origin_"
    return {f"origin_{f}": (db.get_setting(f"{prefix}{f}") or "").strip() for f, _ in ORIGIN_FIELDS}


def origin_descriptor(key):
    """Descriptor fragment that lets the Settings page render and persist an
    instance's ship-from override."""
    return {
        "origin_override_key": origin_override_key(key),
        "origin_fields": [{"key": f"{key}_origin_{f}", "label": label} for f, label in ORIGIN_FIELDS],
    }


def missing_origin_fields(origin, required):
    """Labels of the required origin fields that are blank in `origin`."""
    return [label for k, label in required.items() if not (origin.get(k) or "").strip()]


def parcel_dimensions(parcel):
    """{length, width, height} in inches as floats, or {} unless every side is
    a positive number — a 0×0×0 box means "price by weight only". Never invent
    a side: USPS quotes a 1-inch cube at the cheapest cubic tier but bills the
    real package by weight."""
    dims = {}
    for side in ("length", "width", "height"):
        try:
            value = float(str(parcel.get(side) or "").strip())
        except ValueError:
            return {}
        if value <= 0:
            return {}
        dims[side] = value
    return dims


class ShippingProvider(ABC):
    """Everything the shipping routes need from a platform. All methods may raise
    ProviderError; parallel helpers return per-id ProviderError instead.

    One object represents one configured INSTANCE of a platform: `platform` is
    the class-level platform key ("shipstation"), `name` the instance key that
    identifies it everywhere (settings prefix, shipments.provider, the nav
    selector) and `label` the admin's alias. The primary instance of each
    platform has `name == platform`, so the no-arg constructor is that one."""

    platform: str
    label: str
    modes: tuple = ()  # e.g. ("sandbox", "production"); empty if the provider has no environments

    def __init__(self, key=None, label=None):
        self.name = key or self.platform
        self.label = label or type(self).label

    @property
    def platform_label(self):
        return type(self).label

    def setting_key(self, suffix):
        return f"{self.name}_{suffix}"

    def setting(self, suffix, default=None):
        return db.get_setting(self.setting_key(suffix), default)

    def origin(self):
        return origin_settings(self.name)

    # ---- rating / drafting (POST /rates) ----
    @abstractmethod
    def create_draft_shipments(self, destination, parcels, items, options=None):
        """Returns (list[DraftShipment] in box order, list[Rate] valid for every
        box, list[str] warnings). Hides per-box shipment creation and cross-box
        rate intersection. `options` carries shipment-level choices such as
        {"signature": "none" | "signature" | "adult"}; a provider that cannot
        honor one must say so in a warning rather than silently drop it."""

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

    # ---- end-of-day manifests (USPS SCAN forms) ----
    def supports_manifests(self):
        """Whether create_manifest works on this platform. Default False so a
        platform without an implementation keeps working untouched."""
        return False

    def create_manifest(self, provider_shipment_ids):
        """Carrier end-of-day manifest(s) for already-purchased labels, given
        the stored per-box provider ids (deduped). Blocks — with bounded
        internal polling — until the documents exist, and downloads them
        (their URLs are signed and expire). Returns list[ManifestResult]:
        usually one, more when the platform splits by carrier account."""
        raise ProviderError(f"{self.label} does not support manifests")

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

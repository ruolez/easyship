"""Shipping-provider registry.

The active platform(s) are chosen by per-provider `{name}_enabled` settings.
Add a provider by importing its class in `_load_registry` and registering it.

Imports are deferred so `easyship_client` (which imports `providers.labels`) can
load without a circular import through this package.
"""
import db

_REGISTRY = {}
_INSTANCES = {}


def _load_registry():
    if not _REGISTRY:
        from .easyship import EasyshipProvider
        from .shippo import ShippoProvider
        from .easypost import EasyPostProvider
        from .shipstation import ShipStationProvider
        _REGISTRY["easyship"] = EasyshipProvider
        _REGISTRY["shippo"] = ShippoProvider
        _REGISTRY["easypost"] = EasyPostProvider
        _REGISTRY["shipstation"] = ShipStationProvider
    return _REGISTRY


def registered_names():
    return list(_load_registry().keys())


def get_provider(name):
    """The provider instance for `name`, defaulting to Easyship. Instances are
    stateless (they read settings per call), so caching one per name is safe
    across worker threads."""
    registry = _load_registry()
    name = name if name in registry else "easyship"
    if name not in _INSTANCES:
        _INSTANCES[name] = registry[name]()
    return _INSTANCES[name]


def all_providers():
    return [get_provider(name) for name in registered_names()]


def _is_enabled(name):
    flag = db.get_setting(f"{name}_enabled")
    # Easyship was always on before per-provider enable flags existed, so an
    # unset flag means enabled for it (and only it).
    if flag is None:
        return name == "easyship"
    return flag == "true"


def enabled_providers():
    """Providers the user has turned on, in registration order. Falls back to
    Easyship if somehow none are enabled, so rating never silently no-ops."""
    active = [get_provider(name) for name in registered_names() if _is_enabled(name)]
    return active or [get_provider("easyship")]


def enabled_for_user(user_id, role):
    """Enabled providers this user may ship with. Admins and users without an
    assignment get every enabled provider. The empty-intersection case stays
    empty — never fall back to Easyship for a restricted user. Reads the
    assignment fresh from the DB so admin changes apply without re-login."""
    active = enabled_providers()
    if role == "admin" or not user_id:
        return active
    row = db.query("SELECT allowed_providers FROM users WHERE id = %s", (user_id,), one=True)
    allowed = (row or {}).get("allowed_providers")
    if not allowed:
        return active
    allowed = {str(n) for n in allowed}
    return [p for p in active if p.name in allowed]


def sanitize_allowed(names):
    """An allowed_providers value ready to store: only registered names, in
    registration order; None when empty or when nothing is actually excluded
    (no selection = no restriction)."""
    wanted = {str(n) for n in (names or [])}
    clean = [n for n in registered_names() if n in wanted]
    if not clean or len(clean) == len(registered_names()):
        return None
    return clean


def descriptors():
    return [p.descriptor() for p in all_providers()]

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
        _REGISTRY["easyship"] = EasyshipProvider
        _REGISTRY["shippo"] = ShippoProvider
        _REGISTRY["easypost"] = EasyPostProvider
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


def descriptors():
    return [p.descriptor() for p in all_providers()]

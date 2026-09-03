"""Shipping-provider registry.

Two layers: PLATFORMS are the implementations (`_load_registry`); INSTANCES are
the configured accounts in the `provider_instances` table — several per
platform, each with a stable key and an editable alias. An instance's key is
its identity everywhere (settings prefix, shipments.provider,
users.allowed_providers, the nav selector); the primary instance of each
platform has key == platform name, so the pre-instance data needed no rewrite.

Objects are built from rows on every call rather than cached: gunicorn runs
several workers, so an in-process cache could not see another worker's
add/rename/delete.

Imports are deferred so `easyship_client` (which imports `providers.labels`) can
load without a circular import through this package.
"""

import json

import db

_REGISTRY = {}

MAX_LABEL_LENGTH = 60

NO_ACCOUNT_LEFT = "would be left with no shipping account — assign another account first, or deactivate them"


class InstanceEnabled(Exception):
    """Raised when deleting an instance that is still switched on."""


class InstanceInUse(Exception):
    """Raised when deleting an instance that is some user's only account."""


def _load_registry():
    if not _REGISTRY:
        from .easyship import EasyshipProvider
        from .shippo import ShippoProvider
        from .easypost import EasyPostProvider
        from .shipstation import ShipStationProvider
        from .endicia import EndiciaProvider
        _REGISTRY["easyship"] = EasyshipProvider
        _REGISTRY["shippo"] = ShippoProvider
        _REGISTRY["easypost"] = EasyPostProvider
        _REGISTRY["shipstation"] = ShipStationProvider
        _REGISTRY["endicia"] = EndiciaProvider
    return _REGISTRY


def platform_class(platform):
    return _load_registry().get(platform)


def platforms():
    """Registered platforms, in registration order."""
    return [{"name": name, "label": cls.label} for name, cls in _load_registry().items()]


def _rows():
    return db.query("SELECT id, platform, key, label FROM provider_instances ORDER BY id") or []


def _build(row):
    cls = platform_class(row["platform"])
    return cls(row["key"], row["label"]) if cls else None


def instances():
    """One provider object per configured instance, in creation order."""
    return [p for p in (_build(r) for r in _rows()) if p]


def instance_keys():
    return [p.name for p in instances()]


def get_provider(key):
    """The provider object for an instance key, or None when no such instance
    exists (a deleted instance, or a bad request) — callers must not assume."""
    if not key:
        return None
    row = db.query("SELECT id, platform, key, label FROM provider_instances WHERE key = %s",
                   (key,), one=True)
    return _build(row) if row else None


def _is_enabled(key):
    return db.get_setting(f"{key}_enabled") == "true"


def enabled_providers():
    """Instances the admin has switched on, in creation order."""
    return [p for p in instances() if _is_enabled(p.name)]


def enabled_for_user(user_id, role):
    """Enabled instances this user may ship with. Admins and users without an
    assignment get every enabled instance. The empty-intersection case stays
    empty — never fall back for a restricted user. Reads the assignment fresh
    from the DB so admin changes apply without re-login."""
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
    """An allowed_providers value ready to store: only existing instance keys,
    in creation order; None when empty or when nothing is actually excluded
    (no selection = no restriction)."""
    wanted = {str(n) for n in (names or [])}
    keys = instance_keys()
    clean = [k for k in keys if k in wanted]
    if not clean or len(clean) == len(keys):
        return None
    return clean


def descriptors():
    return [p.descriptor() for p in instances()]


def instance_summaries():
    """Instance rows plus their enabled flag, for the admin instance list."""
    return [
        {"id": r["id"], "platform": r["platform"], "key": r["key"], "label": r["label"],
         "enabled": _is_enabled(r["key"])}
        for r in _rows()
    ]


def clean_label(label, exclude_id=None):
    """A validated alias: trimmed, non-empty, short, unique (case-insensitive)."""
    label = (label or "").strip()
    if not label:
        raise ValueError("A name is required")
    if len(label) > MAX_LABEL_LENGTH:
        raise ValueError(f"Name must be {MAX_LABEL_LENGTH} characters or fewer")
    for r in _rows():
        if r["label"].lower() == label.lower() and r["id"] != exclude_id:
            raise ValueError(f"An integration named '{r['label']}' already exists")
    return label


def create_instance(platform, label):
    """A new, disabled instance of `platform`; its key is `{platform}-{id}`,
    which can never collide with a platform name (those contain no '-')."""
    if not platform_class(platform):
        raise ValueError("Unknown shipping platform")
    label = clean_label(label)
    row = db.execute(
        """INSERT INTO provider_instances (id, platform, key, label)
           SELECT nid, %s, %s || '-' || nid, %s
           FROM nextval(pg_get_serial_sequence('provider_instances', 'id')) AS nid
           RETURNING id, platform, key, label""",
        (platform, platform, label),
        returning=True,
    )
    return dict(row)


def rename_instance(instance_id, label):
    row = db.query("SELECT id, platform, key, label FROM provider_instances WHERE id = %s",
                   (instance_id,), one=True)
    if not row:
        raise LookupError("Integration not found")
    label = clean_label(label, exclude_id=instance_id)
    db.execute("UPDATE provider_instances SET label = %s WHERE id = %s", (label, instance_id))
    return {**row, "label": label}


def _restrictable_users():
    """Users whose allow-list matters: active, non-admin, in id order."""
    return db.query(
        "SELECT id, username, allowed_providers FROM users "
        "WHERE is_active AND role <> 'admin' ORDER BY id"
    ) or []


def set_instance_users(instance_id, user_ids):
    """Make exactly `user_ids` (of the active non-admin users) allowed to ship
    with this instance, leaving their other accounts untouched. NULL = every
    account stays NULL when included; an excluded NULL user gets the explicit
    list of every other account. Refuses — before writing anything — when a
    user would end up with no account at all (there is no 'none' state:
    deactivate the user instead)."""
    row = db.query("SELECT id, platform, key, label FROM provider_instances WHERE id = %s",
                   (instance_id,), one=True)
    if not row:
        raise LookupError("Integration not found")
    key = row["key"]
    keys = instance_keys()
    wanted = {int(u) for u in (user_ids or [])}
    changes, conflicts, assigned = [], [], []
    for u in _restrictable_users():
        current = u["allowed_providers"]
        if u["id"] in wanted:
            assigned.append(u["id"])
            new = None if current is None else sanitize_allowed(list(current) + [key])
        else:
            new = [k for k in (current or keys) if k != key]
            if not new:
                conflicts.append(u["username"])
                continue
            new = sanitize_allowed(new)
        if new != current:
            changes.append((u["id"], new))
    if conflicts:
        raise ValueError(f"{', '.join(conflicts)} {NO_ACCOUNT_LEFT}")
    for user_id, new in changes:
        db.execute(
            "UPDATE users SET allowed_providers = %s WHERE id = %s",
            (json.dumps(new) if new else None, user_id),
        )
    return {**row, "user_ids": assigned}


def delete_instance(instance_id):
    """Remove an instance and every `{key}_*` setting it owned, and drop its key
    from users' allow-lists. Refuses while the instance is enabled, and while
    it is some user's only account (deleting would silently make them
    unrestricted). Shipments keep the key and the alias snapshot; they can no
    longer be voided or re-bought."""
    row = db.query("SELECT id, platform, key, label FROM provider_instances WHERE id = %s",
                   (instance_id,), one=True)
    if not row:
        raise LookupError("Integration not found")
    key = row["key"]
    if _is_enabled(key):
        raise InstanceEnabled("Disable this integration and save before deleting it")
    only = [u["username"] for u in _restrictable_users() if list(u["allowed_providers"] or []) == [key]]
    if only:
        raise InstanceInUse(f"{', '.join(only)} {NO_ACCOUNT_LEFT}")
    db.execute("DELETE FROM provider_instances WHERE id = %s", (instance_id,))
    # starts_with, not LIKE: '_' is a LIKE wildcard and 'shipstation_' would
    # otherwise also match 'shipstation-5_'.
    db.execute("DELETE FROM settings WHERE starts_with(key, %s)", (f"{key}_",))
    db.execute(
        """UPDATE users SET allowed_providers = NULLIF(allowed_providers - %s, '[]'::jsonb)
           WHERE allowed_providers IS NOT NULL AND allowed_providers ? %s""",
        (key, key),
    )
    return dict(row)

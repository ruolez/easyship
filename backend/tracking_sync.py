"""Background pull of carrier tracking events.

Every gunicorn worker starts this thread; each tick one of them wins a
Postgres advisory lock and pulls, for every enabled platform that reports
tracking, once its own interval has passed. UDS hands each pending event out
only once, so events are stored before anything else happens with them.
"""
import logging
import threading
import time

import db
import providers
import tracking_store

TICK_SECONDS = 60
LOCK = "tracking-pull"
log = logging.getLogger("tracking_sync")


def due(provider, now):
    """Whether this provider's pull interval has passed (0 = pulls off)."""
    minutes = provider.poll_minutes() if hasattr(provider, "poll_minutes") else 0
    if not minutes:
        return False
    try:
        last = float(provider.setting("tracking_last_pull") or 0)
    except ValueError:
        last = 0
    return now - last >= minutes * 60


def pull_once(now=None):
    """One pass over the enabled tracking platforms. Returns
    {provider: events stored | error text}."""
    now = now if now is not None else time.time()
    results = {}
    for provider in providers.enabled_providers():
        if not provider.supports_tracking() or not due(provider, now):
            continue
        try:
            events = provider.pull_tracking()
            tracking_store.save(provider.name, events)
        except Exception as e:  # one platform's outage must not stop the others
            log.warning("tracking pull %s failed: %s", provider.name, e)
            db.set_setting(provider.setting_key("tracking_last_error"), str(e)[:300])
            results[provider.name] = str(e)
            continue
        db.set_setting(provider.setting_key("tracking_last_pull"), str(now))
        db.set_setting(provider.setting_key("tracking_last_error"), "")
        results[provider.name] = len(events)
    return results


def _tick(app):
    with app.app_context():
        got = db.query("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (LOCK,), one=True)
        if not got or not got["ok"]:
            return
        try:
            pull_once()
        finally:
            db.query("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK,))


def _run(app):
    while True:
        time.sleep(TICK_SECONDS)
        try:
            _tick(app)
        except Exception:
            log.exception("tracking pull tick failed")


def start(app):
    threading.Thread(target=_run, args=(app,), daemon=True, name="tracking-sync").start()

import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

import tracking_store  # noqa: E402
import tracking_sync  # noqa: E402

CST = timezone(timedelta(hours=-6))


def event(event_id, status, at, number="UDS1"):
    return {"tracking_number": number, "event_id": event_id, "status_code": status[:1].upper(), "status": status,
            "description": status, "event_at": at, "photos": []}


class SummarizeTest(unittest.TestCase):
    def test_latest_event_wins_and_delivery_time_is_the_first_delivered_event(self):
        events = [event(1, "in_transit", "2026-11-03T13:00:00-06:00"),
                  event(3, "delivered", "2026-11-04T11:00:00-06:00"),
                  event(2, "out_for_delivery", "2026-11-04T08:00:00-06:00"),
                  event(4, "delivered", "2026-11-04T12:00:00-06:00")]
        self.assertEqual(tracking_store.summarize(events), (
            "delivered", datetime(2026, 11, 4, 12, tzinfo=CST), datetime(2026, 11, 4, 11, tzinfo=CST)))

    def test_order_of_arrival_does_not_change_the_summary(self):
        events = [event(1, "info_received", "2026-11-03T08:00:00-06:00"),
                  event(2, "in_transit", "2026-11-03T13:00:00-06:00"),
                  event(3, "exception", "2026-11-04T09:00:00-06:00"),
                  event(4, "will_call", None)]
        summaries = {tracking_store.summarize(events[i:] + events[:i]) for i in range(len(events))}
        self.assertEqual(summaries, {("exception", datetime(2026, 11, 4, 9, tzinfo=CST), None)})

    def test_same_time_events_fall_back_to_the_event_id(self):
        at = "2026-11-03T08:00:00-06:00"
        self.assertEqual(tracking_store.summarize([event(9, "in_transit", at), event(10, "exception", at)])[0],
                         "exception")

    def test_no_events_is_no_status(self):
        self.assertEqual(tracking_store.summarize([]), (None, None, None))


class FakeTrackingDb:
    def __init__(self):
        self.events = {}
        self.updates = []
        self.settings = {}

    def execute(self, sql, params=None, returning=False):
        if sql.startswith("INSERT INTO carrier_tracking_events"):
            key = params[:3]
            if key not in self.events:
                self.events[key] = {"tracking_number": params[1], "event_id": params[2], "status": params[4],
                                    "event_at": params[8]}
            return None
        if sql.lstrip().startswith("UPDATE shipments SET tracking_status"):
            self.updates.append(params)
            return {"id": 1} if returning else None
        return None

    def query(self, sql, params=None, one=False):
        if "FROM carrier_tracking_events" in sql:
            provider, numbers = params
            return [dict(e) for (p, n, _), e in self.events.items() if p == provider and n in numbers]
        return None if one else []

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeTrackingDb()
        self.addCleanup(setattr, tracking_store, "db", tracking_store.db)
        tracking_store.db = self.db

    def test_saving_twice_stores_each_event_once_and_sets_the_parcel_status(self):
        events = [event(1, "in_transit", "2026-11-03T13:00:00-06:00"),
                  event(2, "delivered", "2026-11-04T11:00:00-06:00")]
        tracking_store.save("uds", events)
        tracking_store.save("uds", events)
        delivered = datetime(2026, 11, 4, 11, tzinfo=CST)
        self.assertEqual((len(self.db.events), self.db.updates[-1]),
                         (2, ("delivered", delivered, delivered, "uds", "UDS1", "UDS1")))

    def test_events_without_a_number_or_id_are_skipped(self):
        updated = tracking_store.save("uds", [{**event(1, "in_transit", None), "tracking_number": ""},
                                              {**event(None, "in_transit", None)}])
        self.assertEqual((updated, self.db.events), ([], {}))


class FakeProvider:
    def __init__(self, name, minutes=15, tracks=True, events=None, error=None):
        self.name = name
        self.tracks = tracks
        self.minutes = minutes
        self.events = events or []
        self.error = error
        self.settings = {}

    def supports_tracking(self):
        return self.tracks

    def poll_minutes(self):
        return self.minutes

    def setting_key(self, suffix):
        return f"{self.name}_{suffix}"

    def setting(self, suffix, default=None):
        return tracking_sync.db.get_setting(self.setting_key(suffix), default)

    def pull_tracking(self):
        if self.error:
            raise self.error
        return self.events


class PullTest(unittest.TestCase):
    NOW = 1_800_000_000.0

    def setUp(self):
        self.db = FakeTrackingDb()
        self.saved = []
        originals = (tracking_sync.db, tracking_sync.providers.enabled_providers, tracking_sync.tracking_store.save)
        self.addCleanup(self.restore, originals)
        tracking_sync.db = self.db
        tracking_sync.tracking_store.save = lambda provider, events: self.saved.append((provider, events))

    def restore(self, originals):
        (tracking_sync.db, tracking_sync.providers.enabled_providers, tracking_sync.tracking_store.save) = originals

    def run_with(self, provider_list):
        tracking_sync.providers.enabled_providers = lambda: provider_list
        return tracking_sync.pull_once(now=self.NOW)

    def test_due_providers_are_pulled_and_stored_and_the_rest_skipped(self):
        self.db.settings["recent_tracking_last_pull"] = str(self.NOW - 60)
        due = FakeProvider("uds", events=[event(1, "in_transit", None)])
        result = self.run_with([due, FakeProvider("recent"), FakeProvider("off", minutes=0),
                                FakeProvider("shippo", tracks=False)])
        self.assertEqual((result, self.saved, self.db.settings["uds_tracking_last_pull"]),
                         ({"uds": 1}, [("uds", due.events)], str(self.NOW)))

    def test_a_failed_pull_is_recorded_and_retried_next_tick(self):
        result = self.run_with([FakeProvider("uds", error=RuntimeError("UDS error (500): down"))])
        self.assertEqual((result, self.db.settings.get("uds_tracking_last_error"),
                          self.db.settings.get("uds_tracking_last_pull")),
                         ({"uds": "UDS error (500): down"}, "UDS error (500): down", None))


if __name__ == "__main__":
    unittest.main()

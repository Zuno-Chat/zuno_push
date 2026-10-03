from prometheus_client import REGISTRY
from twisted.internet import defer, task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals, add_event
from unit_tests.fakes import FakeEvent, FakeModuleApi, run, state_event
from zuno_push.nse_scope import NseScope
from zuno_push.records import EventTimes, PushAction, ReadPoint

NOW = 1_790_000_000_000
ROOM = "!room:test"
HOUR = 3_600_000


class NseScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(NOW / 1000)
        self.api = FakeModuleApi(self.clock)
        self.internals = FakeInternals(self.api)
        self.scope = NseScope(self.api, self.internals)
        self.api.room_state[ROOM] = {
            ("m.room.name", ""): state_event("m.room.name", "", {"name": "Design team"}, ROOM),
            ("m.room.member", "@alice:test"): state_event(
                "m.room.member", "@alice:test", {"displayname": "Alice"}, ROOM
            ),
        }
        self.event = FakeEvent(
            type="m.room.encrypted",
            sender="@alice:test",
            content={"ciphertext": "x"},
            room_id=ROOM,
            event_id="$e1",
            origin_server_ts=NOW - 1000,
        )
        add_event(self.internals, self.event, NOW - 1000)
        self.internals.actions[("@bob:test", ROOM, "$e1")] = PushAction(
            50, "main", True, False, True
        )

    def fetch(self, room_id: str = ROOM, event_id: str = "$e1") -> dict:
        return run(self.scope.fetch("@bob:test", room_id, event_id, NOW))

    def test_ok_reply(self) -> None:
        self.assertEqual(
            self.fetch(),
            {
                "status": "ok",
                "event": {
                    "event_id": "$e1",
                    "room_id": ROOM,
                    "type": "m.room.encrypted",
                    "sender": "@alice:test",
                    "origin_server_ts": NOW - 1000,
                    "state_key": None,
                    "redacted": False,
                    "content": {"ciphertext": "x"},
                    "invite_room_state": None,
                },
                "sender_name": "Alice",
                "room_name": "Design team",
                "is_dm": False,
                "highlight": False,
                "sound": True,
            },
        )

    def test_dm_alias_and_unknown_names(self) -> None:
        self.api.account_data_manager.data[("@bob:test", "m.direct")] = {"@alice:test": [ROOM]}
        del self.api.room_state[ROOM][("m.room.member", "@alice:test")]
        self.api.room_state[ROOM][("m.room.name", "")] = state_event("m.room.name", "", {}, ROOM)
        self.api.room_state[ROOM][("m.room.canonical_alias", "")] = state_event(
            "m.room.canonical_alias", "", {"alias": "#design:test"}, ROOM
        )
        body = self.fetch()
        self.assertTrue(body["is_dm"])
        self.assertNotIn("sender_name", body)
        self.assertEqual(body["room_name"], "#design:test")

    def test_read_reply_carries_the_unthreaded_receipt_time(self) -> None:
        self.internals.points[ROOM] = ReadPoint(unthreaded_so=60, unthreaded_ts=NOW - 10)
        self.assertEqual(self.fetch(), {"status": "read", "receipt_ts": NOW - 10})

    def test_read_through_the_events_own_thread_keeps_the_unthreaded_time(self) -> None:
        self.internals.actions[("@bob:test", ROOM, "$e1")] = PushAction(
            50, "$root", True, False, False
        )
        self.internals.points[ROOM] = ReadPoint(
            unthreaded_so=10, unthreaded_ts=NOW - 99, threads={"$root": 55}
        )
        self.assertEqual(self.fetch(), {"status": "read", "receipt_ts": NOW - 99})

    def test_uniform_gone_with_reasons_counted(self) -> None:
        def count(reason: str) -> float:
            labels = {"status": "gone", "reason": reason}
            return REGISTRY.get_sample_value("zuno_push_fetch_total", labels) or 0

        before = count("no_row")
        self.assertEqual(self.fetch(event_id="$unknown"), {"status": "gone"})
        self.assertEqual(self.fetch(room_id="!other:test"), {"status": "gone"})
        self.assertEqual(count("no_row"), before + 2)
        self.internals.actions[("@bob:test", ROOM, "$e1")] = PushAction(
            50, "main", False, False, False
        )
        self.assertEqual(self.fetch(), {"status": "gone"})

    def test_age_limits_use_the_servers_receive_time(self) -> None:
        add_event(self.internals, self.event, NOW - 24 * HOUR - 1)
        self.assertEqual(self.fetch()["status"], "gone")
        self.internals.actions[("@bob:test", ROOM, "$e1")] = PushAction(
            50, "main", True, True, True
        )
        self.assertEqual(self.fetch()["status"], "ok")
        add_event(self.internals, self.event, NOW - 72 * HOUR - 1)
        self.assertEqual(self.fetch()["status"], "gone")

    def test_purged_event_is_gone(self) -> None:
        del self.internals.events["$e1"]
        self.assertEqual(self.fetch(), {"status": "gone"})
        self.internals.times["$e1"] = EventTimes(None, None)
        self.assertEqual(self.fetch(), {"status": "gone"})

    def test_slow_remote_sender_name_is_omitted_after_two_seconds(self) -> None:
        self.api.blocked_state.add((ROOM, "@alice:test"))
        d = defer.ensureDeferred(self.scope.fetch("@bob:test", ROOM, "$e1", NOW))
        self.assertNoResult(d)
        self.clock.advance(2.1)
        body = self.successResultOf(d)
        self.assertEqual(body["status"], "ok")
        self.assertNotIn("sender_name", body)

from twisted.internet import task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals, add_delta, add_event
from unit_tests.fakes import FakeEvent, FakeModuleApi, run, state_event
from zuno_push.poller import STARTUP_LOOKBACK_MS, DeltaPoller
from zuno_push.ratelimit import RateLimiter
from zuno_push.rules import RingRules, call_devices, call_memberships
from zuno_push.store import PushStore

ROOM = "!room:test"
KEY = "A" * 43 + "="
NOW = 1_790_000_000_000


def membership(sender: str, *calls: tuple[str, str], event_id: str, device: str = "D") -> FakeEvent:
    return FakeEvent(
        type="m.call.member",
        sender=sender,
        state_key=sender,
        room_id=ROOM,
        event_id=event_id,
        content={
            "memberships": [
                {"call_id": c, "kind": k, "device_id": device, "expires_ts": 1} for c, k in calls
            ]
        },
    )


class CallMembershipParsingTests(unittest.TestCase):
    def test_parses_and_skips_malformed_entries(self) -> None:
        content = {
            "memberships": [
                {"call_id": "c1", "kind": "voice"},
                {"call_id": "c1", "kind": "video"},
                {"call_id": "", "kind": "voice"},
                {"call_id": "x" * 256, "kind": "voice"},
                {"kind": "voice"},
                "junk",
                {"call_id": "c2", "kind": 5},
                {"call_id": "c3", "kind": "screen"},
            ]
        }
        self.assertEqual(call_memberships(content), [("c1", "voice"), ("c3", "screen")])
        self.assertEqual(call_memberships({"memberships": "x"}), [])
        self.assertEqual(call_memberships(None), [])

    def test_call_devices_lists_the_devices_in_one_call(self) -> None:
        content = {
            "memberships": [
                {"call_id": "c1", "device_id": "A"},
                {"call_id": "c2", "device_id": "B"},
                {"call_id": "c1", "device_id": 5},
                {"call_id": "c1"},
                "junk",
                {"call_id": "c1", "device_id": "C"},
            ]
        }
        self.assertEqual(call_devices(content, "c1"), ["A", "C"])
        self.assertEqual(call_devices({"memberships": "x"}, "c1"), [])
        self.assertEqual(call_devices(None, "c1"), [])


class PollerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(NOW / 1000)
        self.api = FakeModuleApi(self.clock)
        self.internals = FakeInternals(self.api)
        self.store = PushStore(self.api)
        self.kicks = 0
        self.poller = self.restart()
        self.api.room_state[ROOM] = {
            ("m.room.create", ""): state_event("m.room.create", "", {}, ROOM),
            ("m.room.power_levels", ""): state_event("m.room.power_levels", "", {}, ROOM),
        }
        self.internals.required[ROOM] = 0
        self.internals.members[ROOM] = ["@alice:test", "@bob:test"]
        self.internals.devices.add(("@bob:test", "PHONE"))
        run(self.store.upsert_voip("@bob:test", "PHONE", "im.zuno.chat.ios.voip", "pk", 1, KEY))
        self.internals.max_so = 10
        run(self.poller.tick())

    def restart(self) -> DeltaPoller:
        rules = RingRules(self.api, self.internals, self.store, RateLimiter(self.api), 5000)
        return DeltaPoller(self.api, self.internals, self.store, rules, self.kick)

    def kick(self) -> None:
        self.kicks += 1

    def publish(self, event: FakeEvent, stream_id: int, received_ts: int = NOW) -> None:
        add_event(self.internals, event, received_ts)
        add_delta(self.internals, event, stream_id)

    def rings(self) -> list[tuple]:
        return self.api.conn.execute(
            "SELECT call_id, user_id, state FROM zuno_push_rings ORDER BY call_id"
        ).fetchall()

    def next_read_from(self) -> int:
        self.internals.max_so += 1
        run(self.poller.tick())
        return self.internals.delta_reads[-1][0]

    def poison(self, bad_event_id: str) -> None:
        real_get_event = self.internals.get_event

        async def get_event(event_id: str) -> object:
            if event_id == bad_event_id:
                raise RuntimeError("poison")
            return await real_get_event(event_id)

        self.internals.get_event = get_event

    def test_first_tick_rescans_the_last_five_minutes(self) -> None:
        old = NOW - STARTUP_LOOKBACK_MS - 1
        self.publish(membership("@alice:test", ("old", "voice"), event_id="$old"), 11, old)
        self.publish(membership("@alice:test", ("new", "voice"), event_id="$new"), 12, NOW - 20_000)
        run(self.restart().tick())
        self.assertEqual(self.internals.delta_reads, [(11, 12)])
        self.assertEqual(self.rings(), [("new", "@bob:test", "pending")])
        calls = self.api.conn.execute("SELECT call_id FROM zuno_push_calls").fetchall()
        self.assertEqual(calls, [("new",)])

    def test_a_restart_never_rings_a_call_twice(self) -> None:
        self.publish(membership("@alice:test", ("c1", "voice"), event_id="$m1"), 11)
        run(self.poller.tick())
        run(self.restart().tick())
        self.assertEqual(self.internals.delta_reads, [(10, 11), (10, 11)])
        self.assertEqual(len(self.rings()), 1)
        self.assertEqual(self.kicks, 1)

    def test_a_failed_startup_lookup_is_retried_next_tick(self) -> None:
        self.publish(membership("@alice:test", ("c1", "voice"), event_id="$m1"), 11)
        poller = self.restart()
        self.internals.fail.add("first_stream_ordering_after")
        run(poller.tick())
        self.assertEqual(self.rings(), [])
        self.internals.fail.clear()
        run(poller.tick())
        self.assertEqual(self.rings(), [("c1", "@bob:test", "pending")])

    def test_new_call_writes_pending_rings_and_kicks_the_ringer(self) -> None:
        self.publish(membership("@alice:test", ("c1", "video"), event_id="$m1"), 11)
        run(self.poller.tick())
        self.assertEqual(self.rings(), [("c1", "@bob:test", "pending")])
        self.assertEqual(self.kicks, 1)
        self.assertEqual(self.next_read_from(), 11)

    def test_republish_and_callee_join_never_ring_again(self) -> None:
        self.publish(membership("@alice:test", ("c1", "voice"), event_id="$m1"), 11)
        run(self.poller.tick())
        self.clock.advance(50)
        self.publish(
            membership("@alice:test", ("c1", "voice"), event_id="$m2"), 12, received_ts=NOW + 50_000
        )
        self.publish(
            membership("@bob:test", ("c1", "voice"), event_id="$m3", device="PHONE"),
            13,
            received_ts=NOW + 50_000,
        )
        run(self.poller.tick())
        self.assertEqual(len(self.rings()), 1)
        self.assertEqual(self.kicks, 1)
        self.assertEqual(self.endings(), [("PHONE", "joined")])
        seen = self.api.conn.execute("SELECT last_seen_ts FROM zuno_push_calls").fetchone()[0]
        self.assertEqual(seen, NOW + 50_000)

    def test_empty_membership_from_the_caller_ends_the_call(self) -> None:
        self.publish(membership("@alice:test", ("c1", "voice"), event_id="$m1"), 11)
        self.publish(membership("@alice:test", event_id="$m2"), 12)
        run(self.poller.tick())
        self.assertEqual(
            self.api.conn.execute("SELECT ended FROM zuno_push_calls").fetchone()[0], 1
        )

    def test_other_kinds_and_other_state_are_ignored(self) -> None:
        self.publish(membership("@alice:test", ("c1", "screen"), event_id="$m1"), 11)
        self.publish(state_event("m.room.topic", "", {"topic": "x"}, ROOM), 12)
        run(self.poller.tick())
        self.assertEqual(self.rings(), [])
        self.assertEqual(self.next_read_from(), 12)

    def test_backlog_is_read_in_clipped_batches(self) -> None:
        for i in range(250):
            self.publish(state_event("org.zuno.noise", f"k{i}", {}, ROOM), 11 + i)
        self.publish(membership("@alice:test", ("c9", "voice"), event_id="$late"), 300)
        run(self.poller.tick())
        self.assertEqual(self.rings(), [("c9", "@bob:test", "pending")])
        self.assertEqual(self.next_read_from(), 300)

    def test_position_rewinds_when_the_stream_went_backwards(self) -> None:
        self.internals.max_so = 5
        run(self.poller.tick())
        self.publish(membership("@alice:test", ("c1", "voice"), event_id="$m1"), 6)
        run(self.poller.tick())
        self.assertEqual(self.internals.delta_reads, [(5, 6)])
        self.assertEqual(self.rings(), [("c1", "@bob:test", "pending")])

    def test_failed_tick_keeps_the_position_for_a_retry(self) -> None:
        self.publish(membership("@alice:test", ("c1", "voice"), event_id="$m1"), 11)
        self.internals.fail.add("state_deltas")
        run(self.poller.tick())
        self.internals.fail.clear()
        run(self.poller.tick())
        self.assertEqual(self.internals.delta_reads, [(10, 11)])
        self.assertEqual(self.rings(), [("c1", "@bob:test", "pending")])

    def test_a_room_state_outage_is_retried_and_then_rings(self) -> None:
        self.publish(membership("@alice:test", ("c1", "voice"), event_id="$m1"), 11)
        real_get_room_state = self.api.get_room_state

        async def unavailable(room_id: str, event_filter: object = None) -> dict:
            raise RuntimeError("state unavailable")

        self.api.get_room_state = unavailable
        run(self.poller.tick())
        self.assertEqual(self.rings(), [])
        self.api.get_room_state = real_get_room_state
        run(self.poller.tick())
        self.assertEqual(self.rings(), [("c1", "@bob:test", "pending")])

    def test_a_member_list_outage_is_retried_and_then_rings(self) -> None:
        self.publish(membership("@alice:test", ("c1", "voice"), event_id="$m1"), 11)
        self.internals.fail.add("local_joined_users")
        run(self.poller.tick())
        self.internals.fail.clear()
        run(self.poller.tick())
        self.assertEqual(self.rings(), [("c1", "@bob:test", "pending")])

    def test_a_call_event_that_keeps_failing_is_skipped_after_three_ticks(self) -> None:
        self.publish(membership("@alice:test", ("c1", "voice"), event_id="$bad"), 11)
        self.publish(membership("@alice:test", ("c2", "voice"), event_id="$good"), 12)
        self.poison("$bad")
        for _ in range(2):
            run(self.poller.tick())
        self.assertEqual(self.internals.delta_reads, [(10, 12), (10, 12)])
        run(self.poller.tick())
        self.assertEqual(self.rings(), [("c2", "@bob:test", "pending")])
        self.assertEqual(self.next_read_from(), 12)

    def test_an_earlier_call_still_rings_when_a_later_event_fails(self) -> None:
        self.publish(membership("@alice:test", ("c1", "voice"), event_id="$good"), 11)
        self.publish(membership("@alice:test", ("c2", "voice"), event_id="$bad"), 12)
        self.poison("$bad")
        run(self.poller.tick())
        self.assertEqual(self.rings(), [("c1", "@bob:test", "pending")])
        self.assertEqual(self.kicks, 1)

    def test_a_failed_next_batch_still_kicks_the_ringer(self) -> None:
        self.publish(membership("@alice:test", ("c1", "voice"), event_id="$m1"), 11)
        for i in range(100):
            self.publish(state_event("org.zuno.noise", f"k{i}", {}, ROOM), 12 + i)
        real_state_deltas = self.internals.state_deltas
        reads: list[int] = []

        async def state_deltas(from_pos: int, to_pos: int) -> tuple:
            reads.append(from_pos)
            if len(reads) > 1:
                raise RuntimeError("database gone")
            return await real_state_deltas(from_pos, to_pos)

        self.internals.state_deltas = state_deltas
        run(self.poller.tick())
        self.assertEqual(reads, [10, 110])
        self.assertEqual(self.rings(), [("c1", "@bob:test", "pending")])
        self.assertEqual(self.kicks, 1)

    def test_one_event_rings_at_most_four_calls(self) -> None:
        calls = [(f"c{i}", "voice") for i in range(50)]
        self.publish(membership("@alice:test", *calls, event_id="$many"), 11)
        run(self.poller.tick())
        self.assertEqual([r[0] for r in self.rings()], ["c0", "c1", "c2", "c3"])

    def test_callee_first_race_rings_the_callers_other_devices(self) -> None:
        self.internals.devices.add(("@alice:test", "IPAD"))
        run(
            self.store.upsert_voip(
                "@alice:test", "IPAD", "im.zuno.chat.ios.voip", "pk-ipad", 1, KEY
            )
        )
        self.publish(membership("@bob:test", ("c1", "voice"), event_id="$callee"), 11)
        self.publish(membership("@alice:test", ("c1", "voice"), event_id="$caller"), 12)
        run(self.poller.tick())
        self.assertEqual(
            self.rings(),
            [("c1", "@alice:test", "pending"), ("c1", "@bob:test", "suppressed:sender")],
        )

    def endings(self) -> list[tuple]:
        return self.api.conn.execute(
            "SELECT device_id, ending FROM zuno_push_rings WHERE user_id = '@bob:test'"
            " ORDER BY device_id"
        ).fetchall()

    def test_the_callers_hang_up_marks_every_ring_and_kicks_the_ringer(self) -> None:
        self.publish(membership("@alice:test", ("c1", "voice"), event_id="$m1"), 11)
        run(self.poller.tick())
        self.publish(membership("@alice:test", event_id="$m2"), 12)
        run(self.poller.tick())
        self.assertEqual(self.endings(), [("PHONE", "hung_up")])
        self.assertEqual(self.kicks, 2)

    def test_a_join_from_one_device_ends_the_users_other_rings(self) -> None:
        self.internals.devices.add(("@bob:test", "TABLET"))
        run(self.store.upsert_voip("@bob:test", "TABLET", "im.zuno.chat.android", "f", 1, KEY))
        self.publish(membership("@alice:test", ("c1", "voice"), event_id="$m1"), 11)
        run(self.poller.tick())
        self.publish(membership("@bob:test", ("c1", "voice"), event_id="$m2", device="TABLET"), 12)
        self.publish(membership("@bob:test", ("c1", "voice"), event_id="$m3", device="TABLET"), 13)
        self.publish(membership("@alice:test", event_id="$m4"), 14)
        run(self.poller.tick())
        self.assertEqual(self.endings(), [("PHONE", "answered_elsewhere"), ("TABLET", "joined")])
        self.assertEqual(self.kicks, 2)

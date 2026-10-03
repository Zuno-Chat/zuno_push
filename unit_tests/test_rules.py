from twisted.internet import defer, task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals
from unit_tests.fakes import FakeModuleApi, run, state_event
from zuno_push.ratelimit import RateLimiter
from zuno_push.rules import CallStart, RingRules, invite_shaped
from zuno_push.store import PushStore

ROOM = "!room:test"
KEY = "A" * 43 + "="
NOW = 1_790_000_000_000


class RulesTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(NOW / 1000)
        self.api = FakeModuleApi(self.clock)
        self.internals = FakeInternals(self.api)
        self.store = PushStore(self.api)
        self.rules = RingRules(
            self.api, self.internals, self.store, RateLimiter(self.api), max_targets=5000
        )
        self.api.room_state[ROOM] = {
            ("m.room.create", ""): state_event("m.room.create", "", {}, ROOM),
            ("m.room.power_levels", ""): state_event("m.room.power_levels", "", {}, ROOM),
            ("m.room.name", ""): state_event("m.room.name", "", {"name": "Design team"}, ROOM),
            ("m.room.member", "@alice:test"): state_event(
                "m.room.member", "@alice:test", {"displayname": "Alice"}, ROOM
            ),
        }
        self.internals.required[ROOM] = 0
        self.internals.members[ROOM] = ["@alice:test"]

    def member(self, user_id: str, device_id: str = "PHONE", level: int = 0) -> None:
        self.internals.members[ROOM].append(user_id)
        self.internals.levels[(ROOM, user_id)] = level
        self.internals.devices.add((user_id, device_id))
        run(
            self.store.upsert_voip(
                user_id, device_id, "im.zuno.chat.ios.voip", f"pk-{user_id}-{device_id}", 1, KEY
            )
        )

    def evaluate(self, received_ts: int = NOW, kind: str = "voice") -> dict:
        result = run(
            self.rules.evaluate(CallStart(ROOM, "c1", "@alice:test", kind, received_ts), NOW)
        )
        self.result = result
        return {(r.user_id, r.device_id): r.state for r in result.rings}


class RingRuleTests(RulesTestBase):
    def test_happy_path_rings_every_registered_device(self) -> None:
        self.member("@bob:test", "PHONE")
        self.member("@bob:test", "IPAD")
        self.member("@carol:test")
        self.assertEqual(
            self.evaluate(),
            {
                ("@bob:test", "IPAD"): "pending",
                ("@bob:test", "PHONE"): "pending",
                ("@carol:test", "PHONE"): "pending",
            },
        )
        self.assertEqual((self.result.cname, self.result.rname), ("Alice", "Design team"))

    def test_callers_own_devices_are_suppressed_as_sender(self) -> None:
        self.internals.devices.add(("@alice:test", "IPAD"))
        run(self.store.upsert_voip("@alice:test", "IPAD", "im.zuno.chat.ios.voip", "pk-a", 1, KEY))
        self.assertEqual(self.evaluate(), {("@alice:test", "IPAD"): "suppressed:sender"})

    def test_stale_call_is_not_fresh(self) -> None:
        self.member("@bob:test")
        self.assertEqual(
            self.evaluate(received_ts=NOW - 30_001),
            {("@bob:test", "PHONE"): "suppressed:not_fresh"},
        )
        self.assertEqual(
            self.evaluate(received_ts=NOW - 30_000), {("@bob:test", "PHONE"): "pending"}
        )

    def test_user_already_live_in_this_call(self) -> None:
        self.member("@bob:test")
        self.api.room_state[ROOM][("m.call.member", "@bob:test")] = state_event(
            "m.call.member",
            "@bob:test",
            {"memberships": [{"call_id": "c1", "kind": "voice"}]},
            ROOM,
        )
        self.assertEqual(self.evaluate(), {("@bob:test", "PHONE"): "suppressed:live"})

    def test_power_level_below_the_call_level(self) -> None:
        self.internals.required[ROOM] = 50
        self.member("@bob:test", level=0)
        self.member("@carol:test", level=50)
        self.assertEqual(
            self.evaluate(),
            {("@bob:test", "PHONE"): "suppressed:not_allowed", ("@carol:test", "PHONE"): "pending"},
        )

    def test_ignored_caller(self) -> None:
        self.member("@bob:test")
        self.api.account_data_manager.data[("@bob:test", "m.ignored_user_list")] = {
            "ignored_users": {"@alice:test": {}}
        }
        self.assertEqual(self.evaluate(), {("@bob:test", "PHONE"): "suppressed:ignored"})

    def test_push_rules_say_no(self) -> None:
        self.member("@bob:test")
        self.internals.notify = lambda room_id, user_id: False
        self.assertEqual(self.evaluate(), {("@bob:test", "PHONE"): "suppressed:no_notify"})

    def test_invite_shape_follows_room_encryption(self) -> None:
        self.member("@bob:test")
        self.api.room_state[ROOM][("m.room.member", "@bob:test")] = state_event(
            "m.room.member", "@bob:test", {"displayname": "Bob"}, ROOM
        )
        self.evaluate(kind="video")
        event, user_id, display_name = self.internals.notify_calls[-1]
        self.assertEqual(
            (event["type"], event["content"]["msgtype"], event["content"]["body"]),
            ("m.room.message", "im.zuno.call_invite", "Incoming video call"),
        )
        self.assertEqual((user_id, display_name), ("@bob:test", "Bob"))
        self.api.room_state[ROOM][("m.room.encryption", "")] = state_event(
            "m.room.encryption", "", {"algorithm": "m.megolm.v1.aes-sha2"}, ROOM
        )
        self.evaluate()
        self.assertEqual(self.internals.notify_calls[-1][0]["type"], "m.room.encrypted")
        self.assertEqual(
            invite_shaped(CallStart(ROOM, "c", "@a:test", "voice", 0), True)["content"],
            {"algorithm": "m.megolm.v1.aes-sha2"},
        )

    def test_device_gone_and_failing_voip(self) -> None:
        self.member("@bob:test", "PHONE")
        self.member("@bob:test", "OLD")
        self.member("@bob:test", "FLAKY")
        self.internals.devices.discard(("@bob:test", "OLD"))
        for _ in range(3):
            run(self.store.record_send("@bob:test", "FLAKY", "pk-@bob:test-FLAKY", "failed", NOW))
        self.assertEqual(
            self.evaluate(),
            {
                ("@bob:test", "PHONE"): "pending",
                ("@bob:test", "OLD"): "suppressed:device_gone",
                ("@bob:test", "FLAKY"): "suppressed:voip_failing",
            },
        )

    def test_dm_flag_and_names(self) -> None:
        self.member("@bob:test")
        self.api.account_data_manager.data[("@bob:test", "m.direct")] = {"@alice:test": [ROOM]}
        self.evaluate()
        self.assertTrue(self.result.rings[0].dm)


class GroupAndLimitTests(RulesTestBase):
    def test_group_call_rings_everyone_up_to_the_safety_valve(self) -> None:
        for i in range(40):
            self.member(f"@u{i:02}:test")
        states = self.evaluate()
        self.assertEqual(len(states), 40)
        self.assertEqual(set(states.values()), {"pending"})
        self.rules = RingRules(
            self.api, self.internals, self.store, RateLimiter(self.api), max_targets=30
        )
        self.api.conn.execute("DELETE FROM zuno_push_counters")
        states = self.evaluate()
        self.assertEqual(list(states.values()).count("pending"), 30)
        self.assertEqual(list(states.values()).count("suppressed:target_cap"), 10)

    def test_limits_count_call_starts_not_targets(self) -> None:
        for i in range(25):
            self.member(f"@u{i:02}:test")
        for _ in range(8):
            self.assertEqual(set(self.evaluate().values()), {"pending"})
        self.assertEqual(set(self.evaluate().values()), {"suppressed:rate_limited"})

    def test_room_limit(self) -> None:
        self.member("@bob:test")
        self.member("@carol:test")
        for i in range(20):
            other = run(
                self.rules.evaluate(CallStart(ROOM, f"c{i}", f"@x{i}:test", "voice", NOW), NOW)
            )
            self.assertTrue(all(r.state == "pending" for r in other.rings))
        self.assertEqual(set(self.evaluate().values()), {"suppressed:rate_limited"})


class FailureModeTests(RulesTestBase):
    def test_power_level_failure_fails_closed(self) -> None:
        self.member("@bob:test")
        self.internals.fail.add("user_level")
        self.assertEqual(self.evaluate(), {("@bob:test", "PHONE"): "suppressed:not_allowed"})

    def test_ignore_list_failure_fails_closed(self) -> None:
        self.member("@bob:test")
        self.api.account_data_manager.fail.add("m.ignored_user_list")
        self.assertEqual(self.evaluate(), {("@bob:test", "PHONE"): "suppressed:ignored"})

    def test_push_rule_failure_fails_open(self) -> None:
        self.member("@bob:test")
        self.internals.fail.add("would_notify")
        self.assertEqual(self.evaluate(), {("@bob:test", "PHONE"): "pending"})

    def test_device_check_failure_fails_open(self) -> None:
        self.member("@bob:test")
        self.internals.fail.add("device_exists")
        self.assertEqual(self.evaluate(), {("@bob:test", "PHONE"): "pending"})

    def test_room_state_failure_suppresses_everyone(self) -> None:
        self.member("@bob:test")
        del self.api.room_state[ROOM][("m.room.create", "")]
        self.assertEqual(self.evaluate(), {("@bob:test", "PHONE"): "suppressed:not_allowed"})

    def test_remote_caller_name_times_out_after_two_seconds(self) -> None:
        self.member("@bob:test")
        self.api.blocked_state.add((ROOM, "@alice:test"))
        d = defer.ensureDeferred(
            self.rules.evaluate(CallStart(ROOM, "c1", "@alice:test", "voice", NOW), NOW)
        )
        self.assertNoResult(d)
        self.clock.advance(2.1)
        result = self.successResultOf(d)
        self.assertEqual(result.cname, "")
        self.assertEqual([r.state for r in result.rings], ["pending"])

    def test_no_targets_when_membership_is_unavailable(self) -> None:
        self.member("@bob:test")
        self.internals.fail.add("local_joined_users")
        self.assertEqual(self.evaluate(), {})

import base64
from typing import Any

from synapse.api.errors import HttpResponseException
from twisted.internet import defer, task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals
from unit_tests.fakes import FakeModuleApi, run
from unit_tests.test_sealer import open_blob
from unit_tests.test_sender import failing
from zuno_push.ringer import Ringer
from zuno_push.sealer import expiry
from zuno_push.sender import SygnalSender
from zuno_push.store import CallRow, PushStore, RingRow

KEY = bytes(range(32))
KEY_B64 = base64.b64encode(KEY).decode()
NOW = 1_790_000_000_000
ANDROID = "im.zuno.chat.android"


class RingerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(NOW / 1000)
        self.api = FakeModuleApi(self.clock)
        self.store = PushStore(self.api)
        sender = SygnalSender(
            self.api, FakeInternals(self.api), "http://sygnal/_matrix/push/v1/notify"
        )
        self.ringer = Ringer(self.api, self.store, sender, (ANDROID,))

    def call(
        self,
        call_id: str,
        received_ts: int,
        *rings: RingRow,
        cname: str = "Alice",
        rname: str = "Design",
    ) -> None:
        row = CallRow("!r:test", call_id, "@alice:test", "video", received_ts, cname, rname)
        run(self.store.create_call_with_rings(row, list(rings), self.api.get_current_time_msec()))

    def register(
        self, user_id: str, device_id: str, pushkey: str, app_id: str = "im.zuno.chat.ios.voip"
    ) -> None:
        run(self.store.upsert_voip(user_id, device_id, app_id, pushkey, 9, KEY_B64))

    def state(self, user_id: str, device_id: str) -> tuple:
        return run(self.store.ring_and_call("!r:test", "c1", user_id, device_id))[0]

    def sent_device(self) -> dict:
        return self.api.http_client.requests[0][1]["notification"]["devices"][0]

    def sent_blob(self, key: bytes = KEY) -> tuple[int, int, dict]:
        blob = self.sent_device()["data"]["default_payload"]["z"]
        return open_blob(key, base64.b64decode(blob))

    def test_pending_ring_is_sealed_and_sent(self) -> None:
        self.register("@bob:test", "B", "pk-b")
        self.call("c1", NOW - 1_000, RingRow("@bob:test", "B", "pending", False))
        run(self.ringer.drive())
        self.assertEqual(self.state("@bob:test", "B"), ("sent", NOW))
        kid, x, plain = self.sent_blob()
        self.assertEqual(kid, 9)
        self.assertEqual(x, NOW // 1000 + 45)
        self.assertEqual(
            plain,
            {
                "room": "!r:test",
                "call": "c1",
                "caller": "@alice:test",
                "cname": "Alice",
                "rname": "Design",
                "kind": "video",
                "ts": NOW,
                "rts": NOW - 1_000,
            },
        )
        self.assertEqual(run(self.store.get_device("@bob:test", "B")).last_result, "sent")

    def test_dm_rings_carry_an_empty_room_name(self) -> None:
        self.register("@bob:test", "B", "pk-b")
        self.call("c1", NOW, RingRow("@bob:test", "B", "pending", True))
        run(self.ringer.drive())
        self.assertEqual(self.sent_blob()[2]["rname"], "")

    def test_device_without_token_becomes_no_token(self) -> None:
        self.call("c1", NOW, RingRow("@bob:test", "B", "pending", False))
        run(self.ringer.drive())
        self.assertEqual(self.state("@bob:test", "B"), ("no_token", NOW))
        self.assertEqual(self.api.http_client.requests, [])

    def test_rejected_pushkey_fails_the_ring_and_clears_the_token(self) -> None:
        self.register("@bob:test", "B", "pk-b")
        self.call("c1", NOW, RingRow("@bob:test", "B", "pending", False))
        self.api.http_client.responses.append(lambda: {"rejected": ["pk-b"]})
        run(self.ringer.drive())
        self.assertEqual(self.state("@bob:test", "B")[0], "failed")
        self.assertFalse(run(self.store.get_device("@bob:test", "B")).has_voip)

    def test_stale_pending_rows_expire_instead_of_ringing(self) -> None:
        self.register("@bob:test", "B", "pk-b")
        self.call("c1", NOW - 31_000, RingRow("@bob:test", "B", "pending", False))
        run(self.ringer.drive())
        self.assertEqual(self.state("@bob:test", "B"), ("failed", NOW))
        self.assertEqual(self.api.http_client.requests, [])

    def test_a_call_the_caller_already_left_is_not_rung(self) -> None:
        self.register("@bob:test", "B", "pk-b")
        self.call("c1", NOW, RingRow("@bob:test", "B", "pending", False))
        run(self.store.end_calls("!r:test", "@alice:test", []))
        run(self.ringer.drive())
        self.assertEqual(self.api.http_client.requests, [])

    def test_restart_redrives_pending_rows(self) -> None:
        self.register("@bob:test", "B", "pk-b")
        self.call("c1", NOW - 5_000, RingRow("@bob:test", "B", "pending", False))
        fresh = Ringer(
            self.api,
            self.store,
            SygnalSender(self.api, FakeInternals(self.api), "http://s/_matrix/push/v1/notify"),
            (ANDROID,),
        )
        run(fresh.drive())
        self.assertEqual(self.state("@bob:test", "B")[0], "sent")

    def test_group_sends_in_batches_of_eight_and_a_failure_stays_local(self) -> None:
        rings = []
        for i in range(10):
            self.register(f"@u{i}:test", "P", f"pk{i}")
            rings.append(RingRow(f"@u{i}:test", "P", "pending", False))
        self.call("c1", NOW, *rings)
        gates: list[defer.Deferred] = []

        def slow() -> Any:
            d: defer.Deferred = defer.Deferred()
            gates.append(d)
            return d

        self.api.http_client.responses.extend([slow] * 10)
        d = defer.ensureDeferred(self.ringer.drive())
        self.assertEqual(len(gates), 8)
        for gate in gates[:7]:
            gate.callback({"rejected": []})

        gates[7].errback(HttpResponseException(400, "bad", b""))
        self.assertEqual(len(gates), 10)
        gates[8].callback({"rejected": []})
        gates[9].callback({"rejected": []})
        self.successResultOf(d)
        states = [
            r[0]
            for r in self.api.conn.execute("SELECT state FROM zuno_push_rings ORDER BY user_id")
        ]
        self.assertEqual(states.count("sent"), 9)
        self.assertEqual(states.count("failed"), 1)

    def test_a_new_call_goes_out_before_an_older_group_calls_remaining_rings(self) -> None:
        rings = []
        for i in range(10):
            self.register(f"@u{i}:test", "P", f"pk{i}")
            rings.append(RingRow(f"@u{i}:test", "P", "pending", False))
        self.call("c1", NOW - 1_000, *rings)
        gates: list[defer.Deferred] = []

        def slow() -> Any:
            d: defer.Deferred = defer.Deferred()
            gates.append(d)
            return d

        self.api.http_client.responses.extend([slow] * 8)
        d = defer.ensureDeferred(self.ringer.drive())
        self.register("@new:test", "P", "pk-new")
        self.call("c2", NOW, RingRow("@new:test", "P", "pending", False))
        for gate in gates:
            gate.callback({"rejected": []})
        self.successResultOf(d)
        pushkeys = [
            body["notification"]["devices"][0]["pushkey"]
            for _, body in self.api.http_client.requests
        ]
        self.assertEqual(pushkeys[8], "pk-new")
        self.assertEqual(len(pushkeys), 11)

    def test_sygnal_outage_never_marks_the_device_failing(self) -> None:
        self.register("@bob:test", "B", "pk-b")

        def refuse() -> Any:
            raise ConnectionRefusedError()

        outages = [[refuse, refuse], [failing(502)] * 2, [failing(500)] * 2, [failing(400)]]
        for i, responses in enumerate(outages):
            self.call(f"c{i}", NOW, RingRow("@bob:test", "B", "pending", False))
            self.api.http_client.responses.extend(responses)
            d = defer.ensureDeferred(self.ringer.drive())
            self.clock.advance(1)
            self.successResultOf(d)
        device = run(self.store.get_device("@bob:test", "B"))
        self.assertEqual((device.has_voip, device.last_result), (True, "failed"))
        states = {r[0] for r in self.api.conn.execute("SELECT state FROM zuno_push_rings")}
        self.assertEqual(states, {"failed"})

    def test_token_rotated_between_evaluation_and_send(self) -> None:
        self.register("@bob:test", "B", "pk-old")
        self.call("c1", NOW, RingRow("@bob:test", "B", "pending", False))
        new_key = bytes(range(100, 132))
        run(
            self.store.upsert_voip(
                "@bob:test",
                "B",
                "im.zuno.chat.ios.voip",
                "pk-new",
                10,
                base64.b64encode(new_key).decode(),
            )
        )
        run(self.ringer.drive())
        self.assertEqual(self.sent_device()["pushkey"], "pk-new")
        kid, _, plain = self.sent_blob(new_key)
        self.assertEqual((kid, plain["call"]), (10, "c1"))
        self.assertEqual(run(self.store.get_device("@bob:test", "B")).last_result, "sent")

    def test_kick_while_driving_runs_again(self) -> None:
        self.register("@bob:test", "B", "pk-b")
        self.call("c1", NOW, RingRow("@bob:test", "B", "pending", False))
        gate: defer.Deferred = defer.Deferred()
        self.api.http_client.responses.extend([lambda: gate])
        self.ringer.kick()
        self.register("@carol:test", "C", "pk-c")
        self.call("c2", NOW, RingRow("@carol:test", "C", "pending", False))
        self.ringer.kick()
        gate.callback({"rejected": []})
        self.assertEqual(len(self.api.http_client.requests), 2)

    def ending(self, user_id: str, device_id: str) -> str | None:
        return self.api.conn.execute(
            "SELECT ending FROM zuno_push_rings WHERE user_id = ? AND device_id = ?",
            (user_id, device_id),
        ).fetchone()[0]

    def test_hang_up_sends_one_sealed_end_to_the_rung_android_device(self) -> None:
        self.register("@bob:test", "B", "fcm-b", ANDROID)
        self.call("c1", NOW - 1_000, RingRow("@bob:test", "B", "pending", False))
        run(self.ringer.drive())
        self.clock.advance(2)
        run(self.store.end_calls("!r:test", "@alice:test", []))
        run(self.ringer.drive())
        run(self.ringer.drive())
        self.assertEqual(len(self.api.http_client.requests), 2)
        notification = self.api.http_client.requests[1][1]["notification"]
        self.assertEqual((notification["event_id"], notification["prio"]), ("$z", "high"))
        device = notification["devices"][0]
        self.assertEqual((device["app_id"], device["pushkey"]), (ANDROID, "fcm-b"))
        self.assertEqual(list(device["data"]["default_payload"]), ["ze"])
        kid, x, plain = open_blob(KEY, base64.b64decode(device["data"]["default_payload"]["ze"]))
        self.assertEqual((kid, x), (9, expiry(NOW + 2_000, NOW - 1_000)))
        self.assertEqual(
            plain, {"room": "!r:test", "call": "c1", "reason": "hung_up", "ts": NOW + 2_000}
        )
        self.assertEqual(self.ending("@bob:test", "B"), "sent")
        self.assertEqual(self.state("@bob:test", "B"), ("sent", NOW))

    def test_ios_rings_get_no_end(self) -> None:
        self.register("@bob:test", "B", "pk-b")
        self.call("c1", NOW, RingRow("@bob:test", "B", "pending", False))
        run(self.ringer.drive())
        run(self.store.end_calls("!r:test", "@alice:test", []))
        run(self.ringer.drive())
        self.assertEqual(len(self.api.http_client.requests), 1)

    def test_a_ring_still_sending_at_hang_up_is_followed_by_its_end(self) -> None:
        self.register("@bob:test", "B", "fcm-b", ANDROID)
        self.call("c1", NOW, RingRow("@bob:test", "B", "pending", False))
        gate: defer.Deferred = defer.Deferred()
        self.api.http_client.responses.append(lambda: gate)
        self.ringer.kick()
        run(self.store.end_calls("!r:test", "@alice:test", []))
        self.ringer.kick()
        gate.callback({"rejected": []})
        payloads = [
            b["notification"]["devices"][0]["data"]["default_payload"]
            for _, b in self.api.http_client.requests
        ]
        self.assertEqual([list(p) for p in payloads], [["z"], ["ze"]])

    def test_no_end_once_the_ring_has_expired_on_its_own(self) -> None:
        self.register("@bob:test", "B", "fcm-b", ANDROID)
        self.call("c1", NOW, RingRow("@bob:test", "B", "pending", False))
        run(self.ringer.drive())
        self.clock.advance(76)
        run(self.store.end_calls("!r:test", "@alice:test", []))
        run(self.ringer.drive())
        self.assertEqual(len(self.api.http_client.requests), 1)

    def test_a_failed_end_is_not_retried(self) -> None:
        self.register("@bob:test", "B", "fcm-b", ANDROID)
        self.call("c1", NOW, RingRow("@bob:test", "B", "pending", False))
        run(self.ringer.drive())
        run(self.store.answer_call("!r:test", "c1", "@bob:test", ["OTHER"]))
        self.api.http_client.responses.append(failing(400))
        run(self.ringer.drive())
        run(self.ringer.drive())
        self.assertEqual(len(self.api.http_client.requests), 2)
        self.assertEqual(self.ending("@bob:test", "B"), "failed")

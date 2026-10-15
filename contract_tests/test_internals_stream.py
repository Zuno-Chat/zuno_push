from __future__ import annotations

from typing import Any
from unittest.mock import Mock

from synapse.server import HomeServer
from synapse.util.clock import Clock
from tests.unittest import override_config
from tests.zuno_push_contract.base import ContractTestCase
from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

from zuno_push.schema import migrate


class StreamContractTests(ContractTestCase):
    def test_state_deltas_are_clipped_and_complete(self) -> None:
        alice = self.user("alice")
        room_id = self.room_with(alice)
        start = self.internals.max_stream_ordering()
        sent = {
            self.helper.send_state(
                room_id, "org.zuno.test", {"n": i}, tok=alice[1], state_key=f"k{i}"
            )["event_id"]
            for i in range(150)
        }
        top = self.internals.max_stream_ordering()
        pos, rows = self.get_success(self.internals.state_deltas(start, top))
        self.assertLessEqual(len(rows), 100)
        self.assertLess(pos, top)
        seen = {r.event_id for r in rows}
        while pos < top:
            previous = pos
            pos, rows = self.get_success(self.internals.state_deltas(pos, top))
            self.assertTrue(all(r.stream_id > previous for r in rows))
            seen |= {r.event_id for r in rows}
        self.assertEqual(pos, top)
        self.assertTrue(sent <= seen)

    def test_room_creation_deltas_are_read_whole(self) -> None:
        alice = self.user("alice")
        start = self.internals.max_stream_ordering()
        room_id = self.room_with(alice)
        top = self.internals.max_stream_ordering()
        pos, rows = self.get_success(self.internals.state_deltas(start, top))
        self.assertEqual(pos, top)
        types = {(r.event_type, r.state_key) for r in rows if r.room_id == room_id}
        self.assertIn(("m.room.create", ""), types)
        self.assertIn(("m.room.power_levels", ""), types)
        self.assertIn(("m.room.member", alice[0]), types)

    def test_delta_event_and_received_ts(self) -> None:
        alice = self.user("alice")
        room_id = self.room_with(alice)
        now = self.api.get_current_time_msec()
        event_id = self.helper.send_state(
            room_id, "m.call.member", {"memberships": []}, tok=alice[1], state_key=alice[0]
        )["event_id"]
        event = self.get_success(self.internals.get_event(event_id))
        self.assertEqual(
            (event.type, event.sender, event.room_id), ("m.call.member", alice[0], room_id)
        )
        times = self.get_success(self.internals.event_times(event_id))
        self.assertEqual(times.origin_server_ts, event.origin_server_ts)
        self.assertLessEqual(abs(times.received_ts - now), 1000)
        self.assertIsNone(self.get_success(self.internals.get_event("$missing")))
        self.assertIsNone(self.get_success(self.internals.event_times("$missing")))

    def test_first_stream_ordering_after_a_receive_time(self) -> None:
        alice = self.user("alice")
        room_id = self.room_with(alice)
        self.reactor.advance(10)
        since = self.api.get_current_time_msec()
        event_id = self.helper.send(room_id, "after", tok=alice[1])["event_id"]
        position = self.get_success(self.store.get_position_for_event(event_id)).stream
        self.assertEqual(
            self.get_success(self.internals.first_stream_ordering_after(since)), position
        )
        self.reactor.advance(10)
        later = self.api.get_current_time_msec()
        self.assertEqual(
            self.get_success(self.internals.first_stream_ordering_after(later)),
            self.internals.max_stream_ordering() + 1,
        )

    def test_device_exists_ignores_hidden_and_missing(self) -> None:
        alice = self.user("alice")
        device = self.device_of(alice[1])
        self.assertTrue(self.get_success(self.internals.device_exists(alice[0], device)))
        self.assertFalse(self.get_success(self.internals.device_exists(alice[0], "NOPE")))
        self.get_success(
            self.store.db_pool.simple_update_one(
                "devices", {"user_id": alice[0], "device_id": device}, {"hidden": True}
            )
        )
        self.assertFalse(self.get_success(self.internals.device_exists(alice[0], device)))

    def test_rows_for_missing_devices_are_deleted(self) -> None:
        alice = self.user("alice")
        device = self.device_of(alice[1])

        def seed(t: Any) -> None:
            for dev in (device, "GONE"):
                t.execute(
                    "INSERT INTO zuno_push_devices (user_id, device_id) VALUES (?, ?)",
                    (alice[0], dev),
                )

        self.get_success(migrate(self.api))
        self.get_success(self.api.run_db_interaction("seed", seed))
        self.assertEqual(self.get_success(self.internals.delete_rows_for_missing_devices()), 1)

    def test_pushers_for_device(self) -> None:
        alice = self.user("alice")
        device = self.device_of(alice[1])
        self.get_success(
            self.hs.get_pusherpool().add_or_update_pusher(
                user_id=alice[0],
                device_id=device,
                kind="http",
                app_id="im.zuno.chat.ios",
                app_display_name="Zuno",
                device_display_name="iPhone",
                pushkey="cHVzaGtleQ==",
                lang=None,
                data={
                    "url": "http://sygnal.invalid/_matrix/push/v1/notify",
                    "format": "event_id_only",
                },
            )
        )
        rows = self.get_success(self.internals.pushers_for_device(alice[0], device))
        self.assertEqual([r.app_id for r in rows], ["im.zuno.chat.ios"])
        self.assertEqual(self.get_success(self.internals.pushers_for_device(alice[0], "OTHER")), [])

    def test_within_times_out(self) -> None:
        async def never() -> None:
            await defer.Deferred()

        d = defer.ensureDeferred(self.internals.within(1.0, never))
        self.reactor.advance(1.5)
        self.get_failure(d, defer.TimeoutError)

    def test_main_process_is_the_pusher_instance_by_default(self) -> None:
        self.assertTrue(self.internals.is_pusher_instance_for("@anyone:test"))

    @override_config({"pusher_instances": ["pusher1"]})
    def test_pusher_instance_elsewhere(self) -> None:
        self.assertFalse(self.internals.is_pusher_instance_for("@anyone:test"))


class TestAlertContractTests(ContractTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.pushed: list[dict[str, Any]] = []
        client = Mock()

        def post_json_get_json(url: str, body: dict[str, Any]) -> Any:
            self.pushed.append(body)
            return defer.succeed({"rejected": []})

        client.post_json_get_json = post_json_get_json
        return self.setup_test_homeserver(proxied_blocklisted_http_client=client)

    def test_send_http_push_notification_reaches_only_this_device_unmodified(self) -> None:
        alice = self.user("alice")
        device = self.device_of(alice[1])
        data = {
            "url": "http://sygnal.invalid/_matrix/push/v1/notify",
            "format": "event_id_only",
            "default_payload": {"aps": {"mutable-content": 1, "alert": {"loc-key": "MSG"}}},
        }
        self.get_success(
            self.hs.get_pusherpool().add_or_update_pusher(
                user_id=alice[0],
                device_id=device,
                kind="http",
                app_id="im.zuno.chat.ios",
                app_display_name="Zuno",
                device_display_name="iPhone",
                pushkey="cHVzaGtleQ==",
                lang=None,
                data=data,
            )
        )
        result = self.get_success(
            self.api.send_http_push_notification(
                alice[0], device, {"event_id": "$zuno_test_1", "prio": "high"}
            )
        )
        self.assertEqual(result, {device: True})
        self.assertEqual(len(self.pushed), 1)
        notification = self.pushed[0]["notification"]
        self.assertEqual(notification["event_id"], "$zuno_test_1")
        self.assertEqual(notification["prio"], "high")
        self.assertNotIn("room_id", notification)
        self.assertEqual(
            notification["devices"][0]["data"]["default_payload"], data["default_payload"]
        )
        pusher = next(iter(self.hs.get_pusherpool().pushers[alice[0]].values()))
        self.assertEqual(pusher.data_minus_url["default_payload"], data["default_payload"])
        self.assertEqual(
            self.get_success(
                self.api.send_http_push_notification(alice[0], "OTHER", {"event_id": "$x"})
            ),
            {},
        )

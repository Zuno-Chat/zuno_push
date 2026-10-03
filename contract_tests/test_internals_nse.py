from __future__ import annotations

from tests.zuno_push_contract.base import ContractTestCase

from zuno_push.features import run_probes


class NseScopeContractTests(ContractTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.alice = self.user("alice")
        self.bob = self.user("bob")
        self.carol = self.user("carol")
        self.room = self.room_with(self.alice, self.bob, self.carol)

    def send(self, body: str, **content: object) -> str:
        return self.helper.send_event(
            self.room,
            "m.room.message",
            {"msgtype": "m.text", "body": body, **content},
            tok=self.alice[1],
        )["event_id"]

    def unread_by_synapse(self) -> set[str]:
        actions = self.get_success(
            self.store.get_unread_push_actions_for_user_in_range_for_http(
                self.bob[0], 0, self.internals.max_stream_ordering(), 100
            )
        )
        return {a.event_id for a in actions}

    def test_push_action_flags(self) -> None:
        plain = self.send("hello")
        mention = self.send("hey bob", **{"m.mentions": {"user_ids": [self.bob[0]]}})
        action = self.get_success(self.internals.push_action(self.bob[0], self.room, plain))
        self.assertEqual(
            (action.notif, action.highlight, action.sound, action.thread_id),
            (True, False, False, "main"),
        )
        action = self.get_success(self.internals.push_action(self.bob[0], self.room, mention))
        self.assertEqual((action.highlight, action.sound), (True, True))
        self.assertIsNone(
            self.get_success(self.internals.push_action(self.carol[0], "!other:test", plain))
        )
        self.assertIsNone(
            self.get_success(self.internals.push_action(self.alice[0], self.room, plain))
        )

    def test_read_points_agree_with_synapse_thread_rule(self) -> None:
        first = self.send("one")
        root = self.send("root")
        reply = self.send(
            "in thread", **{"m.relates_to": {"rel_type": "m.thread", "event_id": root}}
        )
        last = self.send("two")
        self.receipt(self.room, reply, self.bob[1], thread_id=root)
        self.receipt(self.room, first, self.bob[1])
        point = self.get_success(self.internals.read_points(self.bob[0], [self.room]))[self.room]
        first_event = self.get_success(self.internals.get_event(first))
        self.assertEqual(point.unthreaded_ts, first_event.origin_server_ts)
        unread = self.unread_by_synapse()
        for event_id in (first, root, reply, last):
            action = self.get_success(self.internals.push_action(self.bob[0], self.room, event_id))
            self.assertEqual(
                point.is_read(action.stream_ordering, action.thread_id),
                event_id not in unread,
                event_id,
            )
        self.assertEqual(unread, {root, last})

    def test_rotation_drops_read_rows_but_keeps_highlights(self) -> None:
        plain = self.send("hello")
        mention = self.send("hey", **{"m.mentions": {"user_ids": [self.bob[0]]}})
        self.receipt(self.room, mention, self.bob[1])
        self.reactor.advance(65)
        self.assertIsNone(
            self.get_success(self.internals.push_action(self.bob[0], self.room, plain))
        )
        self.assertIsNotNone(
            self.get_success(self.internals.push_action(self.bob[0], self.room, mention))
        )

    def test_serialize_invite_and_redaction(self) -> None:
        dave = self.user("dave")
        self.helper.invite(self.room, self.alice[0], dave[0], tok=self.alice[1])
        invite_id = self.get_success(
            self.api.get_room_state(self.room, [("m.room.member", dave[0])])
        )[("m.room.member", dave[0])].event_id
        action = self.get_success(self.internals.push_action(dave[0], self.room, invite_id))
        self.assertTrue(action.notif)
        invite = self.internals.serialize_event(
            self.get_success(self.internals.get_event(invite_id))
        )
        self.assertEqual((invite["type"], invite["state_key"]), ("m.room.member", dave[0]))
        self.assertIn("m.room.create", {s["type"] for s in invite["invite_room_state"]})
        message = self.send("secret")
        self.helper.send_event(
            self.room, "m.room.redaction", {"redacts": message}, tok=self.alice[1]
        )
        redacted = self.internals.serialize_event(
            self.get_success(self.internals.get_event(message))
        )
        self.assertEqual(
            (redacted["redacted"], redacted["content"], redacted["state_key"]), (True, {}, None)
        )
        self.assertIsNone(redacted["invite_room_state"])

    def test_catch_up_queries(self) -> None:
        floor = self.internals.max_stream_ordering()
        receipt_floor = self.get_success(self.internals.user_receipt_max(self.bob[0]))
        one = self.send("one")
        two = self.send("two")
        rows = self.get_success(
            self.internals.notif_rows_after(
                self.bob[0], floor, self.internals.max_stream_ordering(), 10
            )
        )
        self.assertEqual([r.event_id for r in rows], [one, two])
        self.assertTrue(all(r.received_ts > 0 for r in rows))
        self.receipt(self.room, one, self.bob[1])
        receipts = self.get_success(self.internals.receipts_after(self.bob[0], receipt_floor, 10))
        self.assertEqual([r.room_id for r in receipts], [self.room])
        self.assertEqual(
            receipts[0].event_ts, self.get_success(self.internals.get_event(one)).origin_server_ts
        )
        self.assertEqual(
            self.get_success(self.internals.user_receipt_max(self.bob[0])), receipts[0].stream_id
        )

    def test_every_probe_passes_on_this_synapse(self) -> None:
        results = self.get_success(run_probes(self.internals.probes()))
        self.assertEqual(results, {f: True for f in ("ring", "health", "test", "nse", "catchup")})

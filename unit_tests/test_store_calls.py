from twisted.trial import unittest

from unit_tests.fakes import FakeModuleApi, run
from zuno_push.store import CallRow, PushStore, RingRow

KEY = "A" * 43 + "="
CALL = CallRow("!r:test", "c1", "@alice:test", "video", 1_000, "Alice", "Room")


class CallStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeModuleApi()
        self.store = PushStore(self.api)

    def test_only_the_first_insert_writes_rings(self) -> None:
        rings = [
            RingRow("@bob:test", "B", "pending", True),
            RingRow("@carol:test", "C", "suppressed:live", False),
        ]
        self.assertTrue(run(self.store.create_call_with_rings(CALL, rings, 2_000)))
        self.assertFalse(
            run(
                self.store.create_call_with_rings(
                    CALL, [RingRow("@dan:test", "D", "pending", False)], 3_000
                )
            )
        )
        self.assertTrue(run(self.store.call_exists("!r:test", "c1")))
        self.assertEqual(
            run(self.store.ring_and_call("!r:test", "c1", "@bob:test", "B")),
            (("pending", 2_000), True),
        )
        self.assertEqual(
            run(self.store.ring_and_call("!r:test", "c1", "@dan:test", "D")), (None, True)
        )
        self.assertEqual(
            run(self.store.ring_and_call("!r:test", "c2", "@dan:test", "D")), (None, False)
        )

    def test_pending_rings_join_the_device_registry(self) -> None:
        run(self.store.upsert_voip("@bob:test", "B", "im.zuno.chat.ios.voip", "pk", 7, KEY))
        run(
            self.store.create_call_with_rings(
                CALL,
                [
                    RingRow("@bob:test", "B", "pending", True),
                    RingRow("@carol:test", "C", "pending", False),
                ],
                2_000,
            )
        )
        rings = run(self.store.pending_rings(received_after=0))
        self.assertEqual(
            [(r.user_id, r.pushkey, r.kid, r.dm) for r in rings],
            [("@bob:test", "pk", 7, True), ("@carol:test", None, None, False)],
        )
        self.assertEqual(
            (rings[0].cname, rings[0].rname, rings[0].kind, rings[0].received_ts),
            ("Alice", "Room", "video", 1_000),
        )
        self.assertEqual(run(self.store.pending_rings(received_after=1_001)), [])

    def test_ring_state_and_expiry(self) -> None:
        run(
            self.store.create_call_with_rings(
                CALL, [RingRow("@bob:test", "B", "pending", False)], 2_000
            )
        )
        self.assertEqual(run(self.store.expire_pending(received_before=1_000, now=5_000)), 0)
        self.assertEqual(run(self.store.expire_pending(received_before=1_001, now=5_000)), 1)
        self.assertEqual(
            run(self.store.ring_and_call("!r:test", "c1", "@bob:test", "B"))[0], ("failed", 5_000)
        )
        run(self.store.set_ring_state("!r:test", "c1", "@bob:test", "B", "sent", 6_000))
        self.assertEqual(
            run(self.store.ring_and_call("!r:test", "c1", "@bob:test", "B"))[0], ("sent", 6_000)
        )

    def test_touch_and_end(self) -> None:
        run(self.store.create_call_with_rings(CALL, [], 2_000))
        run(
            self.store.create_call_with_rings(
                CallRow("!r:test", "c2", "@alice:test", "voice", 1_500, "", ""), [], 2_000
            )
        )
        run(self.store.touch_call("!r:test", "c1", 9_000))
        run(self.store.end_calls("!r:test", "@alice:test", keep=["c2"]))
        rows = self.api.conn.execute(
            "SELECT call_id, last_seen_ts, ended FROM zuno_push_calls ORDER BY call_id"
        ).fetchall()
        self.assertEqual(rows, [("c1", 9_000, 1), ("c2", 2_000, 0)])

    def endings(self) -> list[tuple]:
        return self.api.conn.execute(
            "SELECT user_id, device_id, ending FROM zuno_push_rings ORDER BY user_id, device_id"
        ).fetchall()

    def test_an_answer_then_the_hang_up_mark_each_device_once(self) -> None:
        rings = [
            RingRow("@bob:test", "B1", "sent", False),
            RingRow("@bob:test", "B2", "pending", False),
            RingRow("@bob:test", "B3", "sent", False),
            RingRow("@carol:test", "C", "sent", False),
        ]
        run(self.store.create_call_with_rings(CALL, rings, 2_000))
        self.assertEqual(run(self.store.answer_call("!r:test", "c1", "@bob:test", ["B3"])), 2)
        self.assertEqual(run(self.store.answer_call("!r:test", "c1", "@bob:test", ["B3"])), 0)
        self.assertEqual(run(self.store.end_calls("!r:test", "@alice:test", [])), 1)
        self.assertEqual(run(self.store.end_calls("!r:test", "@alice:test", [])), 0)
        self.assertEqual(
            self.endings(),
            [
                ("@bob:test", "B1", "answered_elsewhere"),
                ("@bob:test", "B2", "answered_elsewhere"),
                ("@bob:test", "B3", "joined"),
                ("@carol:test", "C", "hung_up"),
            ],
        )

    def test_pending_ends_are_sent_fcm_rings_inside_the_window(self) -> None:
        android = ("im.zuno.chat.android",)
        run(self.store.upsert_voip("@bob:test", "B", "im.zuno.chat.android", "fcm-b", 7, KEY))
        run(self.store.upsert_voip("@ios:test", "I", "im.zuno.chat.ios.voip", "pk-i", 7, KEY))
        run(self.store.upsert_voip("@new:test", "N", "im.zuno.chat.android", "fcm-n", 7, KEY))
        run(self.store.upsert_voip("@ans:test", "A", "im.zuno.chat.android", "fcm-a", 7, KEY))
        rings = [
            RingRow("@bob:test", "B", "sent", False),
            RingRow("@ios:test", "I", "sent", False),
            RingRow("@new:test", "N", "pending", False),
            RingRow("@gone:test", "G", "sent", False),
            RingRow("@ans:test", "A", "sent", False),
        ]
        run(self.store.create_call_with_rings(CALL, rings, 2_000))
        self.assertEqual(run(self.store.pending_ends(0, android)), [])
        run(self.store.answer_call("!r:test", "c1", "@ans:test", ["A"]))
        self.assertEqual(run(self.store.pending_ends(0, android)), [])
        run(self.store.end_calls("!r:test", "@alice:test", []))
        ends = run(self.store.pending_ends(0, android))
        self.assertEqual(
            [(e.user_id, e.reason, e.pushkey, e.kid, e.received_ts) for e in ends],
            [("@bob:test", "hung_up", "fcm-b", 7, 1_000)],
        )
        self.assertEqual(run(self.store.pending_ends(1_001, android)), [])
        self.assertEqual(run(self.store.pending_ends(0, ())), [])
        run(self.store.set_ending("!r:test", "c1", "@bob:test", "B", "sent"))
        self.assertEqual(run(self.store.pending_ends(0, android)), [])

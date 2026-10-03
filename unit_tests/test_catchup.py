from twisted.internet import task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals, add_event
from unit_tests.fakes import FakeEvent, FakeModuleApi, run, state_event
from zuno_push.catchup import CatchUp
from zuno_push.nse_scope import NseScope
from zuno_push.records import NotifRow, ReadPoint, ReceiptRow
from zuno_push.store import PushStore

NOW = 1_790_000_000_000
ROOM = "!room:test"
OTHER = "!other:test"
DAY = 86_400_000


class CatchUpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(NOW / 1000)
        self.api = FakeModuleApi(self.clock)
        self.internals = FakeInternals(self.api)
        self.internals.max_so = 100
        self.internals.receipt_max["@bob:test"] = 40
        self.store = PushStore(self.api)
        self.catchup = CatchUp(
            self.api, self.internals, self.store, NseScope(self.api, self.internals)
        )
        run(self.store.set_credential("@bob:test", "PHONE", "h", NOW + DAY, NOW, 100, 40))
        self.api.room_state[ROOM] = {
            ("m.room.name", ""): state_event("m.room.name", "", {"name": "Design team"}, ROOM),
            ("m.room.member", "@alice:test"): state_event(
                "m.room.member", "@alice:test", {"displayname": "Alice"}, ROOM
            ),
            ("m.room.member", "@zed:remote"): state_event(
                "m.room.member", "@zed:remote", {"displayname": "Zed"}, ROOM
            ),
        }

    def message(self, event_id: str, so: int, sender: str = "@alice:test", age: int = 1000) -> None:
        event = FakeEvent(
            type="m.room.encrypted",
            sender=sender,
            content={},
            room_id=ROOM,
            event_id=event_id,
            origin_server_ts=NOW - age,
        )
        add_event(self.internals, event, NOW - age)
        self.internals.notif_rows.append(
            NotifRow(event_id, ROOM, "main", so, False, True, NOW - age)
        )
        self.internals.max_so = max(self.internals.max_so, so)

    def extras(self, fetched: str = "$x") -> dict:
        return run(self.catchup.extras("@bob:test", "PHONE", fetched, NOW))

    def floors(self) -> tuple:
        row = run(self.store.get_device("@bob:test", "PHONE"))
        return row.floor_so, row.floor_rs

    def test_missed_lists_unread_events_after_the_floor_and_advances_it(self) -> None:
        self.message("$old", 90)
        self.message("$a", 101)
        self.message("$b", 102, sender="@zed:remote")
        self.message("$fetched", 103)
        missed = self.extras("$fetched")["missed"]
        self.assertEqual([m["event_id"] for m in missed], ["$a", "$b"])
        first, second = missed
        self.assertEqual(
            (first["sender_name"], first["room_name"], first["is_dm"], first["sound"]),
            ("Alice", "Design team", False, True),
        )
        self.assertEqual(first["event"]["event_id"], "$a")
        self.assertNotIn("sender_name", second)
        self.assertEqual(self.floors()[0], 103)
        self.assertEqual(self.extras("$fetched")["missed"], [])

    def test_missed_skips_read_and_stale_events(self) -> None:
        self.message("$read", 101)
        self.message("$stale", 102, age=DAY + 1)
        self.message("$unread", 103)
        self.internals.points[ROOM] = ReadPoint(unthreaded_so=101)
        self.assertEqual([m["event_id"] for m in self.extras()["missed"]], ["$unread"])

    def test_at_most_twenty_and_the_rest_come_next_time(self) -> None:
        for i in range(25):
            self.message(f"$m{i:02}", 101 + i)
        self.assertEqual(len(self.extras()["missed"]), 20)
        self.assertEqual(self.floors()[0], 120)
        self.assertEqual(
            [m["event_id"] for m in self.extras()["missed"]], [f"$m{i:02}" for i in range(20, 25)]
        )

    def test_read_rooms_since_the_floor(self) -> None:
        self.internals.receipts = [
            ReceiptRow(ROOM, 39, NOW - 50),
            ReceiptRow(ROOM, 41, NOW - 20),
            ReceiptRow(OTHER, 42, NOW - 10),
            ReceiptRow(ROOM, 43, NOW - 5),
        ]
        self.assertEqual(
            self.extras()["read_rooms"],
            [{"room_id": ROOM, "receipt_ts": NOW - 5}, {"room_id": OTHER, "receipt_ts": NOW - 10}],
        )
        self.assertEqual(self.floors()[1], 43)
        self.assertEqual(self.extras()["read_rooms"], [])

    def test_read_rooms_are_capped_at_fifty(self) -> None:
        self.internals.receipts = [ReceiptRow(f"!r{i:02}:test", 41 + i, NOW) for i in range(60)]
        self.assertEqual(len(self.extras()["read_rooms"]), 50)
        self.assertEqual(self.floors()[1], 90)

    def test_device_without_floors_starts_at_now(self) -> None:
        self.api.conn.execute("UPDATE zuno_push_devices SET floor_so = NULL, floor_rs = NULL")
        self.api.conn.commit()
        self.message("$a", 101)
        self.assertEqual(self.extras(), {"missed": [], "read_rooms": []})
        self.assertEqual(self.floors(), (101, 40))

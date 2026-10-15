from twisted.internet import task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals
from unit_tests.fakes import FakeModuleApi, run
from zuno_push.janitor import DAY_MS, Janitor
from zuno_push.store import CallRow, PushStore, RingRow

KEY = "A" * 43 + "="
CALL = CallRow("!r:test", "c1", "@alice:test", "video", 1_000, "Alice", "Room")


class JanitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(10 * DAY_MS / 1000)
        self.api = FakeModuleApi(self.clock)
        self.internals = FakeInternals(self.api)
        self.store = PushStore(self.api)
        self.janitor = Janitor(self.api, self.internals, self.store)

    def test_rows_for_devices_that_no_longer_exist_are_removed(self) -> None:
        self.internals.devices.add(("@a:test", "KEEP"))
        run(self.store.upsert_voip("@a:test", "KEEP", "app", "pk1", 1, KEY))
        run(self.store.upsert_voip("@a:test", "GONE", "app", "pk2", 1, KEY))
        run(self.janitor.run())
        self.assertIsNotNone(run(self.store.get_device("@a:test", "KEEP")))
        self.assertIsNone(run(self.store.get_device("@a:test", "GONE")))

    def test_expired_rate_limit_windows_are_removed(self) -> None:
        self.api.conn.execute(
            "INSERT INTO zuno_push_counters VALUES ('fetch:x', 0, 3, 60000), ('fetch:y', 0, 3, ?)",
            (self.api.get_current_time_msec() + 1,),
        )
        self.api.conn.commit()
        run(self.janitor.run())
        keys = [r[0] for r in self.api.conn.execute("SELECT counter_key FROM zuno_push_counters")]
        self.assertEqual(keys, ["fetch:y"])

    def test_janitor_never_raises(self) -> None:
        self.api.conn.execute("DROP TABLE zuno_push_rings")
        run(self.janitor.run())

    def test_sweep_expires_calls_rings_and_credentials(self) -> None:
        day = 86_400_000
        now = 10 * day
        run(
            self.store.create_call_with_rings(
                CALL, [RingRow("@bob:test", "B", "sent", False)], now - day - 1
            )
        )
        run(self.store.set_credential("@carol:test", "C", "h", now - 1, now - 100, None, None))
        run(self.store.upsert_voip("@dan:test", "D", "app", "pk2", 1, KEY))
        run(self.store.janitor_sweep(now, day))
        self.assertFalse(run(self.store.call_exists("!r:test", "c1")))
        self.assertEqual(
            self.api.conn.execute("SELECT COUNT(*) FROM zuno_push_rings").fetchone()[0], 0
        )
        self.assertIsNone(run(self.store.get_device("@carol:test", "C")))
        self.assertTrue(run(self.store.get_device("@dan:test", "D")).has_voip)

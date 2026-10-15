from twisted.trial import unittest

from unit_tests.fakes import FakeModuleApi, run
from zuno_push.store import PREVIOUS_CREDENTIAL_MS, PushStore

KEY = "A" * 43 + "="


class DeviceRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeModuleApi()
        self.store = PushStore(self.api)

    def test_upsert_then_read(self) -> None:
        run(self.store.upsert_voip("@a:test", "A", "im.zuno.chat.ios.voip", "pk1", 7, KEY))
        row = run(self.store.get_device("@a:test", "A"))
        self.assertTrue(row.has_voip)
        self.assertEqual((row.voip_pushkey, row.kid, row.voip_key), ("pk1", 7, KEY))

    def test_same_pushkey_moves_to_the_new_device(self) -> None:
        run(self.store.upsert_voip("@a:test", "OLD", "app", "pk", 1, KEY))
        run(self.store.set_credential("@a:test", "OLD", "h1", 10_000, 0, None, None))
        run(self.store.upsert_voip("@a:test", "NEW", "app", "pk", 2, KEY))
        old = run(self.store.get_device("@a:test", "OLD"))
        self.assertFalse(old.has_voip)
        self.assertEqual(old.cred_hash, "h1")
        self.assertEqual(run(self.store.get_device("@a:test", "NEW")).kid, 2)

    def test_send_results(self) -> None:
        run(self.store.upsert_voip("@a:test", "A", "app", "pk", 1, KEY))
        for _ in range(5):
            run(self.store.record_send("@a:test", "A", "pk", "error", 9))
        row = run(self.store.get_device("@a:test", "A"))
        self.assertEqual((row.has_voip, row.last_result, row.last_ts), (True, "failed", 9))
        run(self.store.record_send("@a:test", "A", "pk", "sent", 12))
        row = run(self.store.get_device("@a:test", "A"))
        self.assertEqual((row.last_result, row.last_ts), ("sent", 12))
        run(self.store.record_send("@a:test", "A", "pk", "rejected", 15))
        row = run(self.store.get_device("@a:test", "A"))
        self.assertEqual((row.has_voip, row.last_result), (False, "rejected"))

    def test_send_result_for_a_replaced_token_is_ignored(self) -> None:
        run(self.store.upsert_voip("@a:test", "A", "app", "new", 1, KEY))
        run(self.store.record_send("@a:test", "A", "old", "rejected", 5))
        self.assertTrue(run(self.store.get_device("@a:test", "A")).has_voip)

    def test_clear_and_delete(self) -> None:
        run(self.store.upsert_voip("@a:test", "A", "app", "pk", 1, KEY))
        run(self.store.set_credential("@a:test", "A", "h", 10_000, 0, 3, 4))
        run(self.store.clear_voip("@a:test", "A"))
        row = run(self.store.get_device("@a:test", "A"))
        self.assertEqual((row.has_voip, row.cred_hash), (False, "h"))
        run(self.store.delete_device("@a:test", "A"))
        self.assertIsNone(run(self.store.get_device("@a:test", "A")))

    def test_delete_user(self) -> None:
        run(self.store.upsert_voip("@a:test", "A", "app", "pk1", 1, KEY))
        run(self.store.upsert_voip("@a:test", "B", "app", "pk2", 1, KEY))
        run(self.store.upsert_voip("@b:test", "C", "app", "pk3", 1, KEY))
        run(self.store.delete_user("@a:test"))
        self.assertEqual(
            [d.user_id for d in run(self.store.voip_devices_for_users(["@a:test", "@b:test"]))],
            ["@b:test"],
        )

    def test_credential_rotation_keeps_previous_for_ten_minutes(self) -> None:
        run(self.store.set_credential("@a:test", "A", "h1", 1_000_000, 0, 11, 22))
        run(self.store.set_credential("@a:test", "A", "h2", 2_000_000, 100, 99, 99))
        row = run(self.store.get_device("@a:test", "A"))
        self.assertEqual((row.cred_hash, row.prev_cred_hash), ("h2", "h1"))
        self.assertEqual(row.prev_cred_until, 100 + PREVIOUS_CREDENTIAL_MS)
        self.assertEqual((row.floor_so, row.floor_rs), (11, 22))
        self.assertEqual(run(self.store.find_credential("h1", 200)), ("@a:test", "A"))
        self.assertIsNone(run(self.store.find_credential("h1", 100 + PREVIOUS_CREDENTIAL_MS)))
        self.assertEqual(run(self.store.find_credential("h2", 1_999_999)), ("@a:test", "A"))
        self.assertIsNone(run(self.store.find_credential("h2", 2_000_000)))

    def test_expired_credential_is_not_kept_as_previous(self) -> None:
        run(self.store.set_credential("@a:test", "A", "h1", 50, 0, None, None))
        run(self.store.set_credential("@a:test", "A", "h2", 1_000, 60, None, None))
        self.assertIsNone(run(self.store.get_device("@a:test", "A")).prev_cred_hash)

    def test_floors_only_move_forward(self) -> None:
        run(self.store.set_credential("@a:test", "A", "h", 1_000, 0, 10, 10))
        run(self.store.advance_floors("@a:test", "A", 5, 20))
        row = run(self.store.get_device("@a:test", "A"))
        self.assertEqual((row.floor_so, row.floor_rs), (10, 20))

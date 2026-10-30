import sqlite3

from twisted.trial import unittest

from unit_tests.fakes import FakeModuleApi, run
from zuno_push.schema import MIGRATIONS, SCHEMA_VERSION, SchemaTooNew, migrate, migrate_txn

TABLES = {
    "zuno_push_schema",
    "zuno_push_devices",
    "zuno_push_calls",
    "zuno_push_rings",
    "zuno_push_counters",
}


class SchemaTests(unittest.TestCase):
    def test_fresh_database_gets_every_table(self) -> None:
        api = FakeModuleApi(migrated=False)
        self.assertEqual(run(migrate(api)), SCHEMA_VERSION)
        names = {
            r[0] for r in api.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertEqual({n for n in names if n.startswith("zuno_push_")}, TABLES)

    def test_migration_is_idempotent_and_keeps_one_version_row(self) -> None:
        api = FakeModuleApi(migrated=False)
        run(migrate(api))
        api.conn.execute(
            "INSERT INTO zuno_push_counters (counter_key, window_start, hits, expires_ts)"
            " VALUES ('k', 0, 7, 1)"
        )
        api.conn.commit()
        run(migrate(api))
        self.assertEqual(api.conn.execute("SELECT COUNT(*) FROM zuno_push_schema").fetchone()[0], 1)
        self.assertEqual(api.conn.execute("SELECT hits FROM zuno_push_counters").fetchone()[0], 7)

    def test_a_version_1_database_keeps_its_rings_and_gains_the_ending_column(self) -> None:
        api = FakeModuleApi(migrated=False)
        for statement in MIGRATIONS[1]:
            api.conn.execute(statement)
        api.conn.execute("CREATE TABLE zuno_push_schema (lock TEXT PRIMARY KEY, version INTEGER)")
        api.conn.execute("INSERT INTO zuno_push_schema VALUES ('X', 1)")
        api.conn.execute(
            "INSERT INTO zuno_push_rings (room_id, call_id, user_id, device_id, state, ts)"
            " VALUES ('!r:test', 'c1', '@b:test', 'B', 'sent', 1)"
        )
        api.conn.commit()
        self.assertEqual(run(migrate(api)), 2)
        rows = api.conn.execute("SELECT state, ending FROM zuno_push_rings").fetchall()
        self.assertEqual(rows, [("sent", None)])

    def test_newer_schema_is_refused(self) -> None:
        api = FakeModuleApi()
        api.conn.execute("UPDATE zuno_push_schema SET version = ?", (SCHEMA_VERSION + 1,))
        api.conn.commit()
        with self.assertRaises(SchemaTooNew):
            run(api.run_db_interaction("m", migrate_txn))

    def test_pushkey_is_unique_per_app_id(self) -> None:
        api = FakeModuleApi()
        api.conn.execute(
            "INSERT INTO zuno_push_devices (user_id, device_id, voip_app_id, voip_pushkey)"
            " VALUES ('@a:test', 'A', 'app', 'key')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            api.conn.execute(
                "INSERT INTO zuno_push_devices (user_id, device_id, voip_app_id, voip_pushkey)"
                " VALUES ('@b:test', 'B', 'app', 'key')"
            )

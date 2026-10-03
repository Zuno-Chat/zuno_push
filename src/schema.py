from __future__ import annotations

from typing import Any

from synapse.module_api import ModuleApi

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS zuno_push_devices (
            user_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            voip_app_id TEXT,
            voip_pushkey TEXT,
            kid BIGINT,
            voip_key TEXT,
            fail_count INTEGER NOT NULL DEFAULT 0,
            first_fail_ts BIGINT,
            last_result TEXT,
            last_ts BIGINT,
            cred_hash TEXT,
            cred_expires BIGINT,
            prev_cred_hash TEXT,
            prev_cred_until BIGINT,
            last_fetch_ts BIGINT,
            floor_so BIGINT,
            floor_rs BIGINT,
            PRIMARY KEY (user_id, device_id)
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS zuno_push_devices_pushkey"
        " ON zuno_push_devices (voip_app_id, voip_pushkey)",
        "CREATE INDEX IF NOT EXISTS zuno_push_devices_cred ON zuno_push_devices (cred_hash)",
        "CREATE INDEX IF NOT EXISTS zuno_push_devices_prev_cred"
        " ON zuno_push_devices (prev_cred_hash)",
        """
        CREATE TABLE IF NOT EXISTS zuno_push_calls (
            room_id TEXT NOT NULL,
            call_id TEXT NOT NULL,
            caller TEXT NOT NULL,
            kind TEXT NOT NULL,
            received_ts BIGINT NOT NULL,
            last_seen_ts BIGINT NOT NULL,
            ended SMALLINT NOT NULL DEFAULT 0,
            cname TEXT NOT NULL DEFAULT '',
            rname TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (room_id, call_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS zuno_push_calls_seen ON zuno_push_calls (last_seen_ts)",
        """
        CREATE TABLE IF NOT EXISTS zuno_push_rings (
            room_id TEXT NOT NULL,
            call_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            state TEXT NOT NULL,
            ts BIGINT NOT NULL,
            dm SMALLINT NOT NULL DEFAULT 0,
            PRIMARY KEY (room_id, call_id, user_id, device_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS zuno_push_rings_state ON zuno_push_rings (state, ts)",
        """
        CREATE TABLE IF NOT EXISTS zuno_push_counters (
            counter_key TEXT NOT NULL,
            window_start BIGINT NOT NULL,
            hits INTEGER NOT NULL,
            expires_ts BIGINT NOT NULL,
            PRIMARY KEY (counter_key, window_start)
        )
        """,
    ],
}


class SchemaTooNew(Exception):
    pass


def migrate_txn(txn: Any) -> int:
    txn.execute(
        "CREATE TABLE IF NOT EXISTS zuno_push_schema"
        " (lock TEXT NOT NULL PRIMARY KEY, version INTEGER NOT NULL)"
    )
    txn.execute(
        "INSERT INTO zuno_push_schema (lock, version) VALUES ('X', 0) ON CONFLICT (lock) DO NOTHING"
    )
    txn.execute("SELECT version FROM zuno_push_schema WHERE lock = 'X'")
    current = txn.fetchone()[0]
    if current > SCHEMA_VERSION:
        raise SchemaTooNew(f"database has zuno_push schema {current}, code knows {SCHEMA_VERSION}")
    for version in range(current + 1, SCHEMA_VERSION + 1):
        for statement in MIGRATIONS[version]:
            txn.execute(statement)
        txn.execute("UPDATE zuno_push_schema SET version = ? WHERE lock = 'X'", (version,))
    return SCHEMA_VERSION


async def migrate(api: ModuleApi, attempts: int = 5) -> int:
    for attempt in range(attempts):
        try:
            return await api.run_db_interaction("zuno_push_migrate", migrate_txn)
        except SchemaTooNew:
            raise
        except Exception:
            if attempt == attempts - 1:
                raise
            await api.sleep(1 + attempt)
    raise AssertionError("unreachable")

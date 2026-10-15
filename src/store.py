from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from synapse.module_api import ModuleApi

PREVIOUS_CREDENTIAL_MS = 10 * 60 * 1000

_DEVICE_COLUMNS = (
    "user_id, device_id, voip_app_id, voip_pushkey, kid, voip_key, last_result, last_ts,"
    " cred_hash, cred_expires, prev_cred_hash, prev_cred_until, last_fetch_ts, floor_so,"
    " floor_rs"
)
_CLEAR_VOIP = "voip_app_id = NULL, voip_pushkey = NULL, kid = NULL, voip_key = NULL"


@dataclass(frozen=True)
class DeviceRow:
    user_id: str
    device_id: str
    voip_app_id: str | None
    voip_pushkey: str | None
    kid: int | None
    voip_key: str | None
    last_result: str | None
    last_ts: int | None
    cred_hash: str | None
    cred_expires: int | None
    prev_cred_hash: str | None
    prev_cred_until: int | None
    last_fetch_ts: int | None
    floor_so: int | None
    floor_rs: int | None

    @property
    def has_voip(self) -> bool:
        return self.voip_pushkey is not None


@dataclass(frozen=True)
class CallRow:
    room_id: str
    call_id: str
    caller: str
    kind: str
    received_ts: int
    cname: str
    rname: str


@dataclass(frozen=True)
class RingRow:
    user_id: str
    device_id: str
    state: str
    dm: bool


@dataclass(frozen=True)
class PendingRing:
    room_id: str
    call_id: str
    user_id: str
    device_id: str
    dm: bool
    caller: str
    kind: str
    received_ts: int
    cname: str
    rname: str
    app_id: str | None
    pushkey: str | None
    kid: int | None
    voip_key: str | None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.room_id, self.call_id, self.user_id, self.device_id)


class PushStore:
    def __init__(self, api: ModuleApi) -> None:
        self._api = api

    def _run(self, desc: str, func: Any) -> Any:
        return self._api.run_db_interaction(f"zuno_push_{desc}", func)

    async def get_device(self, user_id: str, device_id: str) -> DeviceRow | None:
        def txn(t: Any) -> DeviceRow | None:
            t.execute(
                f"SELECT {_DEVICE_COLUMNS} FROM zuno_push_devices"
                " WHERE user_id = ? AND device_id = ?",
                (user_id, device_id),
            )
            row = t.fetchone()
            return DeviceRow(*row) if row else None

        return await self._run("get_device", txn)

    async def upsert_voip(
        self, user_id: str, device_id: str, app_id: str, pushkey: str, kid: int, key_b64: str
    ) -> None:
        def txn(t: Any) -> None:
            t.execute(
                f"UPDATE zuno_push_devices SET {_CLEAR_VOIP}"
                " WHERE voip_app_id = ? AND voip_pushkey = ?"
                " AND NOT (user_id = ? AND device_id = ?)",
                (app_id, pushkey, user_id, device_id),
            )
            t.execute(
                "INSERT INTO zuno_push_devices"
                " (user_id, device_id, voip_app_id, voip_pushkey, kid, voip_key)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (user_id, device_id) DO UPDATE SET"
                " voip_app_id = excluded.voip_app_id, voip_pushkey = excluded.voip_pushkey,"
                " kid = excluded.kid, voip_key = excluded.voip_key",
                (user_id, device_id, app_id, pushkey, kid, key_b64),
            )

        await self._run("upsert_voip", txn)

    async def clear_voip(self, user_id: str, device_id: str) -> None:
        def txn(t: Any) -> None:
            t.execute(
                f"UPDATE zuno_push_devices SET {_CLEAR_VOIP} WHERE user_id = ? AND device_id = ?",
                (user_id, device_id),
            )

        await self._run("clear_voip", txn)

    async def delete_device(self, user_id: str, device_id: str) -> None:
        def txn(t: Any) -> None:
            t.execute(
                "DELETE FROM zuno_push_devices WHERE user_id = ? AND device_id = ?",
                (user_id, device_id),
            )

        await self._run("delete_device", txn)

    async def delete_user(self, user_id: str) -> None:
        def txn(t: Any) -> None:
            t.execute("DELETE FROM zuno_push_devices WHERE user_id = ?", (user_id,))

        await self._run("delete_user", txn)

    async def voip_devices_for_users(self, user_ids: Collection[str]) -> list[DeviceRow]:
        users = sorted(set(user_ids))

        def txn(t: Any) -> list[DeviceRow]:
            out: list[DeviceRow] = []
            for start in range(0, len(users), 100):
                chunk = users[start : start + 100]
                marks = ", ".join("?" for _ in chunk)
                t.execute(
                    f"SELECT {_DEVICE_COLUMNS} FROM zuno_push_devices"
                    f" WHERE voip_pushkey IS NOT NULL AND user_id IN ({marks})"
                    " ORDER BY user_id, device_id",
                    tuple(chunk),
                )
                out.extend(DeviceRow(*r) for r in t.fetchall())
            return out

        return await self._run("voip_devices", txn)

    async def record_send(
        self, user_id: str, device_id: str, pushkey: str, result: str, now: int
    ) -> None:
        def txn(t: Any) -> None:
            if result == "rejected":
                t.execute(
                    f"UPDATE zuno_push_devices SET {_CLEAR_VOIP},"
                    " last_result = 'rejected', last_ts = ?"
                    " WHERE user_id = ? AND device_id = ? AND voip_pushkey = ?",
                    (now, user_id, device_id, pushkey),
                )
            else:
                t.execute(
                    "UPDATE zuno_push_devices SET last_result = ?, last_ts = ?"
                    " WHERE user_id = ? AND device_id = ? AND voip_pushkey = ?",
                    ("sent" if result == "sent" else "failed", now, user_id, device_id, pushkey),
                )

        await self._run("record_send", txn)

    async def set_credential(
        self,
        user_id: str,
        device_id: str,
        cred_hash: str,
        expires: int,
        now: int,
        floor_so: int | None,
        floor_rs: int | None,
    ) -> None:
        def txn(t: Any) -> None:
            t.execute(
                "INSERT INTO zuno_push_devices (user_id, device_id)"
                " VALUES (?, ?) ON CONFLICT (user_id, device_id) DO NOTHING",
                (user_id, device_id),
            )
            t.execute(
                "UPDATE zuno_push_devices SET"
                " prev_cred_hash = CASE WHEN cred_hash IS NOT NULL AND cred_expires > ?"
                "   THEN cred_hash ELSE NULL END,"
                " prev_cred_until = CASE WHEN cred_hash IS NOT NULL AND cred_expires > ?"
                "   THEN ? ELSE NULL END,"
                " cred_hash = ?, cred_expires = ?,"
                " floor_so = COALESCE(floor_so, ?), floor_rs = COALESCE(floor_rs, ?)"
                " WHERE user_id = ? AND device_id = ?",
                (
                    now,
                    now,
                    now + PREVIOUS_CREDENTIAL_MS,
                    cred_hash,
                    expires,
                    floor_so,
                    floor_rs,
                    user_id,
                    device_id,
                ),
            )

        await self._run("set_credential", txn)

    async def find_credential(self, cred_hash: str, now: int) -> tuple[str, str] | None:
        def txn(t: Any) -> tuple[str, str] | None:
            t.execute(
                "SELECT user_id, device_id FROM zuno_push_devices"
                " WHERE (cred_hash = ? AND cred_expires > ?)"
                " OR (prev_cred_hash = ? AND prev_cred_until > ?)",
                (cred_hash, now, cred_hash, now),
            )
            row = t.fetchone()
            return (row[0], row[1]) if row else None

        return await self._run("find_credential", txn)

    async def touch_fetch(self, user_id: str, device_id: str, now: int) -> None:
        def txn(t: Any) -> None:
            t.execute(
                "UPDATE zuno_push_devices SET last_fetch_ts = ?"
                " WHERE user_id = ? AND device_id = ?",
                (now, user_id, device_id),
            )

        await self._run("touch_fetch", txn)

    async def advance_floors(
        self, user_id: str, device_id: str, floor_so: int, floor_rs: int
    ) -> None:
        def txn(t: Any) -> None:
            t.execute(
                "UPDATE zuno_push_devices SET"
                " floor_so = CASE WHEN floor_so IS NULL OR floor_so < ? THEN ? ELSE floor_so END,"
                " floor_rs = CASE WHEN floor_rs IS NULL OR floor_rs < ? THEN ? ELSE floor_rs END"
                " WHERE user_id = ? AND device_id = ?",
                (floor_so, floor_so, floor_rs, floor_rs, user_id, device_id),
            )

        await self._run("advance_floors", txn)

    async def call_exists(self, room_id: str, call_id: str) -> bool:
        def txn(t: Any) -> bool:
            t.execute(
                "SELECT 1 FROM zuno_push_calls WHERE room_id = ? AND call_id = ?",
                (room_id, call_id),
            )
            return t.fetchone() is not None

        return await self._run("call_exists", txn)

    async def create_call_with_rings(
        self, call: CallRow, rings: Sequence[RingRow], now: int
    ) -> bool:
        def txn(t: Any) -> bool:
            t.execute(
                "INSERT INTO zuno_push_calls (room_id, call_id, caller, kind, received_ts,"
                " last_seen_ts, ended, cname, rname) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)"
                " ON CONFLICT (room_id, call_id) DO NOTHING",
                (
                    call.room_id,
                    call.call_id,
                    call.caller,
                    call.kind,
                    call.received_ts,
                    now,
                    call.cname,
                    call.rname,
                ),
            )
            if t.rowcount != 1:
                return False
            for ring in rings:
                t.execute(
                    "INSERT INTO zuno_push_rings (room_id, call_id, user_id, device_id,"
                    " state, ts, dm) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        call.room_id,
                        call.call_id,
                        ring.user_id,
                        ring.device_id,
                        ring.state,
                        now,
                        1 if ring.dm else 0,
                    ),
                )
            return True

        return await self._run("create_call", txn)

    async def touch_call(self, room_id: str, call_id: str, now: int) -> None:
        def txn(t: Any) -> None:
            t.execute(
                "UPDATE zuno_push_calls SET last_seen_ts = ? WHERE room_id = ? AND call_id = ?",
                (now, room_id, call_id),
            )

        await self._run("touch_call", txn)

    async def end_calls(self, room_id: str, caller: str, keep: Collection[str]) -> None:
        keep_list = sorted(keep)

        def txn(t: Any) -> None:
            t.execute(
                "SELECT call_id FROM zuno_push_calls"
                " WHERE room_id = ? AND caller = ? AND ended = 0",
                (room_id, caller),
            )
            for (call_id,) in t.fetchall():
                if call_id not in keep_list:
                    t.execute(
                        "UPDATE zuno_push_calls SET ended = 1 WHERE room_id = ? AND call_id = ?",
                        (room_id, call_id),
                    )

        await self._run("end_calls", txn)

    async def expire_pending(self, received_before: int, now: int) -> int:
        def txn(t: Any) -> int:
            t.execute(
                "UPDATE zuno_push_rings SET state = 'failed', ts = ?"
                " WHERE state = 'pending' AND EXISTS (SELECT 1 FROM zuno_push_calls c"
                " WHERE c.room_id = zuno_push_rings.room_id"
                " AND c.call_id = zuno_push_rings.call_id AND c.received_ts < ?)",
                (now, received_before),
            )
            return t.rowcount

        return await self._run("expire_pending", txn)

    async def pending_rings(self, received_after: int, limit: int = 500) -> list[PendingRing]:
        def txn(t: Any) -> list[PendingRing]:
            t.execute(
                "SELECT r.room_id, r.call_id, r.user_id, r.device_id, r.dm, c.caller,"
                " c.kind, c.received_ts, c.cname, c.rname, d.voip_app_id, d.voip_pushkey,"
                " d.kid, d.voip_key"
                " FROM zuno_push_rings r"
                " JOIN zuno_push_calls c ON c.room_id = r.room_id AND c.call_id = r.call_id"
                " LEFT JOIN zuno_push_devices d"
                "   ON d.user_id = r.user_id AND d.device_id = r.device_id"
                " WHERE r.state = 'pending' AND c.ended = 0 AND c.received_ts >= ?"
                " ORDER BY c.received_ts DESC, r.room_id, r.call_id, r.user_id, r.device_id"
                " LIMIT ?",
                (received_after, limit),
            )
            return [
                PendingRing(
                    room_id=row[0],
                    call_id=row[1],
                    user_id=row[2],
                    device_id=row[3],
                    dm=bool(row[4]),
                    caller=row[5],
                    kind=row[6],
                    received_ts=int(row[7]),
                    cname=row[8],
                    rname=row[9],
                    app_id=row[10],
                    pushkey=row[11],
                    kid=row[12],
                    voip_key=row[13],
                )
                for row in t.fetchall()
            ]

        return await self._run("pending_rings", txn)

    async def set_ring_state(
        self, room_id: str, call_id: str, user_id: str, device_id: str, state: str, ts: int
    ) -> None:
        def txn(t: Any) -> None:
            t.execute(
                "UPDATE zuno_push_rings SET state = ?, ts = ?"
                " WHERE room_id = ? AND call_id = ? AND user_id = ? AND device_id = ?",
                (state, ts, room_id, call_id, user_id, device_id),
            )

        await self._run("set_ring_state", txn)

    async def ring_and_call(
        self, room_id: str, call_id: str, user_id: str, device_id: str
    ) -> tuple[tuple[str, int] | None, bool]:
        def txn(t: Any) -> tuple[tuple[str, int] | None, bool]:
            t.execute(
                "SELECT state, ts FROM zuno_push_rings"
                " WHERE room_id = ? AND call_id = ? AND user_id = ? AND device_id = ?",
                (room_id, call_id, user_id, device_id),
            )
            ring = t.fetchone()
            t.execute(
                "SELECT 1 FROM zuno_push_calls WHERE room_id = ? AND call_id = ?",
                (room_id, call_id),
            )
            call_seen = t.fetchone() is not None
            return ((ring[0], int(ring[1])) if ring else None), call_seen

        return await self._run("ring_and_call", txn)

    async def janitor_sweep(self, now: int, day_ms: int) -> None:
        def txn(t: Any) -> None:
            t.execute("DELETE FROM zuno_push_calls WHERE last_seen_ts < ?", (now - day_ms,))
            t.execute("DELETE FROM zuno_push_rings WHERE ts < ?", (now - day_ms,))
            t.execute("DELETE FROM zuno_push_counters WHERE expires_ts < ?", (now,))
            t.execute(
                "UPDATE zuno_push_devices SET cred_hash = NULL, cred_expires = NULL"
                " WHERE cred_expires IS NOT NULL AND cred_expires <= ?",
                (now,),
            )
            t.execute(
                "UPDATE zuno_push_devices SET prev_cred_hash = NULL, prev_cred_until = NULL"
                " WHERE prev_cred_until IS NOT NULL AND prev_cred_until <= ?",
                (now,),
            )
            t.execute(
                "DELETE FROM zuno_push_devices WHERE voip_pushkey IS NULL"
                " AND cred_hash IS NULL AND prev_cred_hash IS NULL"
            )

        await self._run("janitor", txn)

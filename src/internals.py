from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Collection, Mapping
from typing import Any, TypeVar

from synapse.event_auth import get_send_level, get_user_power_level
from synapse.module_api import ModuleApi, make_deferred_yieldable, run_in_background
from synapse.push.bulk_push_rule_evaluator import _flatten_dict
from synapse.synapse_rust.push import PushRuleEvaluator
from synapse.util.async_helpers import timeout_deferred
from synapse.util.frozenutils import unfreeze

from zuno_push.records import (
    Delta,
    EventTimes,
    NotifRow,
    Probe,
    PushAction,
    PusherRow,
    ReadPoint,
    ReceiptRow,
    StateMap,
    has_sound,
)

T = TypeVar("T")

CREATE_KEY = ("m.room.create", "")
POWER_KEY = ("m.room.power_levels", "")
READ_RECEIPTS = ("m.read", "m.read.private")
PROBE_USER = "@zuno_push_probe:invalid"
PROBE_ROOM = "!zuno_push_probe:invalid"
PROBE_EVENT = "$zuno_push_probe"


def _marks(values: Collection[Any]) -> str:
    return ", ".join("?" for _ in values)


class SynapseInternals:
    def __init__(self, api: ModuleApi) -> None:
        self._api = api
        self._hs = api._hs
        self._store = api._store
        self._controllers = api._storage_controllers
        self._clock = self._hs.get_clock()
        self._experimental = self._hs.config.experimental

    async def within(self, seconds: float, fn: Callable[..., Awaitable[T]], *args: Any) -> T:
        deferred = run_in_background(fn, *args)
        deferred = timeout_deferred(deferred=deferred, timeout=seconds, clock=self._clock)
        return await make_deferred_yieldable(deferred)

    def max_stream_ordering(self) -> int:
        return self._store.get_room_max_stream_ordering()

    async def state_deltas(self, from_pos: int, to_pos: int) -> tuple[int, list[Delta]]:
        pos, rows = await self._controllers.state.get_current_state_deltas(from_pos, to_pos)
        return pos, [
            Delta(r.stream_id, r.room_id, r.event_type, r.state_key, r.event_id) for r in rows
        ]

    async def first_stream_ordering_after(self, ts: int) -> int:
        return int(await self._store.find_first_stream_ordering_after_ts(ts))

    async def get_event(self, event_id: str) -> Any:
        return await self._store.get_event(event_id, allow_none=True)

    async def event_times(self, event_id: str) -> EventTimes | None:
        def txn(t: Any) -> EventTimes | None:
            t.execute(
                "SELECT received_ts, origin_server_ts FROM events WHERE event_id = ?",
                (event_id,),
            )
            row = t.fetchone()
            return EventTimes(row[0], row[1]) if row else None

        return await self._api.run_db_interaction("zuno_push_event_times", txn)

    async def device_exists(self, user_id: str, device_id: str) -> bool:
        def txn(t: Any) -> bool:
            t.execute(
                "SELECT 1 FROM devices WHERE user_id = ? AND device_id = ? AND hidden = ?",
                (user_id, device_id, False),
            )
            return t.fetchone() is not None

        return await self._api.run_db_interaction("zuno_push_device_exists", txn)

    async def delete_rows_for_missing_devices(self) -> int:
        def txn(t: Any) -> int:
            t.execute(
                "DELETE FROM zuno_push_devices WHERE NOT EXISTS (SELECT 1 FROM devices d"
                " WHERE d.user_id = zuno_push_devices.user_id"
                " AND d.device_id = zuno_push_devices.device_id)"
            )
            return t.rowcount

        return await self._api.run_db_interaction("zuno_push_missing_devices", txn)

    async def pushers_for_device(self, user_id: str, device_id: str) -> list[PusherRow]:
        def txn(t: Any) -> list[PusherRow]:
            t.execute(
                "SELECT app_id, last_success, failing_since FROM pushers"
                " WHERE user_name = ? AND device_id = ? AND kind = 'http' ORDER BY app_id",
                (user_id, device_id),
            )
            return [PusherRow(r[0], r[1], r[2]) for r in t.fetchall()]

        return await self._api.run_db_interaction("zuno_push_pushers", txn)

    def is_pusher_instance_for(self, user_id: str) -> bool:
        worker = self._hs.config.worker
        return bool(worker.start_pushers) and worker.pusher_shard_config.should_handle(
            self._hs.get_instance_name(), user_id
        )

    def user_level(self, state: StateMap, user_id: str) -> int:
        auth_events = {CREATE_KEY: state[CREATE_KEY]}
        if POWER_KEY in state:
            auth_events[POWER_KEY] = state[POWER_KEY]
        return get_user_power_level(user_id, auth_events)

    def call_member_level(self, state: StateMap) -> int:
        return get_send_level("m.call.member", "", state.get(POWER_KEY))

    async def would_notify(
        self,
        state: StateMap,
        joined_count: int,
        sender_level: int | None,
        event: Mapping[str, Any],
        user_id: str,
        display_name: str | None,
    ) -> bool:
        rules = await self._store.get_push_rules_for_user(user_id)
        create = state[CREATE_KEY]
        power = state.get(POWER_KEY)
        raw_levels = power.content.get("notifications", {}) if power is not None else {}
        levels: dict[str, int] = {}
        if isinstance(raw_levels, Mapping):
            for key, value in raw_levels.items():
                try:
                    levels[key] = int(value)
                except (TypeError, ValueError):
                    continue
        evaluator = self._evaluator(
            event, joined_count, sender_level, levels, create.room_version.msc3931_push_features
        )
        return "notify" in evaluator.run(rules, user_id, display_name, None)

    def _evaluator(
        self,
        event: Mapping[str, Any],
        joined_count: int,
        sender_level: int | None,
        levels: Mapping[str, int],
        room_features: Collection[str],
    ) -> PushRuleEvaluator:
        return PushRuleEvaluator(
            _flatten_dict(event),
            False,
            joined_count,
            sender_level,
            levels,
            {},
            False,
            room_features,
            self._experimental.msc1767_enabled,
            self._experimental.msc4210_enabled,
            self._experimental.msc4306_enabled,
        )

    async def joined_count(self, room_id: str) -> int:
        return int(await self._store.get_number_joined_users_in_room(room_id))

    async def local_joined_users(self, room_id: str) -> list[str]:
        return list(await self._store.get_local_users_in_room(room_id))

    async def push_action(self, user_id: str, room_id: str, event_id: str) -> PushAction | None:
        def txn(t: Any) -> PushAction | None:
            t.execute(
                "SELECT stream_ordering, thread_id, notif, highlight, actions"
                " FROM event_push_actions"
                " WHERE user_id = ? AND room_id = ? AND event_id = ?",
                (user_id, room_id, event_id),
            )
            row = t.fetchone()
            if row is None:
                return None
            highlight = bool(row[3])
            return PushAction(
                int(row[0]), row[1], row[2] == 1, highlight, has_sound(row[4], highlight)
            )

        return await self._api.run_db_interaction("zuno_push_push_action", txn)

    async def read_points(self, user_id: str, room_ids: Collection[str]) -> dict[str, ReadPoint]:
        rooms = sorted(set(room_ids))

        def txn(t: Any) -> dict[str, ReadPoint]:
            unthreaded: dict[str, tuple[int, int]] = {}
            threads: dict[str, dict[str, int]] = {}
            for start in range(0, len(rooms), 100):
                chunk = rooms[start : start + 100]
                t.execute(
                    "SELECT r.room_id, e.stream_ordering, e.origin_server_ts"
                    " FROM receipts_linearized r"
                    " JOIN events e ON e.room_id = r.room_id AND e.event_id = r.event_id"
                    f" WHERE r.user_id = ? AND r.receipt_type IN ({_marks(READ_RECEIPTS)})"
                    f" AND r.thread_id IS NULL AND r.room_id IN ({_marks(chunk)})",
                    (user_id, *READ_RECEIPTS, *chunk),
                )
                for room_id, so, ts in t.fetchall():
                    if so is not None and so > unthreaded.get(room_id, (0, 0))[0]:
                        unthreaded[room_id] = (int(so), int(ts or 0))
                t.execute(
                    "SELECT room_id, thread_id, MAX(event_stream_ordering)"
                    " FROM receipts_linearized"
                    f" WHERE user_id = ? AND receipt_type IN ({_marks(READ_RECEIPTS)})"
                    f" AND thread_id IS NOT NULL AND room_id IN ({_marks(chunk)})"
                    " GROUP BY room_id, thread_id",
                    (user_id, *READ_RECEIPTS, *chunk),
                )
                for room_id, thread_id, so in t.fetchall():
                    if so is not None:
                        threads.setdefault(room_id, {})[thread_id] = int(so)
            points: dict[str, ReadPoint] = {}
            for room_id in rooms:
                so, ts = unthreaded.get(room_id, (0, 0))
                points[room_id] = ReadPoint(so, ts, threads.get(room_id, {}))
            return points

        return await self._api.run_db_interaction("zuno_push_read_points", txn)

    async def notif_rows_after(
        self, user_id: str, after_so: int, upto_so: int, limit: int
    ) -> list[NotifRow]:
        def txn(t: Any) -> list[NotifRow]:
            t.execute(
                "SELECT ep.event_id, ep.room_id, ep.thread_id, ep.stream_ordering,"
                " ep.highlight, ep.actions, e.received_ts, e.origin_server_ts"
                " FROM event_push_actions ep JOIN events e ON e.event_id = ep.event_id"
                " WHERE ep.user_id = ? AND ep.notif = 1"
                " AND ep.stream_ordering > ? AND ep.stream_ordering <= ?"
                " ORDER BY ep.stream_ordering ASC LIMIT ?",
                (user_id, after_so, upto_so, limit),
            )
            rows = []
            for event_id, room_id, thread_id, so, hl, actions, received, origin in t.fetchall():
                highlight = bool(hl)
                rows.append(
                    NotifRow(
                        event_id,
                        room_id,
                        thread_id,
                        int(so),
                        highlight,
                        has_sound(actions, highlight),
                        int(received or origin or 0),
                    )
                )
            return rows

        return await self._api.run_db_interaction("zuno_push_notif_rows", txn)

    async def receipts_after(
        self, user_id: str, after_stream_id: int, limit: int
    ) -> list[ReceiptRow]:
        def txn(t: Any) -> list[ReceiptRow]:
            t.execute(
                "SELECT r.room_id, r.stream_id, e.origin_server_ts FROM receipts_linearized r"
                " LEFT JOIN events e ON e.room_id = r.room_id AND e.event_id = r.event_id"
                f" WHERE r.user_id = ? AND r.receipt_type IN ({_marks(READ_RECEIPTS)})"
                " AND r.thread_id IS NULL AND r.stream_id > ?"
                " ORDER BY r.stream_id ASC LIMIT ?",
                (user_id, *READ_RECEIPTS, after_stream_id, limit),
            )
            return [ReceiptRow(r[0], int(r[1]), int(r[2] or 0)) for r in t.fetchall()]

        return await self._api.run_db_interaction("zuno_push_receipts_after", txn)

    async def user_receipt_max(self, user_id: str) -> int:
        def txn(t: Any) -> int:
            t.execute(
                "SELECT COALESCE(MAX(stream_id), 0) FROM receipts_linearized WHERE user_id = ?",
                (user_id,),
            )
            return int(t.fetchone()[0])

        return await self._api.run_db_interaction("zuno_push_receipt_max", txn)

    def serialize_event(self, event: Any) -> dict[str, Any]:
        content = unfreeze(event.content)
        invite_room_state = None
        if event.type == "m.room.member" and content.get("membership") == "invite":
            raw = event.unsigned.get("invite_room_state")
            invite_room_state = unfreeze(raw) if raw is not None else None
        return {
            "event_id": event.event_id,
            "room_id": event.room_id,
            "type": event.type,
            "sender": event.sender,
            "origin_server_ts": event.origin_server_ts,
            "state_key": event.get_state_key(),
            "redacted": bool(event.internal_metadata.is_redacted()),
            "content": content,
            "invite_room_state": invite_room_state,
        }

    def probes(self) -> dict[str, list[tuple[str, Probe]]]:
        return {
            "ring": [
                ("within", self._probe_within),
                ("state_deltas", self._probe_deltas),
                ("stream_by_time", self._probe_stream_by_time),
                ("event_times", self._probe_event_times),
                ("get_event", self._probe_get_event),
                ("power_levels", self._probe_power),
                ("push_rule_evaluator", self._probe_evaluator),
                ("room_members", self._probe_members),
                ("devices", self._probe_devices),
            ],
            "health": [("pushers", self._probe_pushers)],
            "test": [("pusher_instance", self._probe_pusher_instance)],
            "nse": [
                ("push_actions", self._probe_push_actions),
                ("read_points", self._probe_read_points),
                ("get_event", self._probe_get_event),
                ("devices", self._probe_devices),
            ],
            "catchup": [
                ("notif_rows", self._probe_notif_rows),
                ("receipts_after", self._probe_receipts_after),
            ],
        }

    async def _probe_within(self) -> None:
        async def one() -> int:
            return 1

        if await self.within(1.0, one) != 1:
            raise AssertionError("within returned the wrong value")

    async def _probe_deltas(self) -> None:
        top = self.max_stream_ordering()
        pos, rows = await self.state_deltas(top, top)
        if not isinstance(pos, int) or not isinstance(rows, list):
            raise AssertionError("unexpected get_current_state_deltas shape")

    async def _probe_stream_by_time(self) -> None:
        position = await self.first_stream_ordering_after(self._api.get_current_time_msec())
        if not isinstance(position, int):
            raise AssertionError("find_first_stream_ordering_after_ts returned a non-integer")

    async def _probe_event_times(self) -> None:
        if await self.event_times(PROBE_EVENT) is not None:
            raise AssertionError("probe event unexpectedly exists")

    async def _probe_get_event(self) -> None:
        if await self.get_event(PROBE_EVENT) is not None:
            raise AssertionError("probe event unexpectedly exists")

    async def _probe_power(self) -> None:
        if list(inspect.signature(get_user_power_level).parameters) != ["user_id", "auth_events"]:
            raise AssertionError("get_user_power_level signature changed")
        if get_send_level("m.call.member", "", None) != 50:
            raise AssertionError("get_send_level default changed")

    async def _probe_evaluator(self) -> None:
        rules = await self._store.get_push_rules_for_user(PROBE_USER)
        event = {
            "type": "m.room.encrypted",
            "room_id": "!p:invalid",
            "sender": "@s:invalid",
            "content": {},
        }
        evaluator = self._evaluator(event, 2, 0, {}, [])
        if "notify" not in evaluator.run(rules, PROBE_USER, None, None):
            raise AssertionError("base rules no longer notify an encrypted 1:1 event")

    async def _probe_members(self) -> None:
        await self.local_joined_users(PROBE_ROOM)
        await self.joined_count(PROBE_ROOM)

    async def _probe_devices(self) -> None:
        if await self.device_exists(PROBE_USER, "PROBE"):
            raise AssertionError("probe device unexpectedly exists")

    async def _probe_pushers(self) -> None:
        await self.pushers_for_device(PROBE_USER, "PROBE")

    async def _probe_pusher_instance(self) -> None:
        self.is_pusher_instance_for(PROBE_USER)

    async def _probe_push_actions(self) -> None:
        await self.push_action(PROBE_USER, PROBE_ROOM, PROBE_EVENT)

    async def _probe_read_points(self) -> None:
        await self.read_points(PROBE_USER, [PROBE_ROOM])

    async def _probe_notif_rows(self) -> None:
        await self.notif_rows_after(PROBE_USER, 0, 0, 1)

    async def _probe_receipts_after(self) -> None:
        await self.receipts_after(PROBE_USER, 0, 1)
        await self.user_receipt_max(PROBE_USER)

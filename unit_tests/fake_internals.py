from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from twisted.internet import defer

from unit_tests.fakes import FakeEvent, FakeModuleApi
from zuno_push.records import (
    Delta,
    EventTimes,
    NotifRow,
    Probe,
    PushAction,
    PusherRow,
    ReadPoint,
    ReceiptRow,
)


class FakeInternals:
    def __init__(self, api: FakeModuleApi) -> None:
        self._api = api
        self.max_so = 0
        self.deltas: list[Delta] = []
        self.delta_reads: list[tuple[int, int]] = []
        self.events: dict[str, Any] = {}
        self.times: dict[str, EventTimes] = {}
        self.levels: dict[tuple[str, str], int] = {}
        self.required: dict[str, int] = {}
        self.notify: Callable[[str, str], bool] = lambda room_id, user_id: True
        self.notify_calls: list[tuple[Mapping[str, Any], str, str | None]] = []
        self.members: dict[str, list[str]] = {}
        self.devices: set[tuple[str, str]] = set()
        self.pushers: dict[tuple[str, str], list[PusherRow]] = {}
        self.pusher_instance = True
        self.actions: dict[tuple[str, str, str], PushAction] = {}
        self.points: dict[str, ReadPoint] = {}
        self.notif_rows: list[NotifRow] = []
        self.receipts: list[ReceiptRow] = []
        self.receipt_max: dict[str, int] = {}
        self.fail: set[str] = set()
        self.probe_failures: set[str] = set()

    def _check(self, name: str) -> None:
        if name in self.fail:
            raise RuntimeError(f"{name} failed")

    async def within(self, seconds: float, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        d = defer.ensureDeferred(fn(*args))
        d.addTimeout(seconds, self._api.clock)
        return await d

    def max_stream_ordering(self) -> int:
        return self.max_so

    async def state_deltas(self, from_pos: int, to_pos: int) -> tuple[int, list[Delta]]:
        self._check("state_deltas")
        self.delta_reads.append((from_pos, to_pos))
        rows = [d for d in self.deltas if from_pos < d.stream_id <= to_pos][:100]
        if len(rows) == 100:
            return rows[-1].stream_id, rows
        return to_pos, rows

    async def first_stream_ordering_after(self, ts: int) -> int:
        self._check("first_stream_ordering_after")
        later = [
            d.stream_id
            for d in self.deltas
            if d.event_id in self.times and (self.times[d.event_id].received_ts or 0) >= ts
        ]
        return min(later) if later else self.max_so + 1

    async def get_event(self, event_id: str) -> Any:
        return self.events.get(event_id)

    async def event_times(self, event_id: str) -> EventTimes | None:
        return self.times.get(event_id)

    def user_level(self, state: Any, user_id: str) -> int:
        self._check("user_level")
        room_id = state[("m.room.create", "")].room_id
        return self.levels.get((room_id, user_id), 0)

    def call_member_level(self, state: Any) -> int:
        room_id = state[("m.room.create", "")].room_id
        return self.required.get(room_id, 50)

    async def would_notify(
        self,
        state: Any,
        joined: int,
        sender_level: Any,
        event: Mapping[str, Any],
        user_id: str,
        display_name: str | None,
    ) -> bool:
        self._check("would_notify")
        self.notify_calls.append((event, user_id, display_name))
        return self.notify(event["room_id"], user_id)

    async def joined_count(self, room_id: str) -> int:
        return len(self.members.get(room_id, []))

    async def local_joined_users(self, room_id: str) -> list[str]:
        self._check("local_joined_users")
        return list(self.members.get(room_id, []))

    async def device_exists(self, user_id: str, device_id: str) -> bool:
        self._check("device_exists")
        return (user_id, device_id) in self.devices

    async def delete_rows_for_missing_devices(self) -> int:
        def txn(t: Any) -> int:
            t.execute("SELECT user_id, device_id FROM zuno_push_devices")
            gone = [r for r in t.fetchall() if (r[0], r[1]) not in self.devices]
            for user_id, device_id in gone:
                t.execute(
                    "DELETE FROM zuno_push_devices WHERE user_id = ? AND device_id = ?",
                    (user_id, device_id),
                )
            return len(gone)

        return await self._api.run_db_interaction("missing", txn)

    async def pushers_for_device(self, user_id: str, device_id: str) -> list[PusherRow]:
        return self.pushers.get((user_id, device_id), [])

    def is_pusher_instance_for(self, user_id: str) -> bool:
        return self.pusher_instance

    async def push_action(self, user_id: str, room_id: str, event_id: str) -> PushAction | None:
        return self.actions.get((user_id, room_id, event_id))

    async def read_points(self, user_id: str, room_ids: Any) -> dict[str, ReadPoint]:
        return {r: self.points.get(r, ReadPoint()) for r in room_ids}

    async def notif_rows_after(
        self, user_id: str, after_so: int, upto_so: int, limit: int
    ) -> list[NotifRow]:
        rows = [r for r in self.notif_rows if after_so < r.stream_ordering <= upto_so]
        return sorted(rows, key=lambda r: r.stream_ordering)[:limit]

    async def receipts_after(self, user_id: str, after: int, limit: int) -> list[ReceiptRow]:
        rows = [r for r in self.receipts if r.stream_id > after]
        return sorted(rows, key=lambda r: r.stream_id)[:limit]

    async def user_receipt_max(self, user_id: str) -> int:
        return self.receipt_max.get(user_id, 0)

    def serialize_event(self, event: Any) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "room_id": event.room_id,
            "type": event.type,
            "sender": event.sender,
            "origin_server_ts": event.origin_server_ts,
            "state_key": event.state_key,
            "redacted": False,
            "content": dict(event.content),
            "invite_room_state": None,
        }

    def probes(self) -> dict[str, list[tuple[str, Probe]]]:
        def probe(name: str) -> Probe:
            async def run() -> None:
                if name in self.probe_failures:
                    raise RuntimeError(f"{name} drifted")

            return run

        return {
            "ring": [("state_deltas", probe("state_deltas"))],
            "health": [("pushers", probe("pushers"))],
            "test": [("pusher_instance", probe("pusher_instance"))],
            "nse": [("push_actions", probe("push_actions"))],
            "catchup": [("notif_rows", probe("notif_rows"))],
        }


def add_event(internals: FakeInternals, event: FakeEvent, received_ts: int) -> None:
    internals.events[event.event_id] = event
    internals.times[event.event_id] = EventTimes(received_ts, received_ts)


def add_delta(internals: FakeInternals, event: FakeEvent, stream_id: int) -> None:
    internals.deltas.append(
        Delta(stream_id, event.room_id, event.type, event.state_key or "", event.event_id)
    )
    internals.max_so = max(internals.max_so, stream_id)

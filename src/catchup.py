from __future__ import annotations

from typing import Any

from synapse.module_api import ModuleApi

from zuno_push.nse_scope import NseScope, RoomInfo
from zuno_push.records import Internals
from zuno_push.store import PushStore

MISSED_MAX = 20
MISSED_SCAN = 200
MISSED_AGE_MS = 24 * 3_600_000
READ_ROOMS_MAX = 50
RECEIPT_SCAN = 100


class CatchUp:
    def __init__(
        self, api: ModuleApi, internals: Internals, store: PushStore, scope: NseScope
    ) -> None:
        self._api = api
        self._internals = internals
        self._store = store
        self._scope = scope

    async def extras(
        self, user_id: str, device_id: str, fetched_event_id: str, now: int
    ) -> dict[str, Any]:
        device = await self._store.get_device(user_id, device_id)
        top = self._internals.max_stream_ordering()
        if device is None or device.floor_so is None or device.floor_rs is None:
            await self._store.advance_floors(
                user_id, device_id, top, await self._internals.user_receipt_max(user_id)
            )
            return {"missed": [], "read_rooms": []}
        missed, floor_so = await self._missed(user_id, device.floor_so, top, fetched_event_id, now)
        read_rooms, floor_rs = await self._read_rooms(user_id, device.floor_rs)
        await self._store.advance_floors(user_id, device_id, floor_so, floor_rs)
        return {"missed": missed, "read_rooms": read_rooms}

    async def _missed(
        self, user_id: str, floor: int, top: int, fetched_event_id: str, now: int
    ) -> tuple[list[dict[str, Any]], int]:
        if floor >= top:
            return [], floor
        rows = await self._internals.notif_rows_after(user_id, floor, top, MISSED_SCAN)
        points = (
            await self._internals.read_points(user_id, {r.room_id for r in rows}) if rows else {}
        )
        picked = []
        new_floor = floor
        complete = len(rows) < MISSED_SCAN
        for row in rows:
            new_floor = row.stream_ordering
            if row.event_id == fetched_event_id or now - row.received_ts > MISSED_AGE_MS:
                continue
            if points[row.room_id].is_read(row.stream_ordering, row.thread_id):
                continue
            picked.append(row)
            if len(picked) == MISSED_MAX:
                complete = False
                break
        if complete:
            new_floor = top
        rooms: dict[str, RoomInfo] = {}
        items: list[dict[str, Any]] = []
        for row in picked:
            event = await self._internals.get_event(row.event_id)
            if event is None or event.room_id != row.room_id:
                continue
            if row.room_id not in rooms:
                rooms[row.room_id] = await self._scope.room_info(user_id, row.room_id)
            info = rooms[row.room_id]
            item: dict[str, Any] = {
                "room_id": row.room_id,
                "event_id": row.event_id,
                "event": self._internals.serialize_event(event),
                "is_dm": info.is_dm,
                "highlight": row.highlight,
                "sound": row.sound,
            }
            if info.room_name:
                item["room_name"] = info.room_name
            if self._api.is_mine(event.sender):
                sender_name = await self._scope.sender_name(row.room_id, event.sender)
                if sender_name:
                    item["sender_name"] = sender_name
            items.append(item)
        return items, new_floor

    async def _read_rooms(self, user_id: str, floor: int) -> tuple[list[dict[str, Any]], int]:
        rows = await self._internals.receipts_after(user_id, floor, RECEIPT_SCAN)
        rooms: dict[str, int] = {}
        new_floor = floor
        for row in rows:
            if row.room_id not in rooms and len(rooms) == READ_ROOMS_MAX:
                break
            rooms[row.room_id] = max(rooms.get(row.room_id, 0), row.event_ts)
            new_floor = row.stream_id
        return [{"room_id": r, "receipt_ts": ts} for r, ts in rooms.items()], new_floor

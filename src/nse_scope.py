from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from synapse.module_api import ModuleApi

from zuno_push import metrics
from zuno_push.names import is_direct, raw_name
from zuno_push.records import Internals

logger = logging.getLogger("zuno_push")

OK_AGE_MS = 24 * 3_600_000
HIGHLIGHT_AGE_MS = 72 * 3_600_000
NAME_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class RoomInfo:
    room_name: str | None
    is_dm: bool


def _gone(reason: str) -> dict[str, Any]:
    metrics.FETCHES.labels(status="gone", reason=reason).inc()
    return {"status": "gone"}


class NseScope:
    def __init__(self, api: ModuleApi, internals: Internals) -> None:
        self._api = api
        self._internals = internals

    async def fetch(self, user_id: str, room_id: str, event_id: str, now: int) -> dict[str, Any]:
        action = await self._internals.push_action(user_id, room_id, event_id)
        if action is None or not action.notif:
            return _gone("no_row")
        times = await self._internals.event_times(event_id)
        received = (times.received_ts or times.origin_server_ts) if times else None
        if received is None:
            return _gone("no_event")
        max_age = HIGHLIGHT_AGE_MS if action.highlight else OK_AGE_MS
        if now - int(received) > max_age:
            return _gone("too_old")
        point = (await self._internals.read_points(user_id, [room_id]))[room_id]
        if point.is_read(action.stream_ordering, action.thread_id):
            metrics.FETCHES.labels(status="read", reason="").inc()
            return {"status": "read", "receipt_ts": point.unthreaded_ts}
        event = await self._internals.get_event(event_id)
        if event is None or event.room_id != room_id:
            return _gone("no_event")
        info = await self.room_info(user_id, room_id)
        reply: dict[str, Any] = {
            "status": "ok",
            "event": self._internals.serialize_event(event),
            "is_dm": info.is_dm,
            "highlight": action.highlight,
            "sound": action.sound,
        }
        sender_name = await self.sender_name(room_id, event.sender)
        if sender_name:
            reply["sender_name"] = sender_name
        if info.room_name:
            reply["room_name"] = info.room_name
        metrics.FETCHES.labels(status="ok", reason="").inc()
        return reply

    async def room_info(self, user_id: str, room_id: str) -> RoomInfo:
        room_name = None
        try:
            state = await self._internals.within(
                NAME_TIMEOUT_S,
                self._api.get_room_state,
                room_id,
                [("m.room.name", ""), ("m.room.canonical_alias", "")],
            )
            name = raw_name(state.get(("m.room.name", "")), "name")
            alias = raw_name(state.get(("m.room.canonical_alias", "")), "alias")
            room_name = name or alias
        except Exception as e:
            logger.warning("zuno_push: room name unavailable: %s", type(e).__name__)
        is_dm = False
        try:
            direct = await self._internals.within(
                NAME_TIMEOUT_S, self._api.account_data_manager.get_global, user_id, "m.direct"
            )
            is_dm = is_direct(direct, room_id)
        except Exception as e:
            logger.warning("zuno_push: m.direct unavailable: %s", type(e).__name__)
        return RoomInfo(room_name, is_dm)

    async def sender_name(self, room_id: str, sender: str) -> str | None:
        try:
            state = await self._internals.within(
                NAME_TIMEOUT_S, self._api.get_room_state, room_id, [("m.room.member", sender)]
            )
        except Exception as e:
            logger.warning("zuno_push: sender name unavailable: %s", type(e).__name__)
            return None
        return raw_name(state.get(("m.room.member", sender)), "displayname")

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

T = TypeVar("T")

DEFAULT_NOTIF_ACTION: list[Any] = ["notify", {"set_tweak": "highlight", "value": False}]
DEFAULT_HIGHLIGHT_ACTION: list[Any] = [
    "notify",
    {"set_tweak": "sound", "value": "default"},
    {"set_tweak": "highlight"},
]


@dataclass(frozen=True)
class Delta:
    stream_id: int
    room_id: str
    event_type: str
    state_key: str
    event_id: str | None


@dataclass(frozen=True)
class EventTimes:
    received_ts: int | None
    origin_server_ts: int | None


@dataclass(frozen=True)
class PusherRow:
    app_id: str
    last_success: int | None
    failing_since: int | None


@dataclass(frozen=True)
class PushAction:
    stream_ordering: int
    thread_id: str
    notif: bool
    highlight: bool
    sound: bool


@dataclass(frozen=True)
class ReadPoint:
    unthreaded_so: int = 0
    unthreaded_ts: int = 0
    threads: Mapping[str, int] = field(default_factory=dict)

    def is_read(self, stream_ordering: int, thread_id: str) -> bool:
        return (
            self.unthreaded_so >= stream_ordering
            or self.threads.get(thread_id, 0) >= stream_ordering
        )


@dataclass(frozen=True)
class NotifRow:
    event_id: str
    room_id: str
    thread_id: str
    stream_ordering: int
    highlight: bool
    sound: bool
    received_ts: int


@dataclass(frozen=True)
class ReceiptRow:
    room_id: str
    stream_id: int
    event_ts: int


Probe = Callable[[], Awaitable[None]]
StateMap = Mapping[tuple[str, str], Any]


def has_sound(actions_text: str | None, highlight: bool) -> bool:
    if actions_text:
        actions = json.loads(actions_text)
    else:
        actions = DEFAULT_HIGHLIGHT_ACTION if highlight else DEFAULT_NOTIF_ACTION
    return any(isinstance(a, dict) and a.get("set_tweak") == "sound" for a in actions)


class Internals(Protocol):
    async def within(self, seconds: float, fn: Callable[..., Awaitable[T]], *args: Any) -> T: ...

    def max_stream_ordering(self) -> int: ...

    async def state_deltas(self, from_pos: int, to_pos: int) -> tuple[int, list[Delta]]: ...

    async def first_stream_ordering_after(self, ts: int) -> int: ...

    async def get_event(self, event_id: str) -> Any: ...

    async def event_times(self, event_id: str) -> EventTimes | None: ...

    async def device_exists(self, user_id: str, device_id: str) -> bool: ...

    async def delete_rows_for_missing_devices(self) -> int: ...

    async def pushers_for_device(self, user_id: str, device_id: str) -> list[PusherRow]: ...

    def is_pusher_instance_for(self, user_id: str) -> bool: ...

    def user_level(self, state: StateMap, user_id: str) -> int: ...

    def call_member_level(self, state: StateMap) -> int: ...

    async def would_notify(
        self,
        state: StateMap,
        joined_count: int,
        sender_level: int | None,
        event: Mapping[str, Any],
        user_id: str,
        display_name: str | None,
    ) -> bool: ...

    async def joined_count(self, room_id: str) -> int: ...

    async def local_joined_users(self, room_id: str) -> list[str]: ...

    async def push_action(self, user_id: str, room_id: str, event_id: str) -> PushAction | None: ...

    async def read_points(
        self, user_id: str, room_ids: Collection[str]
    ) -> dict[str, ReadPoint]: ...

    async def notif_rows_after(
        self, user_id: str, after_so: int, upto_so: int, limit: int
    ) -> list[NotifRow]: ...

    async def receipts_after(
        self, user_id: str, after_stream_id: int, limit: int
    ) -> list[ReceiptRow]: ...

    async def user_receipt_max(self, user_id: str) -> int: ...

    def serialize_event(self, event: Any) -> dict[str, Any]: ...

    def probes(self) -> dict[str, list[tuple[str, Probe]]]: ...

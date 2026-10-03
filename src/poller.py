from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from synapse.module_api import ModuleApi

from zuno_push import metrics
from zuno_push.records import Internals
from zuno_push.rules import CallStart, RingRules
from zuno_push.store import CallRow, PushStore

logger = logging.getLogger("zuno_push")

STARTUP_LOOKBACK_MS = 5 * 60_000
CALL_MEMBER = "m.call.member"
RING_KINDS = ("voice", "video")
MAX_BATCHES_PER_TICK = 10
MAX_CALLS_PER_EVENT = 4
MAX_DELTA_ATTEMPTS = 3


def call_memberships(content: Any) -> list[tuple[str, str]]:
    raw = content.get("memberships") if isinstance(content, Mapping) else None
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for membership in raw:
        if not isinstance(membership, Mapping):
            continue
        call_id = membership.get("call_id")
        kind = membership.get("kind")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 255:
            continue
        if not isinstance(kind, str) or call_id in seen:
            continue
        seen.add(call_id)
        out.append((call_id, kind))
        if len(out) == MAX_CALLS_PER_EVENT:
            break
    return out


class DeltaPoller:
    def __init__(
        self,
        api: ModuleApi,
        internals: Internals,
        store: PushStore,
        rules: RingRules,
        on_new_rings: Callable[[], None],
    ) -> None:
        self._api = api
        self._internals = internals
        self._store = store
        self._rules = rules
        self._on_new_rings = on_new_rings
        self._busy = False
        self._attempts: dict[str, int] = {}
        self._position: int | None = None

    async def tick(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            await self._tick()
        except Exception as e:
            metrics.POLLER_ERRORS.inc()
            logger.warning("zuno_push: poller tick failed: %s", type(e).__name__)
        finally:
            self._busy = False

    async def _tick(self) -> None:
        top = self._internals.max_stream_ordering()
        if self._position is None:
            since = self._api.get_current_time_msec() - STARTUP_LOOKBACK_MS
            self._position = await self._internals.first_stream_ordering_after(since) - 1
        self._position = min(self._position, top)
        created = False
        try:
            for _ in range(MAX_BATCHES_PER_TICK):
                if self._position >= top:
                    break
                pos, deltas = await self._internals.state_deltas(self._position, top)
                for delta in deltas:
                    if delta.event_type == CALL_MEMBER and delta.event_id:
                        if await self._handle_once(delta.room_id, delta.event_id):
                            created = True
                self._position = pos
        finally:
            if created:
                self._on_new_rings()

    async def _handle_once(self, room_id: str, event_id: str) -> bool:
        try:
            created = await self._handle(room_id, event_id)
        except Exception as e:
            attempts = self._attempts.get(event_id, 0) + 1
            if attempts < MAX_DELTA_ATTEMPTS:
                self._attempts[event_id] = attempts
                raise
            self._attempts.pop(event_id, None)
            metrics.POLLER_SKIPPED.inc()
            logger.warning(
                "zuno_push: call event skipped after %d failures: %s", attempts, type(e).__name__
            )
            return False
        self._attempts.pop(event_id, None)
        return created

    async def _handle(self, room_id: str, event_id: str) -> bool:
        event = await self._internals.get_event(event_id)
        if event is None:
            return False
        times = await self._internals.event_times(event_id)
        received_ts = 0
        if times is not None:
            received_ts = int(times.received_ts or times.origin_server_ts or 0)
        now = self._api.get_current_time_msec()
        memberships = call_memberships(event.content)
        await self._store.end_calls(room_id, event.sender, [c for c, _ in memberships])
        created = False
        for call_id, kind in memberships:
            if kind not in RING_KINDS:
                continue
            if await self._store.call_exists(room_id, call_id):
                await self._store.touch_call(room_id, call_id, now)
                continue
            call = CallStart(room_id, call_id, event.sender, kind, received_ts)
            metrics.POLLER_LAG_SECONDS.observe(max(0, now - received_ts) / 1000)
            evaluation = await self._rules.evaluate(call, now)
            for ring in evaluation.rings:
                metrics.RING_DECISIONS.labels(decision=ring.state).inc()
            row = CallRow(
                room_id,
                call_id,
                event.sender,
                kind,
                received_ts,
                evaluation.cname,
                evaluation.rname,
            )
            if await self._store.create_call_with_rings(row, evaluation.rings, now):
                created = True
        return created

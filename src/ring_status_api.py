from __future__ import annotations

from typing import Any

from zuno_push import metrics, ratelimit
from zuno_push.http import ZunoResource, read_json, require_int, require_str

POLL_S = 0.25
MAX_WAIT_MS = 10_000


def describe(ring: tuple[str, int] | None, call_seen: bool) -> dict[str, Any]:
    if ring is None:
        return {"status": "no_token" if call_seen else "pending", "sent_ts": None, "rule": None}
    state, ts = ring
    if state == "sent":
        return {"status": "sent", "sent_ts": ts, "rule": None}
    if state.startswith("suppressed:"):
        return {"status": "suppressed", "sent_ts": None, "rule": state.removeprefix("suppressed:")}
    if state in ("pending", "no_token"):
        return {"status": state, "sent_ts": None, "rule": None}
    return {"status": "failed", "sent_ts": None, "rule": None}


class RingStatusResource(ZunoResource):
    async def _async_render_POST(self, request: Any) -> tuple[int, dict[str, Any]]:
        self._s.gate.require("ring")
        device = await self._s.credentials.authenticate(request)
        await self._s.limiter.check(ratelimit.RING_STATUS, device.cred_hash)
        body = read_json(request)
        room_id = require_str(body, "room_id", "!")
        call_id = require_str(body, "call_id")
        wait_ms = require_int(body, "wait_ms", 0, MAX_WAIT_MS)
        deadline = self._s.now() + wait_ms
        while True:
            ring, call_seen = await self._s.store.ring_and_call(
                room_id, call_id, device.user_id, device.device_id
            )
            status = describe(ring, call_seen)
            remaining = deadline - self._s.now()
            if status["status"] != "pending" or remaining <= 0:
                break
            await self._s.api.sleep(min(POLL_S, remaining / 1000))
        metrics.RING_STATUS.labels(status=status["status"]).inc()
        status["server_ts"] = self._s.now()
        return 200, status

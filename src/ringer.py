from __future__ import annotations

import base64
import logging
from collections.abc import Collection

from synapse.module_api import ModuleApi, make_deferred_yieldable, run_in_background
from twisted.internet import defer

from zuno_push import metrics
from zuno_push.rules import FRESH_MS
from zuno_push.sealer import LIFETIME_S, PayloadTooLarge, RingEndPayload, RingPayload, seal_b64
from zuno_push.sender import SygnalSender
from zuno_push.store import PendingEnd, PendingRing, PushStore

logger = logging.getLogger("zuno_push")

BATCH = 8
END_WINDOW_MS = FRESH_MS + LIFETIME_S * 1000


class Ringer:
    def __init__(
        self,
        api: ModuleApi,
        store: PushStore,
        sender: SygnalSender,
        fcm_app_ids: Collection[str],
    ) -> None:
        self._api = api
        self._store = store
        self._sender = sender
        self._fcm_app_ids = tuple(fcm_app_ids)
        self._inflight: set[tuple[str, ...]] = set()
        self._driving = False
        self._again = False

    def kick(self) -> None:
        if self._driving:
            self._again = True
            return
        self._api.run_as_background_process("zuno_push_ring_send", self.drive)

    async def drive(self) -> None:
        if self._driving:
            self._again = True
            return
        self._driving = True
        attempted: set[tuple[str, ...]] = set()
        try:
            while True:
                self._again = False
                now = self._api.get_current_time_msec()
                await self._store.expire_pending(now - FRESH_MS, now)
                rings = await self._store.pending_rings(now - FRESH_MS)
                ends = await self._store.pending_ends(now - END_WINDOW_MS, self._fcm_app_ids)
                busy = self._inflight | attempted
                jobs = [j for j in [*rings, *ends] if j.key not in busy]
                if not jobs:
                    if self._again:
                        continue
                    break
                batch = jobs[:BATCH]
                attempted.update(j.key for j in batch)
                await make_deferred_yieldable(
                    defer.gatherResults(
                        [run_in_background(self._send_one, j) for j in batch],
                        consumeErrors=True,
                    )
                )
        except Exception as e:
            logger.warning("zuno_push: ring drive failed: %s", type(e).__name__)
        finally:
            self._driving = False

    async def _send_one(self, item: PendingRing | PendingEnd) -> None:
        self._inflight.add(item.key)
        try:
            if isinstance(item, PendingEnd):
                await self._end(item)
            else:
                await self._send(item)
        except Exception as e:
            logger.warning("zuno_push: ring send failed: %s", type(e).__name__)
        finally:
            self._inflight.discard(item.key)

    async def _send(self, ring: PendingRing) -> None:
        now = self._api.get_current_time_msec()
        if ring.pushkey is None or ring.voip_key is None or ring.kid is None or ring.app_id is None:
            await self._store.set_ring_state(*ring.key, "no_token", now)
            return
        payload = RingPayload(
            room=ring.room_id,
            call=ring.call_id,
            caller=ring.caller,
            cname=ring.cname,
            rname="" if ring.dm else ring.rname,
            kind=ring.kind,
            ts=now,
            rts=ring.received_ts,
        )
        try:
            blob = seal_b64(base64.b64decode(ring.voip_key), int(ring.kid), payload)
        except PayloadTooLarge:
            logger.warning("zuno_push: ring payload over 1024 bytes, not sent")
            await self._store.set_ring_state(*ring.key, "failed", now)
            return
        result = await self._sender.send(ring.app_id, ring.pushkey, blob, ring.received_ts)
        done = self._api.get_current_time_msec()
        state = "sent" if result.outcome == "sent" else "failed"
        if result.outcome == "sent":
            metrics.RING_LATENCY_SECONDS.observe(max(0, done - ring.received_ts) / 1000)
        await self._store.set_ring_state(*ring.key, state, done)
        await self._store.record_send(
            ring.user_id, ring.device_id, ring.pushkey, result.outcome, done
        )

    async def _end(self, end: PendingEnd) -> None:
        now = self._api.get_current_time_msec()
        payload = RingEndPayload(end.room_id, end.call_id, end.reason, now, end.received_ts)
        blob = seal_b64(base64.b64decode(end.voip_key), int(end.kid), payload)
        result = await self._sender.send(end.app_id, end.pushkey, blob, now, field="ze")
        done = self._api.get_current_time_msec()
        metrics.RING_ENDS.labels(reason=end.reason, result=result.outcome).inc()
        state = "sent" if result.outcome == "sent" else "failed"
        await self._store.set_ending(*end.key[1:], state)
        await self._store.record_send(end.user_id, end.device_id, end.pushkey, result.outcome, done)

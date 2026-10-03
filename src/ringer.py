from __future__ import annotations

import base64
import logging

from synapse.module_api import ModuleApi, make_deferred_yieldable, run_in_background
from twisted.internet import defer

from zuno_push.rules import FRESH_MS
from zuno_push.sealer import PayloadTooLarge, RingPayload, seal_b64
from zuno_push.sender import SygnalSender
from zuno_push.store import PendingRing, PushStore

logger = logging.getLogger("zuno_push")

BATCH = 8


class Ringer:
    def __init__(self, api: ModuleApi, store: PushStore, sender: SygnalSender) -> None:
        self._api = api
        self._store = store
        self._sender = sender
        self._inflight: set[tuple[str, str, str, str]] = set()
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
        attempted: set[tuple[str, str, str, str]] = set()
        try:
            while True:
                self._again = False
                now = self._api.get_current_time_msec()
                await self._store.expire_pending(now - FRESH_MS, now)
                rows = [
                    r
                    for r in await self._store.pending_rings(now - FRESH_MS)
                    if r.key not in self._inflight and r.key not in attempted
                ]
                if not rows:
                    if self._again:
                        continue
                    break
                for start in range(0, len(rows), BATCH):
                    batch = rows[start : start + BATCH]
                    attempted.update(r.key for r in batch)
                    await make_deferred_yieldable(
                        defer.gatherResults(
                            [run_in_background(self._send_one, r) for r in batch],
                            consumeErrors=True,
                        )
                    )
        except Exception as e:
            logger.warning("zuno_push: ring drive failed: %s", type(e).__name__)
        finally:
            self._driving = False

    async def _send_one(self, ring: PendingRing) -> None:
        self._inflight.add(ring.key)
        try:
            await self._send(ring)
        except Exception as e:
            logger.warning("zuno_push: ring send failed: %s", type(e).__name__)
        finally:
            self._inflight.discard(ring.key)

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
        await self._store.set_ring_state(*ring.key, state, done)
        await self._store.record_send(
            ring.user_id, ring.device_id, ring.pushkey, result.outcome, done
        )

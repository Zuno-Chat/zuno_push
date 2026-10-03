from __future__ import annotations

import base64
import logging

from synapse.module_api import ModuleApi

from zuno_push import metrics
from zuno_push.config import CanaryConfig
from zuno_push.records import Internals
from zuno_push.sealer import RingPayload, seal_b64
from zuno_push.sender import SygnalSender
from zuno_push.store import PushStore

logger = logging.getLogger("zuno_push")

SLOW_MS = 2_000


def canary_payload(now: int) -> RingPayload:
    return RingPayload(
        room="", call=f"canary-{now}", caller="", cname="", rname="", kind="canary", ts=now, rts=now
    )


class Canary:
    def __init__(
        self,
        api: ModuleApi,
        internals: Internals,
        store: PushStore,
        sender: SygnalSender,
        config: CanaryConfig,
    ) -> None:
        self._api = api
        self._internals = internals
        self._store = store
        self._sender = sender
        self._config = config

    async def run(self) -> str:
        result = await self._ring()
        metrics.CANARY.labels(result=result).inc()
        if result == "ok":
            metrics.CANARY_LAST_SUCCESS.set(self._api.get_current_time_msec() / 1000)
        else:
            logger.warning("zuno_push: canary ring %s", result)
        await self._alert()
        return result

    async def _ring(self) -> str:
        device = await self._store.get_device(self._config.user_id, self._config.device_id)
        if device is None or not device.has_voip or device.voip_key is None or device.kid is None:
            return "unregistered"
        now = self._api.get_current_time_msec()
        blob = seal_b64(base64.b64decode(device.voip_key), int(device.kid), canary_payload(now))
        started = self._api.get_current_time_msec()
        outcome = await self._sender.send(
            device.voip_app_id or "", device.voip_pushkey or "", blob, now
        )
        took = self._api.get_current_time_msec() - started
        metrics.CANARY_SEND_SECONDS.set(took / 1000)
        await self._store.record_send(
            device.user_id, device.device_id, device.voip_pushkey or "", outcome.outcome, now + took
        )
        if outcome.outcome != "sent":
            return outcome.outcome
        return "slow" if took > SLOW_MS else "ok"

    async def _alert(self) -> None:
        if not self._internals.is_pusher_instance_for(self._config.user_id):
            return
        now = self._api.get_current_time_msec()
        try:
            sent = await self._api.send_http_push_notification(
                self._config.user_id,
                self._config.device_id,
                {"event_id": f"$zuno_test_{now}", "prio": "high"},
            )
        except Exception as e:
            logger.warning("zuno_push: canary alert failed: %s", type(e).__name__)
            metrics.CANARY.labels(result="alert_failed").inc()
            return
        if not sent.get(self._config.device_id):
            logger.warning("zuno_push: canary alert not accepted")
            metrics.CANARY.labels(result="alert_failed").inc()

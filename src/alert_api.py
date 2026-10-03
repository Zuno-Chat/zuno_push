from __future__ import annotations

import logging
from typing import Any

from zuno_push import errors, metrics, ratelimit
from zuno_push.http import ZunoResource, bearer, read_json

logger = logging.getLogger("zuno_push")


class AlertResource(ZunoResource):
    async def _async_render_POST(self, request: Any) -> tuple[int, dict[str, Any]]:
        user_id, device_id = await bearer(self._s.api, request)
        self._s.gate.require("test")
        read_json(request, allow_empty=True)
        if not self._s.internals.is_pusher_instance_for(user_id):
            metrics.TEST_ALERTS.labels(result="not_pusher_instance").inc()
            raise errors.not_pusher_instance()
        await self._s.limiter.check(ratelimit.TEST_ALERT, f"{user_id}|{device_id}")
        now = self._s.now()
        event_id = f"$zuno_test_{now}"
        result = await self._s.api.send_http_push_notification(
            user_id, device_id, {"event_id": event_id, "prio": "high"}
        )
        if device_id not in result:
            metrics.TEST_ALERTS.labels(result="no_pusher").inc()
            raise errors.not_pusher_instance()
        if not result[device_id]:
            logger.warning("zuno_push: test alert not accepted by the push gateway")
            metrics.TEST_ALERTS.labels(result="gateway_failed").inc()
        else:
            metrics.TEST_ALERTS.labels(result="sent").inc()
        return 200, {"event_id": event_id, "server_ts": now}

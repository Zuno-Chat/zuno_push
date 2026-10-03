from __future__ import annotations

from typing import Any

from zuno_push import ratelimit
from zuno_push.http import ZunoResource, bearer


class HealthResource(ZunoResource):
    async def _async_render_GET(self, request: Any) -> tuple[int, dict[str, Any]]:
        user_id, device_id = await bearer(self._s.api, request)
        self._s.gate.require("health")
        await self._s.limiter.check(ratelimit.HEALTH, f"{user_id}|{device_id}")
        now = self._s.now()
        pushers = await self._s.internals.pushers_for_device(user_id, device_id)
        row = await self._s.store.get_device(user_id, device_id)
        registered = row is not None and row.has_voip
        credential_live = (
            row is not None and row.cred_hash is not None and (row.cred_expires or 0) > now
        )
        return 200, {
            "pushers": [
                {
                    "app_id": p.app_id,
                    "last_success_ts": p.last_success,
                    "failing_since_ts": p.failing_since,
                }
                for p in pushers
            ],
            "voip": {
                "registered": registered,
                "kid": row.kid if registered and row is not None else None,
                "last_result": row.last_result if row is not None else None,
                "last_ts": row.last_ts if row is not None else None,
            },
            "nse": {
                "credential_expires_ts": row.cred_expires if credential_live and row else None,
                "last_fetch_ts": row.last_fetch_ts if row is not None else None,
            },
            "server_ts": now,
        }

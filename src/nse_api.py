from __future__ import annotations

from typing import Any

from zuno_push import ratelimit
from zuno_push.credential import LIFETIME_MS, mint
from zuno_push.http import ZunoResource, bearer, read_json, require_str


class CredentialResource(ZunoResource):
    async def _async_render_POST(self, request: Any) -> tuple[int, dict[str, Any]]:
        user_id, device_id = await bearer(self._s.api, request)
        self._s.gate.require("nse")
        read_json(request, allow_empty=True)
        await self._s.limiter.check(ratelimit.CREDENTIAL_DEVICE, f"{user_id}|{device_id}")
        await self._s.limiter.check(ratelimit.CREDENTIAL_USER, user_id)
        floor_so = floor_rs = None
        if self._s.gate.enabled("catchup"):
            floor_so = self._s.internals.max_stream_ordering()
            floor_rs = await self._s.internals.user_receipt_max(user_id)
        credential, cred_hash = mint()
        now = self._s.now()
        expires = now + LIFETIME_MS
        await self._s.store.set_credential(
            user_id, device_id, cred_hash, expires, now, floor_so, floor_rs
        )
        return 200, {"credential": credential, "expires_ts": expires, "server_ts": now}


class FetchResource(ZunoResource):
    async def _async_render_POST(self, request: Any) -> tuple[int, dict[str, Any]]:
        self._s.gate.require("nse")
        device = await self._s.credentials.authenticate(request)
        await self._s.limiter.check(ratelimit.FETCH, device.cred_hash)
        body = read_json(request)
        room_id = require_str(body, "room_id", "!")
        event_id = require_str(body, "event_id", "$")
        now = self._s.now()
        reply = await self._s.scope.fetch(device.user_id, room_id, event_id, now)
        await self._s.store.touch_fetch(device.user_id, device.device_id, now)
        if self._s.gate.enabled("catchup"):
            reply.update(
                await self._s.catchup.extras(device.user_id, device.device_id, event_id, now)
            )
        reply["server_ts"] = now
        return 200, reply

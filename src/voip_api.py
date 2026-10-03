from __future__ import annotations

import base64
import binascii
from typing import Any

from zuno_push import errors, ratelimit
from zuno_push.http import ZunoResource, bearer, read_json, require_int

MAX_KID = 0xFFFFFFFF


def strict_b64(value: Any, key: str, min_len: int, max_len: int) -> bytes:
    if not isinstance(value, str) or not value:
        raise errors.invalid_param(f"'{key}' must be base64")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise errors.invalid_param(f"'{key}' must be base64") from None
    if base64.b64encode(raw).decode("ascii") != value or not min_len <= len(raw) <= max_len:
        raise errors.invalid_param(f"'{key}' must be padded base64 of {min_len}..{max_len} bytes")
    return raw


class VoipResource(ZunoResource):
    async def _async_render_PUT(self, request: Any) -> tuple[int, dict[str, Any]]:
        user_id, device_id = await bearer(self._s.api, request)
        self._s.gate.require("ring")
        await self._s.limiter.check(ratelimit.VOIP_PUT, f"{user_id}|{device_id}")
        body = read_json(request)
        app_id = body.get("app_id")
        if app_id not in self._s.config.voip_app_ids:
            raise errors.invalid_param("'app_id' is not a Zuno VoIP app id")
        pushkey = body.get("pushkey")
        strict_b64(pushkey, "pushkey", 1, 512)
        kid = require_int(body, "kid", 1, MAX_KID)
        key = body.get("key")
        strict_b64(key, "key", 32, 32)
        await self._s.store.upsert_voip(user_id, device_id, app_id, pushkey, kid, key)
        return 200, {"kid": kid, "server_ts": self._s.now()}

    async def _async_render_DELETE(self, request: Any) -> tuple[int, dict[str, Any]]:
        user_id, device_id = await bearer(self._s.api, request)
        await self._s.store.clear_voip(user_id, device_id)
        return 200, {"server_ts": self._s.now()}


class DeviceResource(ZunoResource):
    async def _async_render_DELETE(self, request: Any) -> tuple[int, dict[str, Any]]:
        user_id, device_id = await bearer(self._s.api, request)
        await self._s.store.delete_device(user_id, device_id)
        return 200, {"server_ts": self._s.now()}

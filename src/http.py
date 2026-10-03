from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from synapse.module_api import DirectServeJsonResource, ModuleApi

from zuno_push import errors

if TYPE_CHECKING:
    from zuno_push.services import Services

ZUNO_HEADER = b"X-Zuno-Push"
BASE_PATH = "/_synapse/client/zuno/push/v1"
MAX_BODY_BYTES = 8192


class ZunoResource(DirectServeJsonResource):
    def __init__(self, services: Services) -> None:
        super().__init__()
        self._s = services

    async def _async_render(self, request: Any) -> Any:
        request.setHeader(ZUNO_HEADER, b"1")
        return await super()._async_render(request)


def read_json(request: Any, allow_empty: bool = False) -> dict[str, Any]:
    raw = request.content.read() if request.content is not None else b""
    if len(raw) > MAX_BODY_BYTES:
        raise errors.invalid_param("Body too large")
    if not raw.strip():
        if allow_empty:
            return {}
        raise errors.not_json()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise errors.not_json() from None
    if not isinstance(value, dict):
        raise errors.not_json()
    return value


def authorization(request: Any) -> str | None:
    values = request.requestHeaders.getRawHeaders(b"Authorization")
    if not values:
        return None
    value = values[0]
    return value.decode("latin-1") if isinstance(value, bytes) else value


async def bearer(api: ModuleApi, request: Any) -> tuple[str, str]:
    requester = await api.get_user_by_req(request)
    if not requester.device_id:
        raise errors.invalid_param("This access token has no device")
    return requester.user.to_string(), requester.device_id


def require_str(body: dict[str, Any], key: str, prefix: str = "", max_len: int = 255) -> str:
    value = body.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_len
        or not value.startswith(prefix)
    ):
        raise errors.invalid_param(f"'{key}' is missing or invalid")
    return value


def require_int(body: dict[str, Any], key: str, low: int, high: int) -> int:
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise errors.invalid_param(f"'{key}' must be an integer in {low}..{high}")
    return value

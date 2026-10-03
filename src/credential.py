from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Any

from synapse.module_api import ModuleApi

from zuno_push import errors
from zuno_push.http import authorization
from zuno_push.records import Internals
from zuno_push.store import PushStore

SCHEME = "zunonotify"
CREDENTIAL_RE = re.compile(r"[A-Za-z0-9_-]{43}")
LIFETIME_MS = 30 * 86_400_000


@dataclass(frozen=True)
class AuthedDevice:
    user_id: str
    device_id: str
    cred_hash: str


def hash_credential(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def mint() -> tuple[str, str]:
    credential = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    return credential, hash_credential(credential)


def parse_header(value: str | None) -> str | None:
    if value is None:
        return None
    scheme, _, rest = value.strip().partition(" ")
    credential = rest.strip()
    if scheme.lower() != SCHEME or not CREDENTIAL_RE.fullmatch(credential):
        return None
    return credential


class CredentialAuth:
    def __init__(self, api: ModuleApi, store: PushStore, internals: Internals) -> None:
        self._api = api
        self._store = store
        self._internals = internals

    async def authenticate(self, request: Any) -> AuthedDevice:
        credential = parse_header(authorization(request))
        if credential is None:
            raise errors.bad_credential()
        cred_hash = hash_credential(credential)
        found = await self._store.find_credential(cred_hash, self._api.get_current_time_msec())
        if found is None:
            raise errors.bad_credential()
        user_id, device_id = found
        if not await self._internals.device_exists(user_id, device_id):
            await self._store.delete_device(user_id, device_id)
            raise errors.bad_credential()
        info = await self._api.get_userinfo_by_id(user_id)
        if (
            info is None
            or info.is_deactivated
            or getattr(info, "locked", False)
            or getattr(info, "suspended", False)
        ):
            raise errors.bad_credential()
        return AuthedDevice(user_id, device_id, cred_hash)

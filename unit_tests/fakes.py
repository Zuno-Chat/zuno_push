from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from synapse.api.errors import MissingClientTokenError
from synapse.module_api import UserID
from twisted.internet import defer, task
from twisted.web.http_headers import Headers

from zuno_push.schema import migrate_txn


class FakeTxn:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    def execute(self, sql: str, args: Any = ()) -> None:
        self._cursor.execute(sql, tuple(args))

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount


@dataclass
class FakeEvent:
    type: str
    sender: str
    content: dict[str, Any]
    state_key: str | None = None
    room_id: str = "!room:test"
    event_id: str = "$event:test"
    origin_server_ts: int = 0
    unsigned: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeUserInfo:
    is_deactivated: bool = False
    locked: bool = False
    suspended: bool = False


@dataclass
class FakeRequester:
    user: UserID
    device_id: str | None


class FakeHttpClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, Any]] = []
        self.responses: list[Callable[[], Any]] = []

    def post_json_get_json(self, uri: str, body: Any) -> defer.Deferred:
        self.requests.append((uri, json.loads(json.dumps(body))))
        if not self.responses:
            return defer.succeed({"rejected": []})
        return defer.maybeDeferred(self.responses.pop(0))


class FakeAccountData:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], Any] = {}
        self.fail: set[str] = set()

    async def get_global(self, user_id: str, data_type: str) -> Any:
        if data_type in self.fail:
            raise RuntimeError("account data unavailable")
        return self.data.get((user_id, data_type))


class FakeModuleApi:
    def __init__(
        self, clock: task.Clock | None = None, server_name: str = "test", migrated: bool = True
    ) -> None:
        self.clock = clock or task.Clock()
        self.conn = sqlite3.connect(":memory:")
        self.server_name = server_name
        self.http_client = FakeHttpClient()
        self.account_data_manager = FakeAccountData()
        self.tokens: dict[str, tuple[str, str | None]] = {}
        self.userinfo: dict[str, FakeUserInfo] = {}
        self.room_state: dict[str, dict[tuple[str, str], FakeEvent]] = {}
        self.blocked_state: set[tuple[str, str]] = set()
        self.push_results: dict[str, dict[str, bool]] = {}
        self.pushes: list[tuple[str, str | None, dict[str, Any]]] = []
        self.background: list[Any] = []
        self.looping: list[tuple[Any, float, str | None]] = []
        self.resources: dict[str, Any] = {}
        self.run_background_tasks = True
        self.on_logged_out: Any = None
        self.on_deactivation: Any = None
        if migrated:
            self.run_db_interaction("zuno_push_migrate", migrate_txn)

    def run_db_interaction(self, desc: str, func: Callable[..., Any], *args: Any) -> defer.Deferred:
        cursor = self.conn.cursor()
        try:
            result = func(FakeTxn(cursor), *args)
        except Exception as e:
            self.conn.rollback()
            return defer.fail(e)
        self.conn.commit()
        return defer.succeed(result)

    def get_current_time_msec(self) -> int:
        return int(self.clock.seconds() * 1000)

    async def sleep(self, seconds: float) -> None:
        d: defer.Deferred = defer.Deferred()
        self.clock.callLater(seconds, d.callback, None)
        await d

    def is_mine(self, user_id: str) -> bool:
        return user_id.endswith(f":{self.server_name}")

    def should_run_background_tasks(self) -> bool:
        return self.run_background_tasks

    def looping_background_call(
        self, f: Any, msec: float, *args: Any, desc: str | None = None, **kw: Any
    ) -> None:
        self.looping.append((f, msec, desc))

    def run_as_background_process(
        self, desc: str, func: Any, *args: Any, **kw: Any
    ) -> defer.Deferred:
        d = defer.ensureDeferred(func(*args))
        self.background.append(d)
        return d

    def register_web_resource(self, path: str, resource: Any) -> None:
        self.resources[path] = resource

    def register_password_auth_provider_callbacks(
        self, *, on_logged_out: Any = None, **kw: Any
    ) -> None:
        self.on_logged_out = on_logged_out

    def register_third_party_rules_callbacks(
        self, *, on_user_deactivation_status_changed: Any = None, **kw: Any
    ) -> None:
        self.on_deactivation = on_user_deactivation_status_changed

    async def get_user_by_req(
        self, request: Any, allow_guest: bool = False, allow_expired: bool = False
    ) -> FakeRequester:
        values = request.requestHeaders.getRawHeaders(b"Authorization") or []
        token = values[0].decode("latin-1").removeprefix("Bearer ") if values else None
        if token not in self.tokens:
            raise MissingClientTokenError()
        user_id, device_id = self.tokens[token]
        return FakeRequester(UserID.from_string(user_id), device_id)

    async def get_userinfo_by_id(self, user_id: str) -> FakeUserInfo | None:
        return self.userinfo.get(user_id)

    async def get_room_state(
        self, room_id: str, event_filter: Any = None
    ) -> dict[tuple[str, str], FakeEvent]:
        state = self.room_state.get(room_id, {})
        wanted = list(event_filter or state.keys())
        if any(
            (room_id, key[1]) in self.blocked_state for key in wanted if key[0] == "m.room.member"
        ):
            await defer.Deferred()
        return {key: state[key] for key in wanted if key in state}

    async def send_http_push_notification(
        self,
        user_id: str,
        device_id: str | None,
        content: dict[str, Any],
        tweaks: Any = None,
        default_payload: Any = None,
    ) -> dict[str, bool]:
        self.pushes.append((user_id, device_id, content))
        return self.push_results.get(user_id, {})


class FakeRequest:
    def __init__(
        self, method: bytes, body: Any = b"", token: str | None = None, auth: str | None = None
    ) -> None:
        self.method = method
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.content = io.BytesIO(raw)
        self.requestHeaders = Headers()
        if token is not None:
            self.requestHeaders.setRawHeaders(b"Authorization", [f"Bearer {token}".encode()])
        if auth is not None:
            self.requestHeaders.setRawHeaders(b"Authorization", [auth.encode()])
        self.responseHeaders = Headers()

    def setHeader(self, name: bytes, value: bytes) -> None:
        self.responseHeaders.setRawHeaders(name, [value])


def run(d: Any) -> Any:
    deferred = defer.ensureDeferred(d) if not isinstance(d, defer.Deferred) else d
    results: list[Any] = []
    failures: list[Any] = []
    deferred.addCallbacks(results.append, failures.append)
    if failures:
        failures[0].raiseException()
    if not results:
        raise AssertionError("deferred did not fire")
    return results[0]


def state_event(
    type_: str, state_key: str, content: dict[str, Any], room_id: str, sender: str = "@creator:test"
) -> FakeEvent:
    return FakeEvent(
        type=type_, sender=sender, content=content, state_key=state_key, room_id=room_id
    )

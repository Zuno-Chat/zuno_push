from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from synapse.module_api import ModuleApi

from zuno_push import metrics, ratelimit
from zuno_push.names import ignores, is_direct, member_name, raw_name, room_title
from zuno_push.ratelimit import RateLimiter
from zuno_push.records import Internals, StateMap
from zuno_push.store import DeviceRow, PushStore, RingRow

logger = logging.getLogger("zuno_push")

FRESH_MS = 30_000
MAX_CALLS_PER_EVENT = 4
STATE_TIMEOUT_S = 2.0
ROOM_TYPES = [
    ("m.room.create", ""),
    ("m.room.power_levels", ""),
    ("m.room.encryption", ""),
    ("m.room.name", ""),
    ("m.room.canonical_alias", ""),
]


@dataclass(frozen=True)
class CallStart:
    room_id: str
    call_id: str
    caller: str
    kind: str
    received_ts: int


@dataclass(frozen=True)
class Evaluation:
    cname: str
    rname: str
    rings: list[RingRow]


def invite_shaped(call: CallStart, encrypted: bool) -> dict[str, Any]:
    base = {"room_id": call.room_id, "sender": call.caller}
    if encrypted:
        return {
            **base,
            "type": "m.room.encrypted",
            "content": {"algorithm": "m.megolm.v1.aes-sha2"},
        }
    body = "Incoming video call" if call.kind == "video" else "Incoming voice call"
    return {
        **base,
        "type": "m.room.message",
        "content": {
            "msgtype": "im.zuno.call_invite",
            "body": body,
            "call_id": call.call_id,
            "kind": call.kind,
        },
    }


def call_memberships(content: Any) -> list[tuple[str, str]]:
    raw = content.get("memberships") if isinstance(content, Mapping) else None
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for membership in raw:
        if not isinstance(membership, Mapping):
            continue
        call_id = membership.get("call_id")
        kind = membership.get("kind")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 255:
            continue
        if not isinstance(kind, str) or call_id in seen:
            continue
        seen.add(call_id)
        out.append((call_id, kind))
        if len(out) == MAX_CALLS_PER_EVENT:
            break
    return out


class RingRules:
    def __init__(
        self,
        api: ModuleApi,
        internals: Internals,
        store: PushStore,
        limiter: RateLimiter,
        max_targets: int,
    ) -> None:
        self._api = api
        self._internals = internals
        self._store = store
        self._limiter = limiter
        self._max_targets = max_targets

    async def evaluate(self, call: CallStart, now: int) -> Evaluation:
        state = await self._internals.within(
            STATE_TIMEOUT_S, self._api.get_room_state, call.room_id, ROOM_TYPES
        )
        rname = room_title(
            state.get(("m.room.name", "")), state.get(("m.room.canonical_alias", ""))
        )
        cname = await self._caller_name(call)
        users = await self._internals.local_joined_users(call.room_id)
        devices = await self._store.voip_devices_for_users(users)
        by_user: dict[str, list[DeviceRow]] = {}
        for device in devices:
            by_user.setdefault(device.user_id, []).append(device)
        if now - call.received_ts > FRESH_MS:
            return Evaluation(cname, rname, [_row(d, "not_fresh") for d in devices])

        targets = sorted(u for u in by_user if u != call.caller)
        members = await self._member_state(call.room_id, targets)
        context = await self._context(call, state)
        rows: list[RingRow] = []
        accepted = 0
        call_allowed: bool | None = None
        for user in sorted(by_user):
            if user == call.caller:
                rows.extend(_row(d, "sender") for d in by_user[user])
                continue
            rule = await self._user_rule(call, user, state, members, context)
            if rule is None:
                if call_allowed is None:
                    call_allowed = await self._call_allowed(call)
                if not call_allowed or not await self._allow(
                    ratelimit.RING_PAIR, f"{call.caller}|{user}"
                ):
                    rule = "rate_limited"
            if rule is not None:
                rows.extend(_row(d, rule) for d in by_user[user])
                continue
            dm = await self._is_dm(user, call.room_id)
            for device in by_user[user]:
                device_rule = await self._device_rule(device)
                if device_rule is None:
                    if accepted == self._max_targets:
                        logger.warning("zuno_push: max_ring_targets reached for one call")
                    if accepted >= self._max_targets:
                        device_rule = "target_cap"
                    accepted += 1
                ring_state = "pending" if device_rule is None else f"suppressed:{device_rule}"
                rows.append(RingRow(device.user_id, device.device_id, ring_state, dm))
        return Evaluation(cname, rname, rows)

    async def _caller_name(self, call: CallStart) -> str:
        try:
            state = await self._internals.within(
                STATE_TIMEOUT_S,
                self._api.get_room_state,
                call.room_id,
                [("m.room.member", call.caller)],
            )
        except Exception as e:
            logger.warning("zuno_push: caller name unavailable: %s", type(e).__name__)
            return ""
        return member_name(state.get(("m.room.member", call.caller)))

    async def _member_state(self, room_id: str, users: list[str]) -> StateMap:
        if not users:
            return {}
        wanted = [("m.room.member", u) for u in users] + [("m.call.member", u) for u in users]
        try:
            return await self._internals.within(
                STATE_TIMEOUT_S, self._api.get_room_state, room_id, wanted
            )
        except Exception as e:
            logger.warning("zuno_push: member state unavailable: %s", type(e).__name__)
            return {}

    async def _context(self, call: CallStart, state: StateMap) -> dict[str, Any]:
        context: dict[str, Any] = {"required": None, "sender_level": None, "joined": 2}
        try:
            context["required"] = self._internals.call_member_level(state)
        except Exception as e:
            logger.warning("zuno_push: send level unavailable: %s", type(e).__name__)
        try:
            context["sender_level"] = self._internals.user_level(state, call.caller)
        except Exception as e:
            logger.warning("zuno_push: caller level unavailable: %s", type(e).__name__)
        try:
            context["joined"] = await self._internals.joined_count(call.room_id)
        except Exception as e:
            logger.warning("zuno_push: member count unavailable: %s", type(e).__name__)
        context["event"] = invite_shaped(call, ("m.room.encryption", "") in state)
        return context

    async def _user_rule(
        self,
        call: CallStart,
        user: str,
        state: Any,
        members: StateMap,
        context: Mapping[str, Any],
    ) -> str | None:
        call_member = members.get(("m.call.member", user))
        if call_member is not None and call.call_id in dict(call_memberships(call_member.content)):
            return "live"
        if context["required"] is None:
            return "not_allowed"
        try:
            if self._internals.user_level(state, user) < context["required"]:
                return "not_allowed"
        except Exception as e:
            logger.warning("zuno_push: power level check failed: %s", type(e).__name__)
            return "not_allowed"
        try:
            ignored = await self._internals.within(
                STATE_TIMEOUT_S,
                self._api.account_data_manager.get_global,
                user,
                "m.ignored_user_list",
            )
            if ignores(ignored, call.caller):
                return "ignored"
        except Exception as e:
            logger.warning("zuno_push: ignore list unavailable: %s", type(e).__name__)
            return "ignored"
        try:
            notify = await self._internals.within(
                STATE_TIMEOUT_S,
                self._internals.would_notify,
                state,
                context["joined"],
                context["sender_level"],
                context["event"],
                user,
                raw_name(members.get(("m.room.member", user)), "displayname"),
            )
            if not notify:
                return "no_notify"
        except Exception as e:
            logger.warning("zuno_push: push rules unavailable, ringing: %s", type(e).__name__)
        return None

    async def _call_allowed(self, call: CallStart) -> bool:
        room_ok = await self._allow(ratelimit.RING_ROOM, call.room_id)
        sender_ok = await self._allow(ratelimit.RING_SENDER, call.caller)
        return room_ok and sender_ok

    async def _allow(self, limit: ratelimit.Limit, key: str) -> bool:
        try:
            allowed = await self._limiter.allow(limit, key)
        except Exception as e:
            logger.warning("zuno_push: ring limit unavailable, ringing: %s", type(e).__name__)
            return True
        if not allowed:
            metrics.RATE_LIMITED.labels(limit=limit.name).inc()
        return allowed

    async def _is_dm(self, user: str, room_id: str) -> bool:
        try:
            direct = await self._internals.within(
                STATE_TIMEOUT_S, self._api.account_data_manager.get_global, user, "m.direct"
            )
        except Exception as e:
            logger.warning("zuno_push: m.direct unavailable: %s", type(e).__name__)
            return False
        return is_direct(direct, room_id)

    async def _device_rule(self, device: DeviceRow) -> str | None:
        try:
            if not await self._internals.device_exists(device.user_id, device.device_id):
                return "device_gone"
        except Exception as e:
            logger.warning("zuno_push: device check failed, ringing: %s", type(e).__name__)
        return None


def _row(device: DeviceRow, rule: str) -> RingRow:
    return RingRow(device.user_id, device.device_id, f"suppressed:{rule}", False)

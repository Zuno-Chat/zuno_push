from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from synapse.module_api import ModuleApi

from zuno_push import errors, metrics

MINUTE = 60_000
HOUR = 3_600_000
DAY = 86_400_000


@dataclass(frozen=True)
class Limit:
    name: str
    hits: int
    period_ms: int


VOIP_PUT = Limit("voip", 10, MINUTE)
HEALTH = Limit("health", 30, MINUTE)
TEST_ALERT = Limit("test", 3, HOUR)
CREDENTIAL_DEVICE = Limit("cred_device", 10, HOUR)
CREDENTIAL_USER = Limit("cred_user", 50, DAY)
FETCH = Limit("fetch", 60, MINUTE)
RING_STATUS = Limit("ring_status", 30, MINUTE)
RING_PAIR = Limit("ring_pair", 8, MINUTE)
RING_SENDER = Limit("ring_sender", 30, MINUTE)
RING_ROOM = Limit("ring_room", 20, MINUTE)


class RateLimiter:
    def __init__(self, api: ModuleApi) -> None:
        self._api = api

    async def hit(self, limit: Limit, key: str) -> int:
        now = self._api.get_current_time_msec()
        window = now - now % limit.period_ms
        counter_key = f"{limit.name}:{key}"

        def txn(t: Any) -> int:
            t.execute(
                "INSERT INTO zuno_push_counters (counter_key, window_start, hits, expires_ts)"
                " VALUES (?, ?, 1, ?)"
                " ON CONFLICT (counter_key, window_start)"
                " DO UPDATE SET hits = zuno_push_counters.hits + 1",
                (counter_key, window, window + limit.period_ms),
            )
            t.execute(
                "SELECT hits FROM zuno_push_counters WHERE counter_key = ? AND window_start = ?",
                (counter_key, window),
            )
            return int(t.fetchone()[0])

        hits = await self._api.run_db_interaction("zuno_push_rate_limit", txn)
        if hits > limit.hits:
            return window + limit.period_ms - now
        return 0

    async def check(self, limit: Limit, key: str) -> None:
        retry_after_ms = await self.hit(limit, key)
        if retry_after_ms:
            metrics.RATE_LIMITED.labels(limit=limit.name).inc()
            raise errors.rate_limited(retry_after_ms)

    async def allow(self, limit: Limit, key: str) -> bool:
        return await self.hit(limit, key) == 0

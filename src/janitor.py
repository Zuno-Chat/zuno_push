from __future__ import annotations

import logging

from synapse.module_api import ModuleApi

from zuno_push.records import Internals
from zuno_push.store import PushStore

logger = logging.getLogger("zuno_push")

DAY_MS = 86_400_000
INTERVAL_MS = 3_600_000


class Janitor:
    def __init__(self, api: ModuleApi, internals: Internals, store: PushStore) -> None:
        self._api = api
        self._internals = internals
        self._store = store

    async def run(self) -> None:
        now = self._api.get_current_time_msec()
        try:
            await self._store.janitor_sweep(now, DAY_MS)
            await self._internals.delete_rows_for_missing_devices()
        except Exception as e:
            logger.warning("zuno_push: janitor failed: %s", type(e).__name__)

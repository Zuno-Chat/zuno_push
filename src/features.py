from __future__ import annotations

import logging
from collections.abc import Collection, Mapping

from zuno_push import errors, metrics
from zuno_push.config import FEATURES
from zuno_push.records import Probe

logger = logging.getLogger("zuno_push")


class FeatureGate:
    def __init__(self) -> None:
        self._enabled: dict[str, bool] | None = None

    @property
    def started(self) -> bool:
        return self._enabled is not None

    def set_ready(self, probe_results: Mapping[str, bool], disabled: Collection[str]) -> None:
        enabled = {f: bool(probe_results.get(f)) and f not in disabled for f in FEATURES}
        enabled["catchup"] = enabled["catchup"] and enabled["nse"]
        self._enabled = enabled
        for feature, on in enabled.items():
            metrics.FEATURE_ENABLED.labels(feature=feature).set(1 if on else 0)

    def disable_all(self) -> None:
        self.set_ready({}, FEATURES)

    def enabled(self, feature: str) -> bool:
        return bool(self._enabled and self._enabled.get(feature))

    def require(self, feature: str) -> None:
        if not self.enabled(feature):
            raise errors.push_disabled(feature)


async def run_probes(probes: Mapping[str, list[tuple[str, Probe]]]) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for feature in FEATURES:
        ok = True
        for name, probe in probes.get(feature, []):
            try:
                await probe()
            except Exception:
                logger.error(
                    "zuno_push: probe %s failed, feature %s disabled", name, feature, exc_info=True
                )
                ok = False
                break
        results[feature] = ok
    return results

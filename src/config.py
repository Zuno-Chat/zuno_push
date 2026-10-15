from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FEATURES = ("ring", "health", "test", "nse", "catchup")
DEFAULT_VOIP_APP_IDS = ("im.zuno.chat.ios.voip", "im.zuno.chat.ios.dev.voip")
DEFAULT_MAX_RING_TARGETS = 5000
NOTIFY_PATH = "/_matrix/push/v1/notify"


@dataclass(frozen=True)
class ZunoPushConfig:
    sygnal_notify_url: str = ""
    voip_app_ids: tuple[str, ...] = DEFAULT_VOIP_APP_IDS
    max_ring_targets: int = DEFAULT_MAX_RING_TARGETS
    disabled_features: frozenset[str] = field(default_factory=frozenset)
    error: str | None = None


def parse_config(raw: Any) -> ZunoPushConfig:
    try:
        return _parse(raw)
    except Exception as e:
        return ZunoPushConfig(error=f"{type(e).__name__}: {e}")


def _parse(raw: Any) -> ZunoPushConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("config must be a mapping")
    unknown = set(raw) - {
        "sygnal_notify_url",
        "voip_app_ids",
        "max_ring_targets",
        "disabled_features",
    }
    if unknown:
        raise ValueError(f"unknown keys: {sorted(unknown)}")

    url = raw.get("sygnal_notify_url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("sygnal_notify_url must be an http(s) URL")
    if not url.endswith(NOTIFY_PATH):
        raise ValueError(f"sygnal_notify_url must end with {NOTIFY_PATH}")

    app_ids = raw.get("voip_app_ids", list(DEFAULT_VOIP_APP_IDS))
    if (
        not isinstance(app_ids, list)
        or not app_ids
        or not all(isinstance(a, str) and a for a in app_ids)
    ):
        raise ValueError("voip_app_ids must be a non-empty list of strings")

    cap = raw.get("max_ring_targets", DEFAULT_MAX_RING_TARGETS)
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        raise ValueError("max_ring_targets must be a positive integer")

    disabled = raw.get("disabled_features", [])
    if not isinstance(disabled, list) or not all(d in FEATURES for d in disabled):
        raise ValueError(f"disabled_features must list some of {FEATURES}")

    return ZunoPushConfig(
        sygnal_notify_url=url,
        voip_app_ids=tuple(app_ids),
        max_ring_targets=cap,
        disabled_features=frozenset(disabled),
    )

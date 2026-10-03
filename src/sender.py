from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from synapse.module_api import ModuleApi
from twisted.internet import defer

from zuno_push import metrics
from zuno_push.records import Internals

logger = logging.getLogger("zuno_push")

CONSTANT_EVENT_ID = "$z"
TIMEOUT_S = 4.0
RETRY_DELAY_S = 1.0
RETRY_WINDOW_MS = 10_000
IMMEDIATE_MS = 2_000
APNS_PAYLOAD_LIMIT = 4096


@dataclass(frozen=True)
class SendResult:
    outcome: str
    status: int | None
    attempts: int


def notify_body(app_id: str, pushkey: str, blob_b64: str) -> dict[str, Any]:
    return {
        "notification": {
            "event_id": CONSTANT_EVENT_ID,
            "prio": "high",
            "devices": [
                {
                    "app_id": app_id,
                    "pushkey": pushkey,
                    "data": {"default_payload": {"z": blob_b64}},
                }
            ],
        }
    }


def apns_payload_size(blob_b64: str) -> int:
    return len(f'{{"z":"{blob_b64}","event_id":"{CONSTANT_EVENT_ID}"}}'.encode())


class SygnalSender:
    def __init__(self, api: ModuleApi, timeouts: Internals, notify_url: str) -> None:
        self._api = api
        self._timeouts = timeouts
        self._url = notify_url

    async def send(self, app_id: str, pushkey: str, blob_b64: str, trigger_ts: int) -> SendResult:
        if apns_payload_size(blob_b64) >= APNS_PAYLOAD_LIMIT:
            logger.warning("zuno_push: VoIP payload over the APNs limit, not sent")
            metrics.VOIP_SENDS.labels(result="too_large").inc()
            return SendResult("error", None, 0)
        body = notify_body(app_id, pushkey, blob_b64)
        attempts = 0
        with metrics.VOIP_SEND_SECONDS.time():
            while True:
                attempts += 1
                attempt_start = self._api.get_current_time_msec()
                try:
                    response = await self._timeouts.within(
                        TIMEOUT_S, self._api.http_client.post_json_get_json, self._url, body
                    )
                except defer.TimeoutError:
                    logger.warning("zuno_push: Sygnal timed out; not retried")
                    metrics.VOIP_SENDS.labels(result="timeout").inc()
                    return SendResult("error", None, attempts)
                except Exception as e:
                    status = getattr(e, "code", None)
                    status = status if isinstance(status, int) else None
                    now = self._api.get_current_time_msec()
                    elapsed = now - attempt_start
                    if status == 504:
                        logger.warning("zuno_push: Sygnal request timed out; not retried")
                        metrics.VOIP_SENDS.labels(result="timeout").inc()
                        return SendResult("error", None, attempts)
                    retryable = (
                        status is None
                        or status == 500
                        or (status == 502 and elapsed < IMMEDIATE_MS)
                    )
                    in_window = now + RETRY_DELAY_S * 1000 <= trigger_ts + RETRY_WINDOW_MS
                    if retryable and attempts == 1 and in_window:
                        logger.warning(
                            "zuno_push: Sygnal answered %s; retrying once", status or "nothing"
                        )
                        metrics.VOIP_SENDS.labels(result="retried").inc()
                        await self._api.sleep(RETRY_DELAY_S)
                        continue
                    logger.warning(
                        "zuno_push: Sygnal answered %s; ring failed", status or "nothing"
                    )
                    metrics.VOIP_SENDS.labels(result="failed" if status else "unreachable").inc()
                    return SendResult("failed" if status else "error", status, attempts)
                rejected = response.get("rejected", []) if isinstance(response, dict) else []
                if pushkey in rejected:
                    metrics.VOIP_SENDS.labels(result="rejected").inc()
                    return SendResult("rejected", 200, attempts)
                metrics.VOIP_SENDS.labels(result="sent").inc()
                metrics.RING_LATENCY_SECONDS.observe(
                    max(0, self._api.get_current_time_msec() - trigger_ts) / 1000
                )
                return SendResult("sent", 200, attempts)

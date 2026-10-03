from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

RING_DECISIONS = Counter(
    "zuno_push_ring_decisions_total", "Ring decisions per target device", ["decision"]
)
VOIP_SENDS = Counter("zuno_push_voip_sends_total", "VoIP sends to Sygnal by result", ["result"])
VOIP_SEND_SECONDS = Histogram(
    "zuno_push_voip_send_seconds",
    "Duration of one Sygnal /notify call including its retry",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16),
)
RING_LATENCY_SECONDS = Histogram(
    "zuno_push_ring_latency_seconds",
    "From the call event's received_ts to Sygnal accepting the ring",
    buckets=(0.25, 0.5, 1, 2, 3, 5, 10, 30),
)
POLLER_LAG_SECONDS = Histogram(
    "zuno_push_poller_lag_seconds",
    "From the call event's received_ts to the start of rule evaluation",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10),
)
POLLER_ERRORS = Counter("zuno_push_poller_errors_total", "Poller ticks that failed")
POLLER_SKIPPED = Counter(
    "zuno_push_poller_skipped_total", "Call events skipped after repeated failures"
)
FETCHES = Counter("zuno_push_fetch_total", "nse/fetch replies", ["status", "reason"])
RING_STATUS = Counter("zuno_push_ring_status_total", "ring/status replies", ["status"])
TEST_ALERTS = Counter("zuno_push_test_alerts_total", "POST test results", ["result"])
RATE_LIMITED = Counter(
    "zuno_push_rate_limited_total", "Requests or rings refused by a limit", ["limit"]
)
CANARY = Counter("zuno_push_canary_total", "Canary runs by result", ["result"])
CANARY_LAST_SUCCESS = Gauge(
    "zuno_push_canary_last_success_timestamp_seconds", "Unix time of the last good canary"
)
CANARY_SEND_SECONDS = Gauge(
    "zuno_push_canary_send_seconds", "Duration of the last canary VoIP send"
)
FEATURE_ENABLED = Gauge(
    "zuno_push_feature_enabled", "1 when a zuno_push feature is serving", ["feature"]
)

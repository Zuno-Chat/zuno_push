from __future__ import annotations

from synapse.module_api.errors import SynapseError

INVALID_PARAM = "M_INVALID_PARAM"
NOT_JSON = "M_NOT_JSON"
LIMIT_EXCEEDED = "M_LIMIT_EXCEEDED"
BAD_CREDENTIAL = "IM.ZUNO.BAD_CREDENTIAL"
PUSH_DISABLED = "IM.ZUNO.PUSH_DISABLED"
NOT_PUSHER_INSTANCE = "IM.ZUNO.NOT_PUSHER_INSTANCE"


def invalid_param(message: str) -> SynapseError:
    return SynapseError(400, message, INVALID_PARAM)


def not_json() -> SynapseError:
    return SynapseError(400, "Body is not a JSON object", NOT_JSON)


def bad_credential() -> SynapseError:
    return SynapseError(401, "Credential rejected", BAD_CREDENTIAL)


def rate_limited(retry_after_ms: int) -> SynapseError:
    return SynapseError(
        429,
        "Too many requests",
        LIMIT_EXCEEDED,
        additional_fields={"retry_after_ms": retry_after_ms},
    )


def push_disabled(feature: str) -> SynapseError:
    return SynapseError(503, f"Push feature '{feature}' is disabled", PUSH_DISABLED)


def not_pusher_instance() -> SynapseError:
    return SynapseError(503, "This process sends no pushes for this device", NOT_PUSHER_INSTANCE)

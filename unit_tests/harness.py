from __future__ import annotations

from typing import Any

from synapse.module_api.errors import SynapseError

from unit_tests.fake_internals import FakeInternals
from unit_tests.fakes import FakeModuleApi, FakeRequest, run
from zuno_push.config import FEATURES, parse_config
from zuno_push.services import Services, build_services

SYGNAL_URL = "http://sygnal.internal:5000/_matrix/push/v1/notify"


def services_for(api: FakeModuleApi, internals: FakeInternals, **config: Any) -> Services:
    parsed = parse_config({"sygnal_notify_url": SYGNAL_URL, **config})
    assert parsed.error is None, parsed.error
    services = build_services(api, parsed, internals)
    services.gate.set_ready({f: True for f in FEATURES}, parsed.disabled_features)
    return services


def render(resource: Any, request: FakeRequest) -> tuple[int, dict[str, Any]]:
    method = request.method.decode("ascii")
    return run(getattr(resource, f"_async_render_{method}")(request))


def render_error(resource: Any, request: FakeRequest) -> tuple[int, str, dict[str, Any]]:
    try:
        render(resource, request)
    except SynapseError as e:
        return e.code, e.errcode, e.error_dict(None)
    raise AssertionError("expected a SynapseError")

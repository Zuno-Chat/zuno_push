from __future__ import annotations

import logging
from typing import Any

import synapse
from synapse.module_api import ModuleApi

from zuno_push.config import ZunoPushConfig, parse_config
from zuno_push.features import run_probes
from zuno_push.http import BASE_PATH
from zuno_push.janitor import INTERVAL_MS as JANITOR_INTERVAL_MS
from zuno_push.records import Internals
from zuno_push.schema import migrate
from zuno_push.services import Services, build_services

logger = logging.getLogger("zuno_push")

TESTED_SYNAPSE_VERSION = "1.162.0"
POLL_INTERVAL_MS = 250


class ZunoPush:
    def __init__(
        self, config: ZunoPushConfig, api: ModuleApi, internals: Internals | None = None
    ) -> None:
        self._api = api
        self.services: Services | None = None
        try:
            if config.error:
                raise ValueError(f"invalid config: {config.error}")
            if internals is None:
                from zuno_push.internals import SynapseInternals

                internals = SynapseInternals(api)
            services = build_services(api, config, internals)
            routes = self._routes(services)
        except Exception:
            logger.error("zuno_push disabled: initialisation failed", exc_info=True)
            return
        self.services = services
        for path, resource in routes.items():
            api.register_web_resource(path, resource)
        self._register_cleanup(api)
        if synapse.__version__ != TESTED_SYNAPSE_VERSION:
            logger.warning(
                "zuno_push: contract tests ran against Synapse %s, this is %s",
                TESTED_SYNAPSE_VERSION,
                synapse.__version__,
            )
        api.run_as_background_process("zuno_push_start", self._start)

    @staticmethod
    def parse_config(config: dict[str, Any]) -> ZunoPushConfig:
        return parse_config(config)

    @staticmethod
    def _routes(services: Services) -> dict[str, Any]:
        from zuno_push.alert_api import AlertResource
        from zuno_push.health_api import HealthResource
        from zuno_push.nse_api import CredentialResource, FetchResource
        from zuno_push.ring_status_api import RingStatusResource
        from zuno_push.voip_api import DeviceResource, VoipResource

        return {
            f"{BASE_PATH}/voip": VoipResource(services),
            f"{BASE_PATH}/device": DeviceResource(services),
            f"{BASE_PATH}/health": HealthResource(services),
            f"{BASE_PATH}/test": AlertResource(services),
            f"{BASE_PATH}/nse/credential": CredentialResource(services),
            f"{BASE_PATH}/nse/fetch": FetchResource(services),
            f"{BASE_PATH}/ring/status": RingStatusResource(services),
        }

    def _register_cleanup(self, api: ModuleApi) -> None:
        try:
            api.register_password_auth_provider_callbacks(on_logged_out=self.on_logged_out)
        except Exception:
            logger.warning("zuno_push: on_logged_out unavailable; the janitor removes stale rows")
        api.register_third_party_rules_callbacks(
            on_user_deactivation_status_changed=self.on_user_deactivation_status_changed
        )

    async def on_logged_out(self, user_id: str, device_id: str | None, access_token: str) -> None:
        if self.services is None or device_id is None:
            return
        try:
            await self.services.store.delete_device(user_id, device_id)
        except Exception as e:
            logger.warning("zuno_push: logout cleanup failed: %s", type(e).__name__)

    async def on_user_deactivation_status_changed(
        self, user_id: str, deactivated: bool, by_admin: bool
    ) -> None:
        if self.services is None or not deactivated:
            return
        try:
            await self.services.store.delete_user(user_id)
        except Exception as e:
            logger.warning("zuno_push: deactivation cleanup failed: %s", type(e).__name__)

    async def _start(self) -> None:
        services = self.services
        if services is None:
            return
        try:
            await migrate(self._api)
        except Exception:
            logger.error("zuno_push disabled: schema migration failed", exc_info=True)
            services.gate.disable_all()
            return
        results = await run_probes(services.internals.probes())
        services.gate.set_ready(results, services.config.disabled_features)
        if not self._api.should_run_background_tasks():
            return
        self._api.looping_background_call(
            services.janitor.run, JANITOR_INTERVAL_MS, desc="zuno_push_janitor"
        )
        if not services.gate.enabled("ring"):
            return
        self._api.looping_background_call(
            services.poller.tick, POLL_INTERVAL_MS, desc="zuno_push_poll"
        )
        services.ringer.kick()
        if services.canary is not None:
            self._api.looping_background_call(
                services.canary.run, services.config.canary.interval_ms, desc="zuno_push_canary"
            )

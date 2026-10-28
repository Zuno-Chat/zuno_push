from __future__ import annotations

import base64
from typing import Any
from unittest.mock import Mock

from synapse.rest.client import account, logout
from synapse.server import HomeServer
from synapse.util.clock import Clock
from tests.server import FakeChannel
from tests.zuno_push_contract.base import ContractTestCase
from twisted.internet import defer
from twisted.internet.testing import MemoryReactor
from twisted.web.resource import Resource

from zuno_push import ZunoPush

BASE = "/_synapse/client/zuno/push/v1"
SYGNAL = "http://sygnal.internal:5000/_matrix/push/v1/notify"
VOIP_KEY = bytes(range(32))


class ModuleTestCase(ContractTestCase):
    servlets = ContractTestCase.servlets + [account.register_servlets, logout.register_servlets]
    module_config: dict[str, Any] = {"sygnal_notify_url": SYGNAL}

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.sygnal_requests: list[dict[str, Any]] = []
        self.sygnal_rejects: set[str] = set()
        client = Mock()

        def post_json_get_json(uri: str, body: dict[str, Any], headers: Any = None) -> Any:
            self.sygnal_requests.append(body)
            pushkey = body["notification"]["devices"][0]["pushkey"]
            return defer.succeed({"rejected": [pushkey] if pushkey in self.sygnal_rejects else []})

        client.post_json_get_json = post_json_get_json
        hs = self.setup_test_homeserver(simple_http_client=client)
        self.module = ZunoPush(ZunoPush.parse_config(self.module_config), hs.get_module_api())
        return hs

    def create_resource_dict(self) -> dict[str, Resource]:
        resources = super().create_resource_dict()
        resources.update(self.hs._module_web_resources)
        return resources

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.pump(0.1)

    def call(
        self,
        method: str,
        path: str,
        body: Any = b"",
        token: str | None = None,
        auth: str | None = None,
    ) -> FakeChannel:
        headers = [("Authorization", auth)] if auth else None
        return self.make_request(
            method,
            f"{BASE}/{path}",
            body,
            access_token=token,
            custom_headers=headers,
            shorthand=False,
        )

    def register_voip(
        self,
        token: str,
        kid: int = 1,
        pushkey: str = base64.b64encode(b"token").decode(),
        app_id: str = "im.zuno.chat.ios.voip",
    ) -> FakeChannel:
        return self.call(
            "PUT",
            "voip",
            {
                "app_id": app_id,
                "pushkey": pushkey,
                "kid": kid,
                "key": base64.b64encode(VOIP_KEY).decode(),
            },
            token=token,
        )

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from synapse.rest import admin
from synapse.rest.client import account_data, login, push_rule, pusher, receipts, room
from synapse.server import HomeServer
from synapse.util.clock import Clock
from tests.unittest import HomeserverTestCase
from twisted.internet.testing import MemoryReactor

from zuno_push.internals import SynapseInternals

ENCRYPTION = {
    "type": "m.room.encryption",
    "state_key": "",
    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
}


class ContractTestCase(HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        receipts.register_servlets,
        push_rule.register_servlets,
        pusher.register_servlets,
        account_data.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.api = hs.get_module_api()
        self.internals = SynapseInternals(self.api)

    def user(self, name: str) -> tuple[str, str]:
        user_id = self.register_user(name, "pass")
        return user_id, self.login(name, "pass")

    def device_of(self, token: str) -> str:
        info = self.get_success(self.store.get_user_by_access_token(token))
        assert info is not None and info.device_id is not None
        return info.device_id

    def room_with(self, owner: tuple[str, str], *members: tuple[str, str], **extra: Any) -> str:
        room_id = self.helper.create_room_as(owner[0], tok=owner[1], extra_content=extra or None)
        for user_id, token in members:
            self.helper.invite(room_id, owner[0], user_id, tok=owner[1])
            self.helper.join(room_id, user_id, tok=token)
        return room_id

    def state(self, room_id: str) -> dict[tuple[str, str], Any]:
        return self.get_success(
            self.api.get_room_state(room_id, [("m.room.create", ""), ("m.room.power_levels", "")])
        )

    def put(self, path: str, body: dict[str, Any], token: str) -> None:
        channel = self.make_request("PUT", path, body, access_token=token)
        self.assertEqual(channel.code, 200, channel.json_body)

    def push_rule(self, token: str, kind: str, rule_id: str, body: dict[str, Any]) -> None:
        self.put(f"/_matrix/client/v3/pushrules/global/{kind}/{quote(rule_id)}", body, token)

    def push_rule_enabled(self, token: str, kind: str, rule_id: str, enabled: bool) -> None:
        self.put(
            f"/_matrix/client/v3/pushrules/global/{kind}/{quote(rule_id)}/enabled",
            {"enabled": enabled},
            token,
        )

    def account_data(
        self, user_id: str, token: str, data_type: str, content: dict[str, Any]
    ) -> None:
        self.put(
            f"/_matrix/client/v3/user/{quote(user_id)}/account_data/{data_type}", content, token
        )

    def receipt(
        self, room_id: str, event_id: str, token: str, thread_id: str | None = None
    ) -> None:
        body = {"thread_id": thread_id} if thread_id else {}
        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/rooms/{room_id}/receipt/m.read/{event_id}",
            body,
            access_token=token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

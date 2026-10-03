from __future__ import annotations

import urllib.request

from tests.server import FakeChannel
from tests.unittest import override_config
from tests.zuno_push_contract.module_base import BASE, ModuleTestCase

from zuno_push.smoke import Reply, run


class HttpContractTests(ModuleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.alice = self.user("alice")

    def assert_module_reply(
        self, channel: FakeChannel, code: int, errcode: str | None = None
    ) -> None:
        self.assertEqual(channel.code, code, channel.json_body)
        self.assertEqual(channel.headers.getRawHeaders(b"X-Zuno-Push"), [b"1"])
        if errcode is not None:
            self.assertEqual(channel.json_body["errcode"], errcode)
        else:
            self.assertIsInstance(channel.json_body["server_ts"], int)

    def test_success_and_every_error_carry_the_header(self) -> None:
        self.assert_module_reply(self.register_voip(self.alice[1]), 200)
        health = self.call("GET", "health", token=self.alice[1])
        self.assert_module_reply(health, 200)
        self.assertEqual(set(health.json_body), {"pushers", "voip", "nse", "server_ts"})
        self.assertEqual(health.json_body["voip"]["kid"], 1)
        self.assertEqual(
            health.json_body["nse"], {"credential_expires_ts": None, "last_fetch_ts": None}
        )
        self.assert_module_reply(self.call("PUT", "voip", {"app_id": "x"}), 401, "M_MISSING_TOKEN")
        self.assert_module_reply(
            self.call("PUT", "voip", {"app_id": "x"}, token="nope"), 401, "M_UNKNOWN_TOKEN"
        )
        self.assert_module_reply(
            self.call("PUT", "voip", b"{", token=self.alice[1]), 400, "M_NOT_JSON"
        )
        self.assert_module_reply(
            self.call("PUT", "voip", {"app_id": "x"}, token=self.alice[1]), 400, "M_INVALID_PARAM"
        )
        self.assert_module_reply(
            self.call(
                "POST",
                "nse/fetch",
                {"room_id": "!r:test", "event_id": "$e"},
                auth="ZunoNotify " + "A" * 43,
            ),
            401,
            "IM.ZUNO.BAD_CREDENTIAL",
        )
        self.assert_module_reply(
            self.call("GET", "voip", token=self.alice[1]), 405, "M_UNRECOGNIZED"
        )
        for _ in range(8):
            self.register_voip(self.alice[1])
        limited = self.register_voip(self.alice[1])
        self.assert_module_reply(limited, 429, "M_LIMIT_EXCEEDED")
        self.assertGreater(limited.json_body["retry_after_ms"], 0)

    def test_unrouted_path_is_synapses_404_without_the_header(self) -> None:
        channel = self.make_request("GET", f"{BASE}/nope", shorthand=False)
        self.assertEqual((channel.code, channel.json_body["errcode"]), (404, "M_UNRECOGNIZED"))
        self.assertIsNone(channel.headers.getRawHeaders(b"X-Zuno-Push"))

    def test_credential_then_fetch_gone_for_an_unknown_event(self) -> None:
        minted = self.call("POST", "nse/credential", {}, token=self.alice[1])
        self.assert_module_reply(minted, 200)
        self.assertEqual(len(minted.json_body["credential"]), 43)
        fetched = self.call(
            "POST",
            "nse/fetch",
            {"room_id": "!r:test", "event_id": "$e"},
            auth=f"ZunoNotify {minted.json_body['credential']}",
        )
        self.assert_module_reply(fetched, 200)
        self.assertEqual(fetched.json_body["status"], "gone")
        self.assertEqual(fetched.json_body["missed"], [])

    def test_logout_removes_the_device_rows(self) -> None:
        self.register_voip(self.alice[1])
        device = self.device_of(self.alice[1])
        self.assertIsNotNone(
            self.get_success(self.module.services.store.get_device(self.alice[0], device))
        )
        channel = self.make_request(
            "POST", "/_matrix/client/v3/logout", {}, access_token=self.alice[1]
        )
        self.assertEqual(channel.code, 200)
        self.assertIsNone(
            self.get_success(self.module.services.store.get_device(self.alice[0], device))
        )

    def test_deactivation_removes_every_row(self) -> None:
        self.register_voip(self.alice[1])
        device = self.device_of(self.alice[1])
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/account/deactivate",
            {"auth": {"type": "m.login.password", "user": self.alice[0], "password": "pass"}},
            access_token=self.alice[1],
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertIsNone(
            self.get_success(self.module.services.store.get_device(self.alice[0], device))
        )

    def test_smoke_checks_pass_against_the_real_module(self) -> None:
        def fetch(request: urllib.request.Request) -> Reply:
            channel = self.make_request(
                request.get_method(),
                request.full_url.removeprefix("https://hs.invalid"),
                request.data or b"",
                custom_headers=[("Authorization", request.get_header("Authorization"))],
                shorthand=False,
            )
            headers = {
                k.decode().lower(): v[0].decode() for k, v in channel.headers.getAllRawHeaders()
            }
            return Reply(channel.code, headers, channel.result["body"])

        self.assertEqual(run("https://hs.invalid", self.alice[1], fetch), [])

    @override_config({"pusher_instances": ["pusher1"]})
    def test_test_alert_off_the_pusher_instance(self) -> None:
        self.assert_module_reply(
            self.call("POST", "test", {}, token=self.alice[1]), 503, "IM.ZUNO.NOT_PUSHER_INSTANCE"
        )


class DisabledModuleTests(ModuleTestCase):
    module_config = {"sygnal_notify_url": "not a url"}

    def test_a_broken_config_registers_nothing(self) -> None:
        alice = self.user("alice")
        self.assertIsNone(self.module.services)
        channel = self.register_voip(alice[1])
        self.assertEqual(channel.code, 404)
        self.assertIsNone(channel.headers.getRawHeaders(b"X-Zuno-Push"))

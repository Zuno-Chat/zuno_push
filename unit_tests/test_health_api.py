import base64

from twisted.internet import task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals
from unit_tests.fakes import FakeModuleApi, FakeRequest, run
from unit_tests.harness import render, render_error, services_for
from zuno_push.health_api import HealthResource
from zuno_push.records import PusherRow

NOW = 1_790_000_000_000


class HealthApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(NOW / 1000)
        self.api = FakeModuleApi(self.clock)
        self.api.tokens["tok"] = ("@alice:test", "PHONE")
        self.internals = FakeInternals(self.api)
        self.services = services_for(self.api, self.internals)
        self.health = HealthResource(self.services)

    def get(self) -> FakeRequest:
        return FakeRequest(b"GET", token="tok")

    def test_unknown_device(self) -> None:
        self.assertEqual(
            render(self.health, self.get()),
            (
                200,
                {
                    "pushers": [],
                    "voip": {
                        "registered": False,
                        "kid": None,
                        "last_result": None,
                        "last_ts": None,
                    },
                    "nse": {"credential_expires_ts": None, "last_fetch_ts": None},
                    "server_ts": NOW,
                },
            ),
        )

    def test_full_report(self) -> None:
        self.internals.pushers[("@alice:test", "PHONE")] = [
            PusherRow("im.zuno.chat.ios", NOW - 5, None)
        ]
        key = base64.b64encode(bytes(32)).decode()
        run(
            self.services.store.upsert_voip(
                "@alice:test", "PHONE", "im.zuno.chat.ios.voip", "pk", 16909060, key
            )
        )
        run(self.services.store.record_send("@alice:test", "PHONE", "pk", "sent", NOW - 9))
        run(
            self.services.store.set_credential(
                "@alice:test", "PHONE", "h", NOW + 100, NOW - 50, None, None
            )
        )
        run(self.services.store.touch_fetch("@alice:test", "PHONE", NOW - 7))
        status, body = render(self.health, self.get())
        self.assertEqual(
            body["pushers"],
            [{"app_id": "im.zuno.chat.ios", "last_success_ts": NOW - 5, "failing_since_ts": None}],
        )
        self.assertEqual(
            body["voip"],
            {"registered": True, "kid": 16909060, "last_result": "sent", "last_ts": NOW - 9},
        )
        self.assertEqual(
            body["nse"], {"credential_expires_ts": NOW + 100, "last_fetch_ts": NOW - 7}
        )

    def test_expired_credential_is_reported_as_none(self) -> None:
        run(
            self.services.store.set_credential(
                "@alice:test", "PHONE", "h", NOW, NOW - 50, None, None
            )
        )
        self.assertIsNone(render(self.health, self.get())[1]["nse"]["credential_expires_ts"])

    def test_limit_and_disabled(self) -> None:
        for _ in range(30):
            render(self.health, self.get())
        self.assertEqual(render_error(self.health, self.get())[:2], (429, "M_LIMIT_EXCEEDED"))
        self.services.gate.set_ready({"health": False}, [])
        self.assertEqual(render_error(self.health, self.get())[:2], (503, "IM.ZUNO.PUSH_DISABLED"))

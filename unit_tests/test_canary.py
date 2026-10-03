import base64
from typing import Any

from prometheus_client import REGISTRY
from twisted.internet import defer, task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals
from unit_tests.fakes import FakeModuleApi, run
from unit_tests.test_sealer import open_blob
from zuno_push.canary import Canary
from zuno_push.config import CanaryConfig
from zuno_push.sender import SygnalSender
from zuno_push.store import PushStore

KEY = bytes(range(32))
NOW = 1_790_000_000_000
CONFIG = CanaryConfig("@canary:test", "CANARY")


class CanaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(NOW / 1000)
        self.api = FakeModuleApi(self.clock)
        self.internals = FakeInternals(self.api)
        self.store = PushStore(self.api)
        sender = SygnalSender(self.api, self.internals, "http://sygnal/_matrix/push/v1/notify")
        self.canary = Canary(self.api, self.internals, self.store, sender, CONFIG)

    def register(self) -> None:
        run(
            self.store.upsert_voip(
                "@canary:test",
                "CANARY",
                "im.zuno.chat.ios.voip",
                "pk",
                3,
                base64.b64encode(KEY).decode(),
            )
        )

    def test_sends_a_sealed_canary_ring_and_a_test_alert(self) -> None:
        self.register()
        self.api.push_results["@canary:test"] = {"CANARY": True}
        before = REGISTRY.get_sample_value("zuno_push_canary_total", {"result": "ok"}) or 0
        self.assertEqual(run(self.canary.run()), "ok")
        device = self.api.http_client.requests[0][1]["notification"]["devices"][0]
        _, _, plain = open_blob(KEY, base64.b64decode(device["data"]["default_payload"]["z"]))
        self.assertEqual(
            plain,
            {
                "room": "",
                "call": f"canary-{NOW}",
                "caller": "",
                "cname": "",
                "rname": "",
                "kind": "canary",
                "ts": NOW,
                "rts": NOW,
            },
        )
        self.assertEqual(
            self.api.pushes,
            [("@canary:test", "CANARY", {"event_id": f"$zuno_test_{NOW}", "prio": "high"})],
        )
        self.assertEqual(
            REGISTRY.get_sample_value("zuno_push_canary_total", {"result": "ok"}), before + 1
        )
        self.assertEqual(
            REGISTRY.get_sample_value("zuno_push_canary_last_success_timestamp_seconds"), NOW / 1000
        )

    def test_unregistered_canary_device(self) -> None:
        self.assertEqual(run(self.canary.run()), "unregistered")
        self.assertEqual(self.api.http_client.requests, [])

    def test_slow_send_is_reported(self) -> None:
        self.register()
        gate: defer.Deferred = defer.Deferred()

        def slow() -> Any:
            return gate

        self.api.http_client.responses.append(slow)
        d = defer.ensureDeferred(self.canary.run())
        self.clock.advance(2.5)
        gate.callback({"rejected": []})
        self.assertEqual(self.successResultOf(d), "slow")

    def test_alert_skipped_off_the_pusher_instance(self) -> None:
        self.register()
        self.internals.pusher_instance = False
        run(self.canary.run())
        self.assertEqual(self.api.pushes, [])

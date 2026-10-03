from twisted.internet import defer, task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals
from unit_tests.fakes import FakeModuleApi, FakeRequest, FakeUserInfo, run
from unit_tests.harness import render, render_error, services_for
from zuno_push.nse_api import CredentialResource
from zuno_push.ring_status_api import RingStatusResource, describe
from zuno_push.store import CallRow, RingRow

NOW = 1_790_000_000_000
ROOM = "!room:test"


class DescribeTests(unittest.TestCase):
    def test_every_state(self) -> None:
        self.assertEqual(
            describe(None, False), {"status": "pending", "sent_ts": None, "rule": None}
        )
        self.assertEqual(
            describe(None, True), {"status": "no_token", "sent_ts": None, "rule": None}
        )
        self.assertEqual(
            describe(("sent", 5), True), {"status": "sent", "sent_ts": 5, "rule": None}
        )
        self.assertEqual(
            describe(("suppressed:no_notify", 5), True),
            {"status": "suppressed", "sent_ts": None, "rule": "no_notify"},
        )
        self.assertEqual(describe(("failed", 5), True)["status"], "failed")
        self.assertEqual(describe(("no_token", 5), True)["status"], "no_token")
        self.assertEqual(describe(("pending", 5), True)["status"], "pending")


class RingStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(NOW / 1000)
        self.api = FakeModuleApi(self.clock)
        self.api.tokens["tok"] = ("@bob:test", "PHONE")
        self.api.userinfo["@bob:test"] = FakeUserInfo()
        self.internals = FakeInternals(self.api)
        self.internals.devices.add(("@bob:test", "PHONE"))
        self.services = services_for(self.api, self.internals)
        self.status = RingStatusResource(self.services)
        self.credential = render(
            CredentialResource(self.services), FakeRequest(b"POST", {}, token="tok")
        )[1]["credential"]

    def request(self, wait_ms: int = 0, call_id: str = "c1") -> FakeRequest:
        return FakeRequest(
            b"POST",
            {"room_id": ROOM, "call_id": call_id, "wait_ms": wait_ms},
            auth=f"ZunoNotify {self.credential}",
        )

    def create(self, state: str) -> None:
        run(
            self.services.store.create_call_with_rings(
                CallRow(ROOM, "c1", "@alice:test", "voice", NOW, "", ""),
                [RingRow("@bob:test", "PHONE", state, False)],
                NOW,
            )
        )

    def test_sent(self) -> None:
        self.create("pending")
        run(self.services.store.set_ring_state(ROOM, "c1", "@bob:test", "PHONE", "sent", NOW + 500))
        self.assertEqual(
            render(self.status, self.request())[1],
            {"status": "sent", "sent_ts": NOW + 500, "rule": None, "server_ts": NOW},
        )

    def test_suppressed_rule(self) -> None:
        self.create("suppressed:not_allowed")
        self.assertEqual(render(self.status, self.request())[1]["rule"], "not_allowed")

    def test_unseen_call_is_pending_and_seen_call_without_row_is_no_token(self) -> None:
        self.assertEqual(render(self.status, self.request())[1]["status"], "pending")
        run(
            self.services.store.create_call_with_rings(
                CallRow(ROOM, "c1", "@alice:test", "voice", NOW, "", ""), [], NOW
            )
        )
        self.assertEqual(render(self.status, self.request())[1]["status"], "no_token")

    def test_long_poll_returns_as_soon_as_the_state_changes(self) -> None:
        self.create("pending")
        d = defer.ensureDeferred(self.status._async_render_POST(self.request(wait_ms=8000)))
        self.clock.advance(1)
        self.assertNoResult(d)
        run(
            self.services.store.set_ring_state(ROOM, "c1", "@bob:test", "PHONE", "sent", NOW + 1000)
        )
        self.clock.advance(0.25)
        self.assertEqual(self.successResultOf(d)[1]["status"], "sent")

    def test_long_poll_gives_up_at_wait_ms(self) -> None:
        d = defer.ensureDeferred(self.status._async_render_POST(self.request(wait_ms=1000)))
        for _ in range(4):
            self.clock.advance(0.25)
        status, body = self.successResultOf(d)
        self.assertEqual((body["status"], body["server_ts"]), ("pending", NOW + 1000))

    def test_wait_ms_bounds(self) -> None:
        for wait_ms in (-1, 10_001):
            self.assertEqual(
                render_error(self.status, self.request(wait_ms=wait_ms))[:2],
                (400, "M_INVALID_PARAM"),
            )

    def test_thirty_a_minute(self) -> None:
        for _ in range(30):
            render(self.status, self.request())
        self.assertEqual(render_error(self.status, self.request())[:2], (429, "M_LIMIT_EXCEEDED"))

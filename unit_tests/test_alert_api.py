from twisted.internet import task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals
from unit_tests.fakes import FakeModuleApi, FakeRequest
from unit_tests.harness import render, render_error, services_for
from zuno_push.alert_api import AlertResource

NOW = 1_790_000_000_000


class AlertApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(NOW / 1000)
        self.api = FakeModuleApi(self.clock)
        self.api.tokens["tok"] = ("@alice:test", "PHONE")
        self.internals = FakeInternals(self.api)
        self.alert = AlertResource(services_for(self.api, self.internals))

    def post(self) -> FakeRequest:
        return FakeRequest(b"POST", {}, token="tok")

    def test_sends_through_synapses_pusher_for_this_device(self) -> None:
        self.api.push_results["@alice:test"] = {"PHONE": True}
        self.assertEqual(
            render(self.alert, self.post()),
            (200, {"event_id": f"$zuno_test_{NOW}", "server_ts": NOW}),
        )
        self.assertEqual(
            self.api.pushes,
            [("@alice:test", "PHONE", {"event_id": f"$zuno_test_{NOW}", "prio": "high"})],
        )

    def test_gateway_failure_still_answers_200(self) -> None:
        self.api.push_results["@alice:test"] = {"PHONE": False}
        self.assertEqual(render(self.alert, self.post())[0], 200)

    def test_not_the_pusher_instance(self) -> None:
        self.internals.pusher_instance = False
        self.assertEqual(
            render_error(self.alert, self.post())[:2], (503, "IM.ZUNO.NOT_PUSHER_INSTANCE")
        )
        self.assertEqual(self.api.pushes, [])

    def test_no_pusher_for_this_device(self) -> None:
        self.api.push_results["@alice:test"] = {}
        self.assertEqual(
            render_error(self.alert, self.post())[:2], (503, "IM.ZUNO.NOT_PUSHER_INSTANCE")
        )

    def test_three_an_hour(self) -> None:
        self.api.push_results["@alice:test"] = {"PHONE": True}
        for _ in range(3):
            render(self.alert, self.post())
        self.assertEqual(render_error(self.alert, self.post())[:2], (429, "M_LIMIT_EXCEEDED"))

    def test_empty_body_is_accepted(self) -> None:
        self.api.push_results["@alice:test"] = {"PHONE": True}
        self.assertEqual(render(self.alert, FakeRequest(b"POST", b"", token="tok"))[0], 200)

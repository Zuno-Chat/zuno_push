from typing import Any

from synapse.api.errors import HttpResponseException
from twisted.internet import defer, task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals
from unit_tests.fakes import FakeModuleApi
from zuno_push.sender import CONSTANT_EVENT_ID, SygnalSender, notify_body, payload_size

URL = "http://sygnal.internal:5000/_matrix/push/v1/notify"
BLOB = "A" * 732


def failing(status: int) -> Any:
    def respond() -> Any:
        raise HttpResponseException(status, "error", b"")

    return respond


class SenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(1000)
        self.api = FakeModuleApi(self.clock)
        self.sender = SygnalSender(self.api, FakeInternals(self.api), URL)
        self.now = self.api.get_current_time_msec()

    def send(self, trigger_ts: int | None = None) -> defer.Deferred:
        return defer.ensureDeferred(
            self.sender.send(
                "im.zuno.chat.ios.voip",
                "cGs=",
                BLOB,
                self.now if trigger_ts is None else trigger_ts,
            )
        )

    def test_request_shape_has_no_room_type_or_counts(self) -> None:
        result = self.successResultOf(self.send())
        self.assertEqual((result.outcome, result.attempts), ("sent", 1))
        url, body = self.api.http_client.requests[0]
        self.assertEqual(url, URL)
        self.assertEqual(body, notify_body("im.zuno.chat.ios.voip", "cGs=", BLOB))
        notification = body["notification"]
        self.assertEqual(notification["event_id"], CONSTANT_EVENT_ID)
        for absent in ("room_id", "type", "counts", "sender"):
            self.assertNotIn(absent, notification)
        self.assertEqual(notification["devices"][0]["data"], {"default_payload": {"z": BLOB}})

    def test_rejected_pushkey(self) -> None:
        self.api.http_client.responses.append(lambda: {"rejected": ["cGs="]})
        self.assertEqual(self.successResultOf(self.send()).outcome, "rejected")

    def test_500_is_retried_once_after_one_second(self) -> None:
        self.api.http_client.responses.extend([failing(500), lambda: {"rejected": []}])
        d = self.send()
        self.assertNoResult(d)
        self.clock.advance(1)
        result = self.successResultOf(d)
        self.assertEqual((result.outcome, result.attempts), ("sent", 2))

    def test_second_failure_is_final(self) -> None:
        self.api.http_client.responses.extend([failing(502), failing(500)])
        d = self.send()
        self.clock.advance(1)
        result = self.successResultOf(d)
        self.assertEqual((result.outcome, result.status, result.attempts), ("error", 500, 2))

    def test_no_retry_outside_the_ten_second_window(self) -> None:
        self.api.http_client.responses.append(failing(500))
        result = self.successResultOf(self.send(trigger_ts=self.now - 9_500))
        self.assertEqual((result.outcome, result.attempts), ("error", 1))

    def test_400_is_not_retried(self) -> None:
        self.api.http_client.responses.append(failing(400))
        result = self.successResultOf(self.send())
        self.assertEqual((result.outcome, result.attempts), ("error", 1))

    def test_timeout_after_four_seconds_is_never_retried(self) -> None:
        self.api.http_client.responses.append(lambda: defer.Deferred())
        d = self.send()
        self.clock.advance(4.1)
        result = self.successResultOf(d)
        self.assertEqual((result.outcome, result.attempts), ("error", 1))
        self.assertEqual(len(self.api.http_client.requests), 1)

    def test_connection_error_is_retried(self) -> None:
        def refuse() -> Any:
            raise ConnectionRefusedError()

        self.api.http_client.responses.extend([refuse, lambda: {"rejected": []}])
        d = self.send()
        self.clock.advance(1)
        self.assertEqual(self.successResultOf(d).outcome, "sent")
        self.api.http_client.responses.extend([refuse, refuse])
        d = self.send()
        self.clock.advance(1)
        self.assertEqual(self.successResultOf(d).outcome, "error")

    def test_oversized_payload_is_not_sent(self) -> None:
        big = "A" * 4100
        self.assertGreaterEqual(payload_size(big), 4096)
        result = self.successResultOf(
            defer.ensureDeferred(self.sender.send("app", "cGs=", big, self.now))
        )
        self.assertEqual(result.outcome, "error")
        self.assertEqual(self.api.http_client.requests, [])

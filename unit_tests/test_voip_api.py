import base64

from twisted.internet import task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals
from unit_tests.fakes import FakeModuleApi, FakeRequest, run
from unit_tests.harness import render, render_error, services_for
from zuno_push.voip_api import DeviceResource, VoipResource

TOKEN_B64 = base64.b64encode(bytes(range(32))).decode()
KEY_B64 = base64.b64encode(bytes(range(32, 64))).decode()
BODY = {"app_id": "im.zuno.chat.ios.voip", "pushkey": TOKEN_B64, "kid": 16909060, "key": KEY_B64}
FCM_TOKEN = "dGVzdA-x_y:APA91bH" + "z" * 140
ANDROID = {**BODY, "app_id": "im.zuno.chat.android", "pushkey": FCM_TOKEN}


class VoipApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(1_790_000_000)
        self.api = FakeModuleApi(self.clock)
        self.api.tokens["tok"] = ("@alice:test", "PHONE")
        self.services = services_for(self.api, FakeInternals(self.api))
        self.voip = VoipResource(self.services)
        self.device = DeviceResource(self.services)

    def put(self, body: object) -> FakeRequest:
        return FakeRequest(b"PUT", body, token="tok")

    def test_register_returns_kid_and_server_ts(self) -> None:
        for body in (BODY, ANDROID):
            self.assertEqual(
                render(self.voip, self.put(body)),
                (200, {"kid": 16909060, "server_ts": 1_790_000_000_000}),
            )
            row = run(self.services.store.get_device("@alice:test", "PHONE"))
            self.assertEqual(
                (row.voip_app_id, row.voip_pushkey, row.kid, row.voip_key), tuple(body.values())
            )

    def test_sandbox_app_id_is_accepted(self) -> None:
        self.assertEqual(
            render(self.voip, self.put({**BODY, "app_id": "im.zuno.chat.ios.dev.voip"}))[0], 200
        )

    def test_invalid_bodies(self) -> None:
        for body in (
            {**BODY, "app_id": "im.zuno.chat.ios"},
            {**BODY, "pushkey": "not base64!"},
            {**BODY, "pushkey": TOKEN_B64.rstrip("=")},
            {**BODY, "pushkey": FCM_TOKEN},
            {**ANDROID, "pushkey": ""},
            {**ANDROID, "pushkey": "has space"},
            {**ANDROID, "pushkey": "x" * 513},
            {**ANDROID, "pushkey": 5},
            {**ANDROID, "key": base64.b64encode(bytes(31)).decode()},
            {**BODY, "kid": 0},
            {**BODY, "kid": 4294967296},
            {**BODY, "kid": "1"},
            {**BODY, "kid": True},
            {**BODY, "key": base64.b64encode(bytes(31)).decode()},
            {k: v for k, v in BODY.items() if k != "key"},
        ):
            self.clock.advance(61)
            self.assertEqual(
                render_error(self.voip, self.put(body))[:2], (400, "M_INVALID_PARAM"), body
            )
        self.assertEqual(render_error(self.voip, self.put(b"nope"))[:2], (400, "M_NOT_JSON"))

    def test_bearer_is_required(self) -> None:
        self.assertEqual(
            render_error(self.voip, FakeRequest(b"PUT", BODY))[:2], (401, "M_MISSING_TOKEN")
        )

    def test_ten_registrations_a_minute(self) -> None:
        for _ in range(10):
            render(self.voip, self.put(BODY))
        code, errcode, body = render_error(self.voip, self.put(BODY))
        self.assertEqual((code, errcode), (429, "M_LIMIT_EXCEEDED"))
        self.assertGreater(body["retry_after_ms"], 0)

    def test_disabled_ring_feature(self) -> None:
        self.services.gate.set_ready({"ring": False}, [])
        self.assertEqual(
            render_error(self.voip, self.put(BODY))[:2], (503, "IM.ZUNO.PUSH_DISABLED")
        )
        self.assertEqual(render(self.voip, FakeRequest(b"DELETE", token="tok"))[0], 200)

    def test_delete_voip_keeps_the_credential(self) -> None:
        render(self.voip, self.put(BODY))
        run(
            self.services.store.set_credential(
                "@alice:test", "PHONE", "h", 2_000_000_000_000, 0, None, None
            )
        )
        self.assertEqual(
            render(self.voip, FakeRequest(b"DELETE", token="tok")),
            (200, {"server_ts": 1_790_000_000_000}),
        )
        row = run(self.services.store.get_device("@alice:test", "PHONE"))
        self.assertEqual((row.has_voip, row.cred_hash), (False, "h"))

    def test_delete_device_removes_everything(self) -> None:
        render(self.voip, self.put(BODY))
        self.assertEqual(render(self.device, FakeRequest(b"DELETE", token="tok"))[0], 200)
        self.assertIsNone(run(self.services.store.get_device("@alice:test", "PHONE")))

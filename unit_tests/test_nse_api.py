from twisted.internet import task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals, add_event
from unit_tests.fakes import FakeEvent, FakeModuleApi, FakeRequest, FakeUserInfo, run
from unit_tests.harness import render, render_error, services_for
from zuno_push.credential import LIFETIME_MS, hash_credential
from zuno_push.nse_api import CredentialResource, FetchResource
from zuno_push.records import NotifRow, PushAction

NOW = 1_790_000_000_000
ROOM = "!room:test"


class NseApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(NOW / 1000)
        self.api = FakeModuleApi(self.clock)
        self.api.tokens["tok"] = ("@bob:test", "PHONE")
        self.api.userinfo["@bob:test"] = FakeUserInfo()
        self.internals = FakeInternals(self.api)
        self.internals.devices.add(("@bob:test", "PHONE"))
        self.internals.max_so = 77
        self.internals.receipt_max["@bob:test"] = 12
        self.services = services_for(self.api, self.internals)
        self.mint = CredentialResource(self.services)
        self.fetch = FetchResource(self.services)
        event = FakeEvent(
            type="m.room.encrypted", sender="@alice:test", content={}, room_id=ROOM, event_id="$e1"
        )
        add_event(self.internals, event, NOW - 1000)
        self.internals.actions[("@bob:test", ROOM, "$e1")] = PushAction(
            80, "main", True, False, True
        )

    def minted(self) -> str:
        status, body = render(self.mint, FakeRequest(b"POST", {}, token="tok"))
        self.assertEqual(status, 200)
        self.assertEqual((body["expires_ts"], body["server_ts"]), (NOW + LIFETIME_MS, NOW))
        return body["credential"]

    def post(self, credential: str, body: object) -> FakeRequest:
        return FakeRequest(b"POST", body, auth=f"ZunoNotify {credential}")

    def test_mint_stores_the_hash_and_the_catch_up_floors(self) -> None:
        credential = self.minted()
        row = run(self.services.store.get_device("@bob:test", "PHONE"))
        self.assertEqual(row.cred_hash, hash_credential(credential))
        self.assertEqual((row.floor_so, row.floor_rs), (77, 12))

    def test_mint_without_catchup_leaves_floors_empty(self) -> None:
        self.services.gate.set_ready({"nse": True, "catchup": False}, [])
        self.minted()
        row = run(self.services.store.get_device("@bob:test", "PHONE"))
        self.assertEqual((row.floor_so, row.floor_rs), (None, None))

    def test_mint_limits_per_device_and_per_user(self) -> None:
        for _ in range(10):
            self.minted()
        request = FakeRequest(b"POST", {}, token="tok")
        self.assertEqual(render_error(self.mint, request)[:2], (429, "M_LIMIT_EXCEEDED"))
        for i in range(40):
            self.api.tokens[f"t{i}"] = ("@bob:test", f"D{i}")
            render(self.mint, FakeRequest(b"POST", {}, token=f"t{i}"))
        self.api.tokens["last"] = ("@bob:test", "LAST")
        request = FakeRequest(b"POST", {}, token="last")
        self.assertEqual(render_error(self.mint, request)[:2], (429, "M_LIMIT_EXCEEDED"))

    def test_fetch_reply_has_server_ts_catch_up_and_records_the_fetch(self) -> None:
        credential = self.minted()
        self.internals.notif_rows.append(NotifRow("$e1", ROOM, "main", 80, False, True, NOW - 1000))
        status, body = render(
            self.fetch, self.post(credential, {"room_id": ROOM, "event_id": "$e1"})
        )
        self.assertEqual((status, body["status"], body["server_ts"]), (200, "ok", NOW))
        self.assertEqual((body["missed"], body["read_rooms"]), ([], []))
        self.assertEqual(
            run(self.services.store.get_device("@bob:test", "PHONE")).last_fetch_ts, NOW
        )

    def test_fetch_without_catchup_omits_the_fields(self) -> None:
        credential = self.minted()
        self.services.gate.set_ready({"nse": True, "catchup": False}, [])
        body = render(self.fetch, self.post(credential, {"room_id": ROOM, "event_id": "$e1"}))[1]
        self.assertNotIn("missed", body)
        self.assertNotIn("read_rooms", body)

    def test_fetch_errors(self) -> None:
        credential = self.minted()
        bad = self.post("B" * 43, {"room_id": ROOM, "event_id": "$e1"})
        self.assertEqual(render_error(self.fetch, bad)[:2], (401, "IM.ZUNO.BAD_CREDENTIAL"))
        for body in (
            {"room_id": ROOM},
            {"room_id": "room", "event_id": "$e1"},
            {"room_id": ROOM, "event_id": 5},
        ):
            self.assertEqual(
                render_error(self.fetch, self.post(credential, body))[:2], (400, "M_INVALID_PARAM")
            )

    def test_sixty_fetches_a_minute_per_credential(self) -> None:
        credential = self.minted()
        for _ in range(60):
            render(self.fetch, self.post(credential, {"room_id": ROOM, "event_id": "$e1"}))
        request = self.post(credential, {"room_id": ROOM, "event_id": "$e1"})
        self.assertEqual(render_error(self.fetch, request)[:2], (429, "M_LIMIT_EXCEEDED"))

    def test_disabled_nse(self) -> None:
        credential = self.minted()
        self.services.gate.set_ready({"nse": False}, [])
        request = self.post(credential, {"room_id": ROOM, "event_id": "$e1"})
        self.assertEqual(render_error(self.fetch, request)[:2], (503, "IM.ZUNO.PUSH_DISABLED"))
        minting = FakeRequest(b"POST", {}, token="tok")
        self.assertEqual(render_error(self.mint, minting)[:2], (503, "IM.ZUNO.PUSH_DISABLED"))

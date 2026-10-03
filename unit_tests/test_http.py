from synapse.module_api.errors import SynapseError
from twisted.trial import unittest

from unit_tests.fakes import FakeModuleApi, FakeRequest, run
from zuno_push.http import authorization, bearer, read_json, require_int, require_str


class ReadJsonTests(unittest.TestCase):
    def test_object_body(self) -> None:
        self.assertEqual(read_json(FakeRequest(b"POST", {"a": 1})), {"a": 1})

    def test_empty_body_only_when_allowed(self) -> None:
        self.assertEqual(read_json(FakeRequest(b"POST", b""), allow_empty=True), {})
        with self.assertRaises(SynapseError) as e:
            read_json(FakeRequest(b"POST", b""))
        self.assertEqual(e.exception.errcode, "M_NOT_JSON")

    def test_not_json_and_not_object(self) -> None:
        for raw in (b"{", b"[1]", b"\xff\xfe", b'"text"'):
            with self.assertRaises(SynapseError) as e:
                read_json(FakeRequest(b"POST", raw))
            self.assertEqual((e.exception.code, e.exception.errcode), (400, "M_NOT_JSON"))

    def test_oversized_body(self) -> None:
        with self.assertRaises(SynapseError) as e:
            read_json(FakeRequest(b"POST", b"{" + b" " * 9000 + b"}"))
        self.assertEqual(e.exception.errcode, "M_INVALID_PARAM")


class FieldTests(unittest.TestCase):
    def test_require_str(self) -> None:
        self.assertEqual(require_str({"room_id": "!r:hs"}, "room_id", "!"), "!r:hs")
        for body in (
            {},
            {"room_id": 5},
            {"room_id": ""},
            {"room_id": "r:hs"},
            {"room_id": "!" * 300},
        ):
            with self.assertRaises(SynapseError):
                require_str(body, "room_id", "!")

    def test_require_int_rejects_bools_and_range(self) -> None:
        self.assertEqual(require_int({"n": 5}, "n", 0, 10), 5)
        for value in (True, -1, 11, 1.5, "5"):
            with self.assertRaises(SynapseError):
                require_int({"n": value}, "n", 0, 10)


class AuthHeaderTests(unittest.TestCase):
    def test_authorization_header(self) -> None:
        self.assertEqual(
            authorization(FakeRequest(b"GET", auth="ZunoNotify abc")), "ZunoNotify abc"
        )
        self.assertIsNone(authorization(FakeRequest(b"GET")))

    def test_bearer_returns_user_and_device(self) -> None:
        api = FakeModuleApi()
        api.tokens["tok"] = ("@alice:test", "PHONE")
        self.assertEqual(
            run(bearer(api, FakeRequest(b"GET", token="tok"))), ("@alice:test", "PHONE")
        )

    def test_bearer_without_device_is_invalid(self) -> None:
        api = FakeModuleApi()
        api.tokens["tok"] = ("@alice:test", None)
        with self.assertRaises(SynapseError) as e:
            run(bearer(api, FakeRequest(b"GET", token="tok")))
        self.assertEqual(e.exception.errcode, "M_INVALID_PARAM")

    def test_bearer_missing_token(self) -> None:
        with self.assertRaises(SynapseError) as e:
            run(bearer(FakeModuleApi(), FakeRequest(b"GET")))
        self.assertEqual((e.exception.code, e.exception.errcode), (401, "M_MISSING_TOKEN"))

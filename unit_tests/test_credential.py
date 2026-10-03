import hashlib

from synapse.module_api.errors import SynapseError
from twisted.internet import task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals
from unit_tests.fakes import FakeModuleApi, FakeRequest, FakeUserInfo, run
from zuno_push.credential import CredentialAuth, hash_credential, mint, parse_header
from zuno_push.store import PREVIOUS_CREDENTIAL_MS, PushStore

NOW = 1_790_000_000_000


class CredentialFormatTests(unittest.TestCase):
    def test_mint_is_43_chars_of_base64url_and_stores_only_the_hash(self) -> None:
        credential, cred_hash = mint()
        self.assertRegex(credential, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(cred_hash, hashlib.sha256(credential.encode()).hexdigest())
        self.assertNotEqual(mint()[0], credential)

    def test_header_parsing(self) -> None:
        credential = "A" * 43
        self.assertEqual(parse_header(f"ZunoNotify {credential}"), credential)
        self.assertEqual(parse_header(f"zunonotify   {credential} "), credential)
        for bad in (
            None,
            f"Bearer {credential}",
            "ZunoNotify short",
            f"ZunoNotify {credential}=",
            "ZunoNotify",
        ):
            self.assertIsNone(parse_header(bad), bad)


class CredentialAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(NOW / 1000)
        self.api = FakeModuleApi(self.clock)
        self.api.userinfo["@alice:test"] = FakeUserInfo()
        self.internals = FakeInternals(self.api)
        self.internals.devices.add(("@alice:test", "PHONE"))
        self.store = PushStore(self.api)
        self.auth = CredentialAuth(self.api, self.store, self.internals)

    def issue(self, expires: int = NOW + 1_000_000) -> str:
        credential, cred_hash = mint()
        now = self.api.get_current_time_msec()
        run(self.store.set_credential("@alice:test", "PHONE", cred_hash, expires, now, None, None))
        return credential

    def check(self, credential: str) -> object:
        request = FakeRequest(b"POST", auth=f"ZunoNotify {credential}")
        return run(self.auth.authenticate(request))

    def rejected(self, credential: str | None) -> tuple[int, str]:
        auth = None if credential is None else f"ZunoNotify {credential}"
        try:
            run(self.auth.authenticate(FakeRequest(b"POST", auth=auth)))
        except SynapseError as e:
            return e.code, e.errcode
        raise AssertionError("expected a rejection")

    def test_valid_credential_identifies_the_device(self) -> None:
        credential = self.issue()
        device = self.check(credential)
        self.assertEqual((device.user_id, device.device_id), ("@alice:test", "PHONE"))
        self.assertEqual(device.cred_hash, hash_credential(credential))

    def test_previous_credential_works_for_ten_minutes(self) -> None:
        old = self.issue()
        new = self.issue()
        self.assertEqual(self.check(old).device_id, "PHONE")
        self.clock.advance(PREVIOUS_CREDENTIAL_MS / 1000)
        self.assertEqual(self.rejected(old), (401, "IM.ZUNO.BAD_CREDENTIAL"))
        self.assertEqual(self.check(new).device_id, "PHONE")

    def test_expired_credential(self) -> None:
        credential = self.issue(expires=NOW + 5)
        self.clock.advance(0.005)
        self.assertEqual(self.rejected(credential), (401, "IM.ZUNO.BAD_CREDENTIAL"))

    def test_deactivated_locked_or_suspended_user(self) -> None:
        credential = self.issue()
        for info in (
            FakeUserInfo(is_deactivated=True),
            FakeUserInfo(locked=True),
            FakeUserInfo(suspended=True),
        ):
            self.api.userinfo["@alice:test"] = info
            self.assertEqual(self.rejected(credential), (401, "IM.ZUNO.BAD_CREDENTIAL"))
        del self.api.userinfo["@alice:test"]
        self.assertEqual(self.rejected(credential), (401, "IM.ZUNO.BAD_CREDENTIAL"))

    def test_deleted_device_is_rejected_and_its_rows_removed(self) -> None:
        credential = self.issue()
        self.internals.devices.clear()
        self.assertEqual(self.rejected(credential), (401, "IM.ZUNO.BAD_CREDENTIAL"))
        self.assertIsNone(run(self.store.get_device("@alice:test", "PHONE")))

    def test_missing_or_garbage_credential(self) -> None:
        self.assertEqual(self.rejected(None), (401, "IM.ZUNO.BAD_CREDENTIAL"))
        self.assertEqual(self.rejected("nope"), (401, "IM.ZUNO.BAD_CREDENTIAL"))
        self.assertEqual(self.rejected("B" * 43), (401, "IM.ZUNO.BAD_CREDENTIAL"))

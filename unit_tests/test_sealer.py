import base64
import binascii
import hashlib
import json
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from twisted.trial import unittest

from zuno_push.sealer import PayloadTooLarge, RingPayload, expiry, seal, seal_b64

KEY = bytes(range(0x00, 0x20))
NONCE = bytes(range(0xA0, 0xAC))
KID = 16909060
VECTOR = RingPayload(
    room="!abc:zuno.im",
    call="c1",
    caller="@alice:zuno.im",
    cname="Alice",
    rname="",
    kind="video",
    ts=1790000000000,
    rts=1789999999000,
)
VECTOR_SHA256 = "aaf898f4941c4ff73dd0b30839264042535a098823be3a5ed024339fc58b6a80"
VECTOR_B64 = (
    "AQECAwRqsTutoKGio6Slpqeoqaqrd4kKMCKL4JeCLpJ2n8CHjvMx/dYuSEyB17m33VNY4W9dojqozaiT"
    "5qO+14c/ERkqI6cvAnN5Be4JPWZiYAjGptCWAmwCy9OcS5O3qyGS2xBh9GrBJp60jFlDI+7q7pqZ6i4D"
    "xeB5QdfbM0JYpfcixXjyg2VlYNqVmMCCIcbjkgROGsTHsJ7C+jyM7w1KyBcZmdIB6XYoClH+d4fpuYXm"
    "BJi6HlJYnT1rrbuMQJixbU33vuJdBJ+x0zSd2GMZSgUZIoL4BBDbClZ8EM4wYS/xMoAoR+6ik2ntfTCf"
    "6YoByJqq+8RPhrl1U7mTfHA9Ut2T3GsAhceFRJbFrYPdk4MvxWv0NKsiC9YWGmcr21+121DqgYQZJHyC"
    "yvge7gfbDTY8sYdSGyj692Pvi4JTW2rhihznC+jq5CLM2JoBoUz/DVDFucOtumDMYU0X0fJ2ptPiXr45"
    "Ds9f+Qp0mOqfBxfdk/WKQwindryVl718Rj9LWD5l/gNqmdVIiLWwlAgUOO0RoAl6IUJliPttCnua3ggF"
    "OSLLF2HHCsVk1YLT1LYrI4ltgbPL4sPGdZBoM8laoOGr5Apxt/AjekECPQFZa5bbqcmFmWaAqCh8pnbl"
    "P3dbQMiExGrsQDrdKj/qRIFH7SQTL3UuNsJtg/yFDqErTane6dIx8PIlAkn08qeojK8vPl3SLCwgPNYu"
    "vY78cI3xfXod"
)
FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "push", "voip_blob_v1.json")
PAYLOAD_KEYS = ("room", "call", "caller", "cname", "rname", "kind", "ts", "rts")


def open_blob(key: bytes, blob: bytes) -> tuple[int, int, dict]:
    version, kid, x = struct.unpack(">BII", blob[:9])
    if version != 1:
        raise ValueError("unknown version")
    nonce = blob[9:21]
    plaintext = ChaCha20Poly1305(key).decrypt(nonce, blob[21:], b"zuno-voip-v1" + blob[:9])
    return kid, x, json.loads(plaintext.rstrip(b"\x00"))


def classify(key: bytes, kid: int, blob_b64: str) -> str:
    try:
        blob = base64.b64decode(blob_b64, validate=True)
    except (binascii.Error, ValueError):
        return "forged"
    if len(blob) < 9:
        return "forged"
    version, blob_kid, _ = struct.unpack(">BII", blob[:9])
    if version != 1 or blob_kid != kid:
        return "generic"
    if len(blob) - 37 not in (512, 1024):
        return "forged"
    try:
        open_blob(key, blob)
    except InvalidTag:
        return "forged"
    return "ok"


def shared_fixture() -> dict:
    if not os.path.exists(FIXTURE):
        raise unittest.SkipTest("copy the app repo's test/fixtures/push/ into fixtures/push/")
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


class KnownAnswerTests(unittest.TestCase):
    def test_contract_vector_byte_for_byte(self) -> None:
        blob = seal(KEY, KID, VECTOR, NONCE)
        self.assertEqual(len(blob), 549)
        self.assertEqual(hashlib.sha256(blob).hexdigest(), VECTOR_SHA256)
        self.assertEqual(base64.b64encode(blob).decode(), VECTOR_B64)
        self.assertEqual(seal_b64(KEY, KID, VECTOR, NONCE), VECTOR_B64)

    def test_expiry_rule(self) -> None:
        self.assertEqual(expiry(1790000000000, 1789999999000), 1790000045)
        self.assertEqual(expiry(1790000100000, 1790000000000), 1790000030 + 45)


class SharedBlobVectorTests(unittest.TestCase):
    def test_every_blob_vector_from_the_app_repo(self) -> None:
        fixture = shared_fixture()
        key = base64.b64decode(fixture["key"])
        kid = fixture["kid"]
        self.assertEqual(fixture["aad_prefix"], "zuno-voip-v1")
        self.assertIn("known_answer", [v["name"] for v in fixture["vectors"]])
        for vector in fixture["vectors"]:
            name = vector["name"]
            payload = RingPayload(**{k: vector["payload"][k] for k in PAYLOAD_KEYS})
            self.assertEqual(payload.to_json_bytes(), vector["plaintext"].encode("utf-8"), name)
            self.assertEqual(expiry(payload.ts, payload.rts), vector["x"], name)
            blob = seal(key, kid, payload, base64.b64decode(vector["nonce"]))
            self.assertEqual(base64.b64encode(blob).decode("ascii"), vector["blob"], name)
            self.assertEqual(len(blob), vector["blob_length"], name)
            self.assertEqual(hashlib.sha256(blob).hexdigest(), vector["blob_sha256"], name)
            self.assertEqual(len(blob) - 37, vector["padded_length"], name)
            self.assertEqual(classify(key, kid, vector["blob"]), "ok", name)

    def test_every_tamper_case_from_the_app_repo(self) -> None:
        fixture = shared_fixture()
        key = base64.b64decode(fixture["key"])
        self.assertTrue(fixture["tamper"])
        for case in fixture["tamper"]:
            outcome = classify(key, fixture["kid"], case["blob"])
            self.assertEqual(outcome, case["expect"], case["name"])


class SealerTests(unittest.TestCase):
    def test_round_trip_with_random_nonce(self) -> None:
        a = seal(KEY, KID, VECTOR)
        b = seal(KEY, KID, VECTOR)
        self.assertNotEqual(a[9:21], b[9:21])
        self.assertEqual(open_blob(KEY, a)[2]["caller"], "@alice:zuno.im")

    def test_header_is_authenticated(self) -> None:
        blob = bytearray(seal(KEY, KID, VECTOR, NONCE))
        blob[8] ^= 0x01
        with self.assertRaises(InvalidTag):
            open_blob(KEY, bytes(blob))
        self.assertEqual(classify(KEY, KID, base64.b64encode(bytes(blob)).decode()), "forged")

    def test_non_ascii_names_are_raw_utf8(self) -> None:
        payload = RingPayload("!r:hs", "c", "@a:hs", "Zoë 😀", "Café", "voice", 1, 1)
        self.assertIn("Zoë 😀".encode(), payload.to_json_bytes())
        self.assertEqual(open_blob(KEY, seal(KEY, 1, payload))[2]["cname"], "Zoë 😀")

    def test_json_escaping_matches_the_contract(self) -> None:
        payload = RingPayload("!r:hs", "c\x1f", "@a:hs", 'Q"u\\o/te', "", "voice", 1, 2)
        self.assertEqual(
            payload.to_json_bytes(),
            b'{"room":"!r:hs","call":"c\\u001f","caller":"@a:hs","cname":"Q\\"u\\\\o/te",'
            b'"rname":"","kind":"voice","ts":1,"rts":2}',
        )

    def test_padding_grows_to_1024_then_refuses(self) -> None:
        big = RingPayload("!" + "r" * 500, "c", "@a:hs", "", "", "voice", 1, 1)
        self.assertEqual(len(seal(KEY, 1, big)), 1 + 4 + 4 + 12 + 1024 + 16)
        huge = RingPayload("!" + "r" * 1100, "c", "@a:hs", "", "", "voice", 1, 1)
        with self.assertRaises(PayloadTooLarge):
            seal(KEY, 1, huge)

    def test_bad_inputs(self) -> None:
        with self.assertRaises(ValueError):
            seal(KEY[:31], 1, VECTOR)
        with self.assertRaises(ValueError):
            seal(KEY, 0, VECTOR)
        with self.assertRaises(ValueError):
            seal(KEY, 1, VECTOR, NONCE[:11])

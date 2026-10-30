from __future__ import annotations

import base64
import json
import os
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

VERSION = 1
AAD_PREFIX = b"zuno-voip-v1"
PAD_SMALL = 512
PAD_LARGE = 1024
NONCE_BYTES = 12
LIFETIME_S = 45


class PayloadTooLarge(Exception):
    pass


@dataclass(frozen=True)
class RingPayload:
    room: str
    call: str
    caller: str
    cname: str
    rname: str
    kind: str
    ts: int
    rts: int

    def to_json_bytes(self) -> bytes:
        ordered = {
            "room": self.room,
            "call": self.call,
            "caller": self.caller,
            "cname": self.cname,
            "rname": self.rname,
            "kind": self.kind,
            "ts": self.ts,
            "rts": self.rts,
        }
        return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class RingEndPayload:
    room: str
    call: str
    reason: str
    ts: int
    rts: int

    def to_json_bytes(self) -> bytes:
        ordered = {"room": self.room, "call": self.call, "reason": self.reason, "ts": self.ts}
        return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def expiry(ts: int, rts: int) -> int:
    return min(ts, rts + 30_000) // 1000 + LIFETIME_S


def pad(plaintext: bytes) -> bytes:
    if len(plaintext) <= PAD_SMALL:
        size = PAD_SMALL
    elif len(plaintext) <= PAD_LARGE:
        size = PAD_LARGE
    else:
        raise PayloadTooLarge(len(plaintext))
    return plaintext + b"\x00" * (size - len(plaintext))


def seal(
    key: bytes, kid: int, payload: RingPayload | RingEndPayload, nonce: bytes | None = None
) -> bytes:
    if len(key) != 32:
        raise ValueError("key must be 32 bytes")
    if not 1 <= kid <= 0xFFFFFFFF:
        raise ValueError("kid out of range")
    nonce = os.urandom(NONCE_BYTES) if nonce is None else nonce
    if len(nonce) != NONCE_BYTES:
        raise ValueError("nonce must be 12 bytes")
    x = expiry(payload.ts, payload.rts)
    header = struct.pack(">BII", VERSION, kid, x)
    sealed = ChaCha20Poly1305(key).encrypt(nonce, pad(payload.to_json_bytes()), AAD_PREFIX + header)
    return header + nonce + sealed


def seal_b64(
    key: bytes, kid: int, payload: RingPayload | RingEndPayload, nonce: bytes | None = None
) -> str:
    return base64.b64encode(seal(key, kid, payload, nonce)).decode("ascii")

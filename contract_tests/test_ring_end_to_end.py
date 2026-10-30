from __future__ import annotations

import base64
import json
import struct

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from tests.zuno_push_contract.base import ENCRYPTION
from tests.zuno_push_contract.module_base import VOIP_KEY, ModuleTestCase

ANDROID = "im.zuno.chat.android"


def open_blob(blob_b64: str) -> tuple[int, dict]:
    blob = base64.b64decode(blob_b64)
    _, kid, _ = struct.unpack(">BII", blob[:9])
    plain = ChaCha20Poly1305(VOIP_KEY).decrypt(blob[9:21], blob[21:], b"zuno-voip-v1" + blob[:9])
    return kid, json.loads(plain.rstrip(b"\x00"))


class RingEndToEndTests(ModuleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.alice = self.user("alice")
        self.bob = self.user("bob")
        self.assertEqual(self.register_voip(self.bob[1], kid=77).code, 200)
        self.room = self.helper.create_room_as(
            self.alice[0],
            is_public=False,
            room_version="12",
            tok=self.alice[1],
            extra_content={
                "preset": "trusted_private_chat",
                "invite": [self.bob[0]],
                "is_direct": True,
                "initial_state": [ENCRYPTION],
            },
        )
        self.helper.join(self.room, self.bob[0], tok=self.bob[1])
        self.pump(1)
        self.sygnal_requests.clear()

    def publish(
        self, user: tuple[str, str], call_id: str | None, kind: str = "video", device: str = "D"
    ) -> None:
        memberships = (
            []
            if call_id is None
            else [
                {
                    "call_id": call_id,
                    "kind": kind,
                    "device_id": device,
                    "expires_ts": 1,
                    "created_ts": 1,
                    "foci_active": {},
                }
            ]
        )
        self.helper.send_state(
            self.room, "m.call.member", {"memberships": memberships}, tok=user[1], state_key=user[0]
        )

    def ring_status(self, call_id: str) -> dict:
        credential = self.call("POST", "nse/credential", {}, token=self.bob[1]).json_body[
            "credential"
        ]
        channel = self.call(
            "POST",
            "ring/status",
            {"room_id": self.room, "call_id": call_id, "wait_ms": 0},
            auth=f"ZunoNotify {credential}",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        return channel.json_body

    def test_android_registration_rings_over_fcm(self) -> None:
        token = "fcm-tok_1:APA91b"
        registered = self.register_voip(
            self.bob[1], kid=78, pushkey=token, app_id="im.zuno.chat.android"
        )
        self.assertEqual(registered.code, 200)
        self.publish(self.alice, "c1")
        self.pump(0.5)
        self.assertEqual(len(self.sygnal_requests), 1)
        notification = self.sygnal_requests[0]["notification"]
        self.assertEqual(set(notification), {"event_id", "prio", "devices"})
        self.assertEqual(notification["event_id"], "$z")
        device = notification["devices"][0]
        self.assertEqual((device["app_id"], device["pushkey"]), ("im.zuno.chat.android", token))
        kid, plain = open_blob(device["data"]["default_payload"]["z"])
        self.assertEqual((kid, plain["call"]), (78, "c1"))

    def ended_ring(self, token: str) -> dict:
        self.assertEqual(len(self.sygnal_requests), 2)
        notification = self.sygnal_requests[1]["notification"]
        self.assertEqual(notification["event_id"], "$z")
        device = notification["devices"][0]
        self.assertEqual((device["app_id"], device["pushkey"]), (ANDROID, token))
        self.assertEqual(list(device["data"]["default_payload"]), ["ze"])
        kid, plain = open_blob(device["data"]["default_payload"]["ze"])
        self.assertEqual(kid, 78)
        self.assertEqual(set(plain), {"room", "call", "reason", "ts"})
        self.assertEqual((plain["room"], plain["call"]), (self.room, "c1"))
        return plain

    def test_android_ring_ends_when_the_caller_hangs_up(self) -> None:
        token = "fcm-tok_1:APA91b"
        self.assertEqual(self.register_voip(self.bob[1], 78, token, ANDROID).code, 200)
        self.publish(self.alice, "c1")
        self.pump(0.5)
        self.publish(self.alice, None)
        self.pump(0.5)
        self.assertEqual(self.ended_ring(token)["reason"], "hung_up")
        self.publish(self.alice, None)
        self.pump(0.5)
        self.assertEqual(len(self.sygnal_requests), 2)
        self.assertEqual(self.ring_status("c1")["status"], "sent")

    def test_android_ring_ends_when_answered_on_another_device(self) -> None:
        token = "fcm-tok_1:APA91b"
        self.assertEqual(self.register_voip(self.bob[1], 78, token, ANDROID).code, 200)
        laptop = self.device_of(self.login("bob", "pass"))
        self.publish(self.alice, "c1")
        self.pump(0.5)
        self.publish(self.bob, "c1", device=laptop)
        self.pump(0.5)
        self.assertEqual(self.ended_ring(token)["reason"], "answered_elsewhere")
        self.publish(self.alice, None)
        self.pump(0.5)
        self.assertEqual(len(self.sygnal_requests), 2)

    def test_v12_dm_call_rings_the_creator_peer_once(self) -> None:
        self.publish(self.alice, "c1")
        self.pump(0.5)
        self.assertEqual(len(self.sygnal_requests), 1)
        notification = self.sygnal_requests[0]["notification"]
        self.assertEqual(notification["event_id"], "$z")
        self.assertNotIn("room_id", notification)
        device = notification["devices"][0]
        self.assertEqual(device["app_id"], "im.zuno.chat.ios.voip")
        kid, plain = open_blob(device["data"]["default_payload"]["z"])
        self.assertEqual(kid, 77)
        self.assertEqual(
            (plain["room"], plain["call"], plain["caller"], plain["kind"]),
            (self.room, "c1", self.alice[0], "video"),
        )
        self.assertEqual(plain["cname"], "alice")
        status = self.ring_status("c1")
        self.assertEqual((status["status"], status["rule"]), ("sent", None))
        self.publish(self.alice, "c1")
        self.publish(self.bob, "c1")
        self.pump(1)
        self.assertEqual(len(self.sygnal_requests), 1)

    def test_muted_room_is_suppressed_with_its_rule(self) -> None:
        self.push_rule(
            self.bob[1],
            "override",
            self.room,
            {
                "conditions": [{"kind": "event_match", "key": "room_id", "pattern": self.room}],
                "actions": [],
            },
        )
        self.publish(self.alice, "c2")
        self.pump(0.5)
        self.assertEqual(self.sygnal_requests, [])
        self.assertEqual(self.ring_status("c2")["rule"], "no_notify")

    def test_rejected_token_is_deleted(self) -> None:
        self.sygnal_rejects.add(base64.b64encode(b"token").decode())
        self.publish(self.alice, "c3")
        self.pump(0.5)
        self.assertEqual(self.ring_status("c3")["status"], "failed")
        device = self.get_success(
            self.module.services.store.get_device(self.bob[0], self.device_of(self.bob[1]))
        )
        self.assertFalse(device.has_voip)
        self.assertEqual(device.last_result, "rejected")

    def test_unknown_call_is_pending(self) -> None:
        self.assertEqual(self.ring_status("never")["status"], "pending")

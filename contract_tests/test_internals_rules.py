from __future__ import annotations

from typing import Any

from synapse.types.state import StateFilter
from tests.zuno_push_contract.base import ENCRYPTION, ContractTestCase

from zuno_push.rules import CallStart, invite_shaped


class PowerLevelContractTests(ContractTestCase):
    def can_ring(self, room_id: str, user_id: str) -> bool:
        state = self.state(room_id)
        return self.internals.user_level(state, user_id) >= self.internals.call_member_level(state)

    def test_v12_trusted_private_chat_makes_both_people_creators(self) -> None:
        alice, bob = self.user("alice"), self.user("bob")
        room_id = self.helper.create_room_as(
            alice[0],
            is_public=False,
            room_version="12",
            tok=alice[1],
            extra_content={"preset": "trusted_private_chat", "invite": [bob[0]], "is_direct": True},
        )
        self.helper.join(room_id, bob[0], tok=bob[1])
        state = self.state(room_id)
        self.assertEqual(state[("m.room.power_levels", "")].content.get("users"), {})
        self.assertTrue(self.can_ring(room_id, alice[0]))
        self.assertTrue(self.can_ring(room_id, bob[0]))

    def test_v12_public_room_with_moderator_calls(self) -> None:
        alice, bob, carol = self.user("alice"), self.user("bob"), self.user("carol")
        room_id = self.room_with(
            alice,
            bob,
            carol,
            room_version="12",
            power_level_content_override={"events": {"m.call.member": 50}, "users": {carol[0]: 50}},
        )
        self.assertTrue(self.can_ring(room_id, alice[0]))
        self.assertFalse(self.can_ring(room_id, bob[0]))
        self.assertTrue(self.can_ring(room_id, carol[0]))

    def test_room_without_power_levels_uses_state_default(self) -> None:
        alice = self.user("alice")
        room_id = self.room_with(alice, room_version="10")
        create = self.state(room_id)[("m.room.create", "")]
        state = {("m.room.create", ""): create}
        self.assertEqual(self.internals.call_member_level(state), 50)
        self.assertEqual(self.internals.user_level(state, alice[0]), 100)
        self.assertEqual(self.internals.user_level(state, "@other:test"), 0)


class PushRuleContractTests(ContractTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.alice = self.user("alice")
        self.bob = self.user("bob")

    def module_says(self, room_id: str, encrypted: bool) -> bool:
        state = self.state(room_id)
        call = CallStart(room_id, "c1", self.alice[0], "voice", 0)
        joined = self.get_success(self.internals.joined_count(room_id))
        sender_level = self.internals.user_level(state, self.alice[0])
        return self.get_success(
            self.internals.would_notify(
                state, joined, sender_level, invite_shaped(call, encrypted), self.bob[0], "bob"
            )
        )

    def synapse_says(self, room_id: str, encrypted: bool) -> bool:
        call = CallStart(room_id, "c1", self.alice[0], "voice", 0)
        invite = invite_shaped(call, encrypted)
        content = dict(invite["content"])
        if encrypted:
            content.update(
                {"ciphertext": "x", "session_id": "s", "sender_key": "k", "device_id": "D"}
            )
        event_id = self.helper.send_event(room_id, invite["type"], content, tok=self.alice[1])[
            "event_id"
        ]
        notif = self.get_success(
            self.store.db_pool.simple_select_one_onecol(
                "event_push_actions",
                {"user_id": self.bob[0], "room_id": room_id, "event_id": event_id},
                "notif",
                allow_none=True,
            )
        )
        return notif == 1

    def assert_parity(self, room_id: str, encrypted: bool, expected: bool) -> None:
        self.assertEqual(self.module_says(room_id, encrypted), expected)
        self.assertEqual(self.synapse_says(room_id, encrypted), expected)

    def e2ee_room(self, *extra_members: tuple[str, str]) -> str:
        return self.room_with(self.alice, self.bob, *extra_members, initial_state=[ENCRYPTION])

    def test_default_rules_notify_encrypted_and_plain_invites(self) -> None:
        self.assert_parity(self.e2ee_room(), True, True)
        self.assert_parity(self.e2ee_room(self.user("carol")), True, True)
        self.assert_parity(self.room_with(self.alice, self.bob), False, True)

    def test_room_muted_by_override_rule(self) -> None:
        room_id = self.e2ee_room()
        self.push_rule(
            self.bob[1],
            "override",
            room_id,
            {
                "conditions": [{"kind": "event_match", "key": "room_id", "pattern": room_id}],
                "actions": [],
            },
        )
        self.assert_parity(room_id, True, False)

    def test_mentions_only_room_rule(self) -> None:
        room_id = self.e2ee_room()
        self.push_rule(self.bob[1], "room", room_id, {"actions": []})
        self.assert_parity(room_id, True, False)

    def test_master_rule(self) -> None:
        room_id = self.e2ee_room()
        self.push_rule_enabled(self.bob[1], "override", ".m.rule.master", True)
        self.assert_parity(room_id, True, False)

    def test_disabled_encrypted_group_rule_spares_one_to_one(self) -> None:
        one_to_one = self.e2ee_room()
        group = self.e2ee_room(self.user("carol"))
        self.push_rule_enabled(self.bob[1], "underride", ".m.rule.encrypted", False)
        self.assert_parity(group, True, False)
        self.assert_parity(one_to_one, True, True)

    def test_sender_rule_matches_synapse_which_ignores_it(self) -> None:
        room_id = self.e2ee_room()
        self.push_rule(self.bob[1], "sender", self.alice[0], {"actions": []})
        self.assert_parity(room_id, True, True)

    def test_members_and_counts(self) -> None:
        room_id = self.e2ee_room(self.user("carol"))
        self.assertEqual(self.get_success(self.internals.joined_count(room_id)), 3)
        self.assertEqual(
            set(self.get_success(self.internals.local_joined_users(room_id))),
            {self.alice[0], self.bob[0], "@carol:test"},
        )

    def test_local_member_reads_never_wait_for_full_state(self) -> None:
        is_mine = self.hs.is_mine_id
        local: list[tuple[str, Any]] = [
            ("m.room.member", self.bob[0]),
            ("m.call.member", self.bob[0]),
        ]
        self.assertFalse(StateFilter.from_types(local).must_await_full_state(is_mine))
        self.assertFalse(
            StateFilter.from_types([("m.call.member", "@remote:elsewhere")]).must_await_full_state(
                is_mine
            )
        )
        self.assertTrue(
            StateFilter.from_types([("m.room.member", "@remote:elsewhere")]).must_await_full_state(
                is_mine
            )
        )

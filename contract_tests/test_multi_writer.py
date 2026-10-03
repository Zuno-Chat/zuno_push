from __future__ import annotations

from typing import Any

from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.util.clock import Clock
from tests.replication._base import BaseMultiWorkerStreamTestCase
from twisted.internet.testing import MemoryReactor

from zuno_push.internals import SynapseInternals


class MultiWriterContractTests(BaseMultiWorkerStreamTestCase):
    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
    ]

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config["stream_writers"] = {"events": ["worker1", "worker2"]}
        config["instance_map"] = {
            "main": {"host": "testserv", "port": 8765},
            "worker1": {"host": "testserv", "port": 1001},
            "worker2": {"host": "testserv", "port": 1002},
        }
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.internals = SynapseInternals(hs.get_module_api())

    def test_deltas_from_both_writers_reach_the_main_process_poller(self) -> None:
        self.make_worker_hs("synapse.app.generic_worker", {"worker_name": "worker1"})
        self.make_worker_hs("synapse.app.generic_worker", {"worker_name": "worker2"})
        user_id = self.register_user("alice", "pass")
        token = self.login("alice", "pass")
        start = self.internals.max_stream_ordering()
        calls: set[str] = set()
        writers: set[str] = set()
        for i in range(20):
            room_id = self.helper.create_room_as(user_id, tok=token)
            event_id = self.helper.send_state(
                room_id,
                "m.call.member",
                {"memberships": [{"call_id": f"c{i}", "kind": "voice"}]},
                tok=token,
                state_key=user_id,
            )["event_id"]
            calls.add(event_id)
            writers.add(self.get_success(self.store.get_position_for_event(event_id)).instance_name)
            if writers == {"worker1", "worker2"}:
                break
        self.assertEqual(writers, {"worker1", "worker2"})
        self.replicate()
        top = self.internals.max_stream_ordering()
        pos, seen = start, set()
        while pos < top:
            pos, rows = self.get_success(self.internals.state_deltas(pos, top))
            seen |= {r.event_id for r in rows if r.event_type == "m.call.member"}
        self.assertEqual(seen, calls)

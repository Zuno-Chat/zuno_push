import sys

from twisted.internet import task
from twisted.trial import unittest

from unit_tests.fake_internals import FakeInternals
from unit_tests.fakes import FakeModuleApi, run
from unit_tests.harness import SYGNAL_URL
from zuno_push import ZunoPush
from zuno_push.http import BASE_PATH

ROUTES = {
    f"{BASE_PATH}/{p}"
    for p in ("voip", "device", "health", "test", "nse/credential", "nse/fetch", "ring/status")
}
KEY = "A" * 43 + "="


def config(**extra: object):
    return ZunoPush.parse_config({"sygnal_notify_url": SYGNAL_URL, **extra})


class ModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.api = FakeModuleApi(self.clock, migrated=False)
        self.internals = FakeInternals(self.api)

    def start(self, **extra: object) -> ZunoPush:
        module = ZunoPush(config(**extra), self.api, self.internals)
        for d in self.api.background:
            self.successResultOf(d)
        return module

    def test_registers_routes_cleanup_and_background_work(self) -> None:
        module = self.start()
        self.assertEqual(set(self.api.resources), ROUTES)
        self.assertIsNotNone(self.api.on_logged_out)
        self.assertIsNotNone(self.api.on_deactivation)
        self.assertEqual(
            {desc for _, _, desc in self.api.looping},
            {"zuno_push_janitor", "zuno_push_poll"},
        )
        self.assertEqual({desc: ms for _, ms, desc in self.api.looping}["zuno_push_poll"], 250)
        self.assertTrue(
            all(
                module.services.gate.enabled(f)
                for f in ("ring", "health", "test", "nse", "catchup")
            )
        )

    def test_workers_serve_routes_but_run_no_loops(self) -> None:
        self.api.run_background_tasks = False
        self.start()
        self.assertEqual(set(self.api.resources), ROUTES)
        self.assertEqual(self.api.looping, [])

    def test_invalid_config_disables_the_module_without_raising(self) -> None:
        module = ZunoPush(
            ZunoPush.parse_config({"sygnal_notify_url": "nope"}), self.api, self.internals
        )
        self.assertIsNone(module.services)
        self.assertEqual(self.api.resources, {})
        self.assertEqual(self.api.background, [])

    def test_missing_private_internals_disable_the_module(self) -> None:
        saved = sys.modules.get("zuno_push.internals")
        sys.modules["zuno_push.internals"] = None
        try:
            module = ZunoPush(config(), self.api)
        finally:
            if saved is None:
                del sys.modules["zuno_push.internals"]
            else:
                sys.modules["zuno_push.internals"] = saved
        self.assertIsNone(module.services)
        self.assertEqual(self.api.resources, {})

    def test_probe_failure_disables_only_its_feature_and_stops_the_poller(self) -> None:
        self.internals.probe_failures.add("state_deltas")
        module = self.start()
        self.assertFalse(module.services.gate.enabled("ring"))
        self.assertTrue(module.services.gate.enabled("nse"))
        self.assertEqual({desc for _, _, desc in self.api.looping}, {"zuno_push_janitor"})

    def test_kill_switch(self) -> None:
        module = self.start(disabled_features=["nse"])
        self.assertFalse(module.services.gate.enabled("nse"))
        self.assertFalse(module.services.gate.enabled("catchup"))

    def test_migration_failure_disables_every_feature(self) -> None:
        self.api.conn.execute(
            "CREATE TABLE zuno_push_schema (lock TEXT PRIMARY KEY, version INTEGER)"
        )
        self.api.conn.execute("INSERT INTO zuno_push_schema VALUES ('X', 99)")
        self.api.conn.commit()
        module = self.start()
        self.assertEqual(self.api.looping, [])
        self.assertFalse(module.services.gate.enabled("health"))

    def test_untested_synapse_version_is_logged(self) -> None:
        import zuno_push.module

        saved = zuno_push.module.TESTED_SYNAPSE_VERSION
        zuno_push.module.TESTED_SYNAPSE_VERSION = "0.0.1"
        try:
            with self.assertLogs("zuno_push", level="WARNING") as logs:
                self.start()
        finally:
            zuno_push.module.TESTED_SYNAPSE_VERSION = saved
        self.assertTrue(any("0.0.1" in line for line in logs.output))

    def test_logout_and_deactivation_cleanup(self) -> None:
        module = self.start()
        store = module.services.store
        run(store.upsert_voip("@a:test", "A", "app", "pk1", 1, KEY))
        run(store.upsert_voip("@a:test", "B", "app", "pk2", 1, KEY))
        run(store.upsert_voip("@b:test", "C", "app", "pk3", 1, KEY))
        run(self.api.on_logged_out("@a:test", "A", "token"))
        run(self.api.on_logged_out("@a:test", None, "token"))
        self.assertIsNone(run(store.get_device("@a:test", "A")))
        self.assertIsNotNone(run(store.get_device("@a:test", "B")))
        run(self.api.on_deactivation("@a:test", False, False))
        self.assertIsNotNone(run(store.get_device("@a:test", "B")))
        run(self.api.on_deactivation("@a:test", True, True))
        self.assertIsNone(run(store.get_device("@a:test", "B")))
        self.assertIsNotNone(run(store.get_device("@b:test", "C")))

from prometheus_client import REGISTRY
from synapse.module_api.errors import SynapseError
from twisted.trial import unittest

from unit_tests.fakes import run
from zuno_push.features import FeatureGate, run_probes


async def ok() -> None:
    return None


async def broken() -> None:
    raise RuntimeError("drift")


class FeatureGateTests(unittest.TestCase):
    def test_starting_gate_refuses_everything(self) -> None:
        gate = FeatureGate()
        self.assertFalse(gate.started)
        with self.assertRaises(SynapseError) as e:
            gate.require("ring")
        self.assertEqual((e.exception.code, e.exception.errcode), (503, "IM.ZUNO.PUSH_DISABLED"))

    def test_probe_failure_disables_only_that_feature(self) -> None:
        results = run(
            run_probes(
                {
                    "ring": [("a", ok), ("b", broken)],
                    "health": [("c", ok)],
                    "test": [],
                    "nse": [("d", ok)],
                    "catchup": [("e", ok)],
                }
            )
        )
        self.assertEqual(
            results, {"ring": False, "health": True, "test": True, "nse": True, "catchup": True}
        )
        gate = FeatureGate()
        gate.set_ready(results, disabled=["test"])
        self.assertEqual(
            {f: gate.enabled(f) for f in ("ring", "health", "test", "nse", "catchup")},
            {"ring": False, "health": True, "test": False, "nse": True, "catchup": True},
        )
        self.assertEqual(
            REGISTRY.get_sample_value("zuno_push_feature_enabled", {"feature": "ring"}), 0
        )

    def test_catchup_needs_nse(self) -> None:
        gate = FeatureGate()
        gate.set_ready({"nse": False, "catchup": True}, disabled=[])
        self.assertFalse(gate.enabled("catchup"))

    def test_disable_all(self) -> None:
        gate = FeatureGate()
        gate.disable_all()
        self.assertTrue(gate.started)
        self.assertFalse(gate.enabled("nse"))

from synapse.module_api.errors import SynapseError
from twisted.internet import task
from twisted.trial import unittest

from unit_tests.fakes import FakeModuleApi, run
from zuno_push.ratelimit import Limit, RateLimiter

LIMIT = Limit("demo", 2, 60_000)


class RateLimiterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = task.Clock()
        self.clock.advance(1)
        self.api = FakeModuleApi(self.clock)
        self.limiter = RateLimiter(self.api)

    def test_allows_up_to_the_limit_then_refuses_with_retry_after(self) -> None:
        run(self.limiter.check(LIMIT, "k"))
        run(self.limiter.check(LIMIT, "k"))
        with self.assertRaises(SynapseError) as e:
            run(self.limiter.check(LIMIT, "k"))
        self.assertEqual((e.exception.code, e.exception.errcode), (429, "M_LIMIT_EXCEEDED"))
        self.assertEqual(e.exception.error_dict(None)["retry_after_ms"], 59_000)

    def test_keys_are_independent_and_windows_reset(self) -> None:
        self.assertTrue(run(self.limiter.allow(LIMIT, "a")))
        self.assertTrue(run(self.limiter.allow(LIMIT, "a")))
        self.assertFalse(run(self.limiter.allow(LIMIT, "a")))
        self.assertTrue(run(self.limiter.allow(LIMIT, "b")))
        self.clock.advance(60)
        self.assertTrue(run(self.limiter.allow(LIMIT, "a")))

    def test_counts_are_shared_through_the_database(self) -> None:
        other = RateLimiter(self.api)
        run(self.limiter.check(LIMIT, "k"))
        run(other.check(LIMIT, "k"))
        self.assertFalse(run(self.limiter.allow(LIMIT, "k")))

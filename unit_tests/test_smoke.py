import json
import urllib.request

from twisted.trial import unittest

from zuno_push.smoke import Reply, run, verdict

MODULE = {"x-zuno-push": "1", "content-type": "application/json"}


def reply(status: int, body: object, headers: dict | None = None) -> Reply:
    return Reply(status, MODULE if headers is None else headers, json.dumps(body).encode())


class SmokeTests(unittest.TestCase):
    def test_verdicts(self) -> None:
        self.assertIsNone(verdict(reply(200, {"server_ts": 1}), 200, None))
        self.assertIsNone(
            verdict(
                reply(401, {"errcode": "IM.ZUNO.BAD_CREDENTIAL"}), 401, "IM.ZUNO.BAD_CREDENTIAL"
            )
        )
        self.assertIn(
            "route failure", verdict(reply(404, {"errcode": "M_UNRECOGNIZED"}, {}), 200, None)
        )
        self.assertIn(
            "route failure", verdict(Reply(502, {}, b"<html>bad gateway</html>"), 200, None)
        )
        self.assertIn("not JSON", verdict(Reply(200, MODULE, b"<html>"), 200, None))
        self.assertIn(
            "expected HTTP 200",
            verdict(reply(503, {"errcode": "IM.ZUNO.PUSH_DISABLED"}), 200, None),
        )
        self.assertIn("without server_ts", verdict(reply(200, {}), 200, None))

    def test_run_checks_both_routes(self) -> None:
        seen: list[urllib.request.Request] = []

        def fetch(request: urllib.request.Request) -> Reply:
            seen.append(request)
            if request.full_url.endswith("/health"):
                return reply(200, {"server_ts": 5})
            return reply(401, {"errcode": "IM.ZUNO.BAD_CREDENTIAL"})

        self.assertEqual(run("https://matrix.zuno.im/", "tok", fetch), [])
        self.assertEqual(
            [r.full_url for r in seen],
            [
                "https://matrix.zuno.im/_synapse/client/zuno/push/v1/health",
                "https://matrix.zuno.im/_synapse/client/zuno/push/v1/nse/fetch",
            ],
        )
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer tok")
        self.assertTrue(seen[1].get_header("Authorization").startswith("ZunoNotify "))

    def test_misrouted_nse_path_fails_the_smoke_test(self) -> None:
        def fetch(request: urllib.request.Request) -> Reply:
            if request.full_url.endswith("/health"):
                return reply(200, {"server_ts": 5})
            return reply(404, {"errcode": "M_UNRECOGNIZED"}, {})

        problems = run("https://hs", "tok", fetch)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("POST nse/fetch: route failure"))

    def test_network_error(self) -> None:
        def fetch(request: urllib.request.Request) -> Reply:
            raise ConnectionRefusedError()

        self.assertEqual(len(run("https://hs", "tok", fetch)), 2)

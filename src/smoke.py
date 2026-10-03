from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

PATH = "/_synapse/client/zuno/push/v1"
DUMMY_CREDENTIAL = "A" * 43


@dataclass(frozen=True)
class Reply:
    status: int
    headers: Mapping[str, str]
    body: bytes


Fetch = Callable[[urllib.request.Request], Reply]


def http_fetch(request: urllib.request.Request) -> Reply:
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return Reply(
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as e:
        return Reply(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())


def verdict(reply: Reply, status: int, errcode: str | None) -> str | None:
    if reply.headers.get("x-zuno-push") != "1":
        return (
            f"route failure: HTTP {reply.status} without X-Zuno-Push (proxy or worker misrouting)"
        )
    try:
        body = json.loads(reply.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return f"module reply is not JSON (HTTP {reply.status})"
    if reply.status != status:
        return f"expected HTTP {status}, got {reply.status}: {body}"
    if errcode is not None and body.get("errcode") != errcode:
        return f"expected {errcode}, got {body.get('errcode')}"
    if errcode is None and not isinstance(body.get("server_ts"), int):
        return "200 reply without server_ts"
    return None


def run(homeserver: str, token: str, fetch: Fetch = http_fetch) -> list[str]:
    base = homeserver.rstrip("/") + PATH
    health = urllib.request.Request(f"{base}/health", headers={"Authorization": f"Bearer {token}"})
    nse = urllib.request.Request(
        f"{base}/nse/fetch",
        data=json.dumps({"room_id": "!smoke:invalid", "event_id": "$smoke"}).encode(),
        headers={
            "Authorization": f"ZunoNotify {DUMMY_CREDENTIAL}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    problems = []
    for name, request, status, errcode in (
        ("GET health", health, 200, None),
        ("POST nse/fetch", nse, 401, "IM.ZUNO.BAD_CREDENTIAL"),
    ):
        try:
            problem = verdict(fetch(request), status, errcode)
        except OSError as e:
            problem = f"network error: {type(e).__name__}"
        if problem:
            problems.append(f"{name}: {problem}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m zuno_push.smoke")
    parser.add_argument("--homeserver", required=True)
    parser.add_argument("--token", default=os.environ.get("ZUNO_SMOKE_TOKEN"))
    args = parser.parse_args(argv)
    if not args.token:
        parser.error("pass --token or set ZUNO_SMOKE_TOKEN")
    problems = run(args.homeserver, args.token)
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        print("OK zuno_push answers on the public URL")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

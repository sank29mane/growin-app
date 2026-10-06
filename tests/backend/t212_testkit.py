"""Shared helpers for the Trading 212 transport tests (66-02).

Nothing here talks to a network. ``no_real_network`` is the guard the plan's
prohibition asks for: a test module installs it as an autouse fixture and any
real httpx transport or socket connect fails the test.
"""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path
from typing import Callable, Optional

import httpx

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "trading212_v0"
DEMO_BASE = "https://demo.trading212.com/api/v0"
LIVE_BASE = "https://live.trading212.com/api/v0"
START = 1_800_000_000.0


class FakeClock:
    """A clock whose sleep advances it. Records every requested sleep."""

    def __init__(self, start: float = START) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def install_no_real_network(monkeypatch) -> None:
    """Fail on any real transport send or socket connect."""

    async def _refuse_async(self, request, *args, **kwargs):
        raise AssertionError(f"real httpx transport used for {request.method} {request.url}")

    def _refuse_sync(self, request, *args, **kwargs):
        raise AssertionError(f"real httpx transport used for {request.method} {request.url}")

    def _refuse_connect(self, *args, **kwargs):
        raise AssertionError(f"socket connect attempted: {args!r}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _refuse_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _refuse_sync)
    monkeypatch.setattr(socket.socket, "connect", _refuse_connect)


class Fixture:
    def __init__(self, name: str) -> None:
        self.name = name
        self.doc = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
        meta = self.doc["_fixture"]
        assert meta["spec_sha256"].startswith("a272f70a"), name

    @property
    def body(self):
        return json.loads(json.dumps(self.doc["response"].get("body")))

    def response(self, clock: Optional[FakeClock] = None, **overrides) -> httpx.Response:
        spec = self.doc["response"]
        headers = {}
        for key, value in spec["headers"].items():
            match = re.fullmatch(r"@NOW\+(\d+)", value)
            if match:
                value = str(int((clock.now if clock else START) + int(match.group(1))))
            headers[key] = value
        headers.update(overrides.pop("headers", {}))
        status = overrides.pop("status", spec["status"])
        if "body_text" in spec and "json" not in overrides:
            return httpx.Response(status, headers=headers, text=spec["body_text"])
        payload = overrides.pop("json", self.body)
        return httpx.Response(status, headers=headers, json=payload)


class Recorder:
    """A MockTransport handler that logs every request and answers from a router."""

    def __init__(self, router: Callable[[httpx.Request], httpx.Response]) -> None:
        self.router = router
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.router(request)

    @property
    def count(self) -> int:
        return len(self.requests)

    @property
    def methods(self) -> list[str]:
        return [request.method for request in self.requests]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def serving(*fixtures: Fixture, clock: Optional[FakeClock] = None) -> Recorder:
    """Answer each request from the fixture whose path (and method) matches."""

    table = {}
    for fx in fixtures:
        request = fx.doc["request"]
        table[(request["method"], request["path"].split("?", 1)[0])] = fx

    def router(request: httpx.Request) -> httpx.Response:
        fx = table.get((request.method, request.url.path))
        if fx is None:
            return httpx.Response(599, text=f"unexpected {request.method} {request.url.path}")
        return fx.response(clock)

    return Recorder(router)

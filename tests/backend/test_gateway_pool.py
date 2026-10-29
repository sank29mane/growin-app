"""PooledTransport (Phase 61-08, OD-3 and OD-7): bounded persistent upstream connections.

A real ThreadingHTTPServer on 127.0.0.1 stands in for the upstream; nothing
leaves the machine. The server counts connections that carried a request and
requests in flight so reuse and the connection cap are measured, not assumed.

The port is an ephemeral one on a shared loopback, so other local processes can
and do connect to it (a node dev tool probing for servers was caught doing
`GET /`, and another probe that connects, idles, then sends). Only requests
carrying the X-Test header that `get` sends are counted; anything else gets a
bare 404 and leaves every counter alone. A server-side accept count cannot tell
the pool's socket from an idle probe's, so "the pool opened no socket" is
checked in-process instead: `connect_spy` records every address this process
connects a socket to through `socket.socket.connect` or `connect_ex`. A raw
`_socket` connect would slip past it; the pool has no such path.
"""

from __future__ import annotations

import http.client
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway" / "vm"))

from gateway_vm.pool import PooledTransport  # noqa: E402
from gateway_vm.transport import (  # noqa: E402
    AllowedHost,
    HttpClientTransport,
    Timeouts,
    TransportError,
)

T = Timeouts(connect_s=2.0, read_s=3.0, total_s=5.0)


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # resets are the point of some tests; print nothing
        pass


class Upstream:
    """A local HTTP/1.1 server with scripted behaviour per path."""

    def __init__(self) -> None:
        self.connections = 0
        self.requests = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.bodies: list[bytes] = []
        self.lock = threading.Lock()
        self.entered = threading.Event()  # a /hold request is inside the handler
        self.release = threading.Event()  # lets every /hold request finish
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.counted = False  # this connection has not yet carried one of our requests

            def log_message(self, *_a):
                pass

            def _serve(self):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                if self.headers.get("X-Test") is None:  # not from `get`: a stray local client
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                with outer.lock:
                    if not self.counted:
                        self.counted = True
                        outer.connections += 1
                    outer.requests += 1
                    outer.bodies.append(body)
                    outer.in_flight += 1
                    outer.max_in_flight = max(outer.max_in_flight, outer.in_flight)
                try:
                    path = self.path
                    if path.startswith("/hold"):  # blocks until the test releases it; no timing guess
                        outer.entered.set()
                        outer.release.wait(10)  # safety cap so a broken pool cannot hang the suite
                    if path.startswith("/slow"):
                        time.sleep(float(path.split("=")[1]) if "=" in path else 0.4)
                    if path.startswith("/redirect"):
                        self.send_response(302)
                        self.send_header("Location", "http://127.0.0.1:1/")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if path.startswith("/close"):
                        payload = b"closing"
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(payload)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(payload)
                        self.close_connection = True
                        return
                    if path.startswith("/hangup"):  # one good answer, then the server drops the socket
                        payload = b"bye"
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        self.wfile.flush()
                        self.close_connection = True
                        return
                    if path.startswith("/short"):  # promises 100 bytes, sends 10, then closes
                        self.send_response(200)
                        self.send_header("Content-Length", "100")
                        self.end_headers()
                        self.wfile.write(b"x" * 10)
                        self.wfile.flush()
                        self.close_connection = True
                        return
                    if path.startswith("/stream"):  # no Content-Length: the body ends when the server closes
                        self.send_response(200)
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(b"s" * 5000)
                        self.wfile.flush()
                        self.close_connection = True
                        return
                    if path.startswith("/big"):
                        payload = b"z" * 5000
                    else:
                        payload = b"ok:" + path.encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("X-Canary", "CANARY-HEADER")
                    self.end_headers()
                    self.wfile.write(payload)
                finally:
                    with outer.lock:
                        outer.in_flight -= 1

            do_GET = _serve
            do_POST = _serve

        self.server = _QuietServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def allowed(self) -> dict[str, AllowedHost]:
        return {"127.0.0.1": AllowedHost("http", self.port)}

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def connect_spy(monkeypatch):
    """Every (host, port) this process connects a socket to. Stray loopback clients cannot appear here."""
    seen: list[tuple[str, int]] = []
    real = socket.socket.connect
    real_ex = socket.socket.connect_ex

    def spy(self, address):
        seen.append(tuple(address[:2]))
        return real(self, address)

    def spy_ex(self, address):
        seen.append(tuple(address[:2]))
        return real_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", spy)
    monkeypatch.setattr(socket.socket, "connect_ex", spy_ex)
    return seen


@pytest.fixture()
def up():
    server = Upstream()
    yield server
    server.release.set()  # never leave a /hold handler parked
    server.stop()


class ManualClock:
    def __init__(self) -> None:
        self.t = 100.0

    def monotonic(self) -> float:
        return self.t

    def now_utc(self):  # not used by the pool
        raise AssertionError


def pooled(up: Upstream, **kwargs) -> PooledTransport:
    return PooledTransport(up.allowed(), **kwargs)


def get(transport, url, *, max_bytes=1 << 20, timeouts=T, body=None, method="GET"):
    return transport.request(method, url, headers={"X-Test": "1"}, body=body, timeouts=timeouts, max_bytes=max_bytes)


# ------------------------------------------------------------- reuse


def test_ten_sequential_requests_share_one_connection(up):
    transport = pooled(up)
    for i in range(10):
        resp = get(transport, up.url(f"/n{i}"))
        assert resp.status == 200 and resp.body == f"ok:/n{i}".encode()
    assert up.requests == 10 and up.connections == 1
    transport.close()


def test_headers_are_lowercased_and_a_post_body_is_sent(up):
    transport = pooled(up)
    resp = get(transport, up.url("/p"), method="POST", body=b'{"a":1}')
    assert resp.headers["x-canary"] == "CANARY-HEADER" and up.bodies[-1] == b'{"a":1}'
    transport.close()


def test_a_second_host_gets_its_own_pool(up):
    other = Upstream()
    try:
        transport = PooledTransport({"127.0.0.1": AllowedHost("http", up.port), "localhost": AllowedHost("http", other.port)})
        get(transport, up.url("/a"))
        get(transport, f"http://localhost:{other.port}/b")
        get(transport, up.url("/c"))
        assert (up.connections, other.connections) == (1, 1)
        transport.close()
    finally:
        other.stop()


# --------------------------------------------------------- concurrency


def test_eight_threads_never_exceed_four_connections_in_flight(up):
    transport = pooled(up, max_connections=4)
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(3):
                get(transport, up.url("/slow?t=0.1"))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert up.requests == 24
    assert 1 < up.max_in_flight <= 4
    assert up.connections <= 8  # connections are reused, never one per request
    transport.close()


def test_max_connections_two_caps_in_flight_at_two(up):
    transport = pooled(up, max_connections=2)

    def worker():
        get(transport, up.url("/slow?t=0.15"))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert up.requests == 6 and up.max_in_flight == 2
    transport.close()


class _RecordingSlots:
    """Wraps a pool's semaphore and remembers the timeout every acquire was given."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.timeouts: list[float | None] = []

    def acquire(self, blocking=True, timeout=None):
        self.timeouts.append(timeout)
        return self.inner.acquire(blocking, timeout)

    def release(self):
        self.inner.release()


def test_a_caller_that_cannot_get_a_slot_in_time_is_a_timeout_and_busy_calls_finish(up):
    transport = pooled(up, max_connections=1, pool_wait_s=0.3)
    pool = transport._pool_for(("http", "127.0.0.1", up.port))
    slots = pool.slots = _RecordingSlots(pool.slots)
    results: list[object] = []

    def busy():
        results.append(get(transport, up.url("/hold")).status)

    thread = threading.Thread(target=busy)
    thread.start()
    try:
        assert up.entered.wait(10)  # the busy call is inside the upstream, so it holds the only slot
        started = time.monotonic()
        with pytest.raises(TransportError) as err:
            get(transport, up.url("/x"))
        waited = time.monotonic() - started
        assert err.value.kind == "timeout"
        assert 0.25 <= waited < 0.3 + 1.0  # waited out pool_wait_s, not less, not several times more
        assert slots.timeouts == [0.3, 0.3]  # both callers were bounded by pool_wait_s, exactly
        # The busy call cannot finish until released, so timing out proves the pool did
        # not make the refused caller wait for it.
        assert thread.is_alive() and results == []
        up.release.set()
        thread.join(10)
        assert not thread.is_alive() and results == [200]
        assert up.requests == 1  # the refused caller never reached the upstream
        assert get(transport, up.url("/after")).status == 200  # the slot came back
    finally:
        up.release.set()  # a failed assert above must not leave a handler parked
        thread.join(10)
        transport.close()


def test_stray_loopback_traffic_is_not_counted_as_upstream_requests(up):
    """A foreign client on the test's port must not move any counter (the flake this fixes)."""
    transport = pooled(up)
    stray = http.client.HTTPConnection("127.0.0.1", up.port, timeout=5)
    stray.request("GET", "/", headers={"User-Agent": "node"})
    assert stray.getresponse().status == 404
    stray.close()
    assert get(transport, up.url("/ok")).status == 200
    assert (up.requests, up.connections, len(up.bodies)) == (1, 1, 1)
    assert up.max_in_flight == 1
    transport.close()


def test_a_slot_is_returned_after_every_kind_of_failure(up):
    transport = pooled(up, max_connections=1, pool_wait_s=0.5)
    for path, kind in (("/redirect", "redirect"), ("/big", "too_large")):
        with pytest.raises(TransportError) as err:
            get(transport, up.url(path), max_bytes=100)
        assert err.value.kind == kind
    with pytest.raises(TransportError) as err:
        get(transport, up.url("/slow?t=1.0"), timeouts=Timeouts(2.0, 0.2, 5.0))
    assert err.value.kind == "timeout"
    assert get(transport, up.url("/fine")).status == 200
    transport.close()


# ------------------------------------------------------------- staleness


def test_a_server_that_closes_after_every_response_still_answers_five_gets(up):
    transport = pooled(up)
    for i in range(5):
        resp = get(transport, up.url(f"/hangup{i}"))
        assert resp.status == 200 and resp.body == b"bye"
    assert up.requests >= 5  # a replaced stale connection may add a request; never a failure
    transport.close()


def peer_closed_idle_connection(up, transport) -> None:
    """Leave one pooled connection that the server has already closed."""
    assert get(transport, up.url("/hangup")).body == b"bye"
    assert idle_count(transport) == 1  # the client could not know: no Connection: close was sent
    time.sleep(0.1)  # let the server's close reach the idle socket


def test_a_dead_idle_connection_is_replaced_once_for_a_get(up):
    transport = pooled(up)
    peer_closed_idle_connection(up, transport)
    assert get(transport, up.url("/again")).body == b"ok:/again"
    assert up.connections == 2 and up.requests == 2
    transport.close()


def test_a_post_on_a_dead_reused_connection_is_connect_and_never_resent(up):
    transport = pooled(up)
    peer_closed_idle_connection(up, transport)
    before = up.requests
    with pytest.raises(TransportError) as err:
        get(transport, up.url("/order-like"), method="POST", body=b"{}")
    assert err.value.kind == "connect"
    assert up.requests == before  # the body was not delivered again on a fresh connection
    assert idle_count(transport) == 0
    transport.close()


def test_a_connection_idle_longer_than_idle_s_is_not_reused(up):
    clock = ManualClock()
    transport = pooled(up, idle_s=4.0, clock=clock)
    get(transport, up.url("/a"))
    clock.t += 3.9
    get(transport, up.url("/b"))
    assert up.connections == 1
    clock.t += 4.1
    get(transport, up.url("/c"))
    assert up.connections == 2
    transport.close()


# --------------------------------------------- what never returns to the pool


def idle_count(transport) -> int:
    return sum(len(p.idle) for p in transport._pools.values())


@pytest.mark.parametrize(
    "path, kwargs, kind",
    [
        ("/big", {"max_bytes": 100}, "too_large"),
        ("/slow?t=1.0", {"timeouts": Timeouts(2.0, 0.2, 5.0)}, "timeout"),
        ("/short", {}, "protocol"),
        ("/redirect", {}, "redirect"),
    ],
)
def test_a_failed_exchange_never_puts_its_connection_back(up, path, kwargs, kind):
    transport = pooled(up)
    with pytest.raises(TransportError) as err:
        get(transport, up.url(path), **kwargs)
    assert err.value.kind == kind
    assert idle_count(transport) == 0
    transport.close()


def test_the_byte_cap_holds_when_the_server_declares_no_length(up):
    transport = pooled(up)
    with pytest.raises(TransportError) as err:
        get(transport, up.url("/stream"), max_bytes=100)
    assert err.value.kind == "too_large" and idle_count(transport) == 0
    assert get(transport, up.url("/stream"), max_bytes=5000).body == b"s" * 5000
    transport.close()


def test_a_connection_close_response_is_delivered_but_not_pooled(up):
    transport = pooled(up)
    resp = get(transport, up.url("/close"))
    assert resp.body == b"closing" and idle_count(transport) == 0
    get(transport, up.url("/close"))
    assert up.connections == 2
    transport.close()


def test_a_good_response_is_pooled(up):
    transport = pooled(up)
    get(transport, up.url("/ok"))
    assert idle_count(transport) == 1
    transport.close()
    assert idle_count(transport) == 0


def test_after_close_nothing_new_is_opened(up, connect_spy):
    transport = pooled(up)
    transport.close()
    with pytest.raises(TransportError) as err:
        get(transport, up.url("/ok"))
    assert err.value.kind == "connect"
    assert connect_spy == [] and up.connections == 0  # no socket at all, not just no request


# ------------------------------------------------------------- rules


def test_a_redirect_is_never_followed(up):
    transport = pooled(up)
    with pytest.raises(TransportError) as err:
        get(transport, up.url("/redirect"))
    assert err.value.kind == "redirect"
    assert up.requests == 1


@pytest.mark.parametrize(
    "url, kind",
    [
        ("http://evil.example/x", "host_not_allowed"),
        ("http://user:pw@127.0.0.1:{port}/x", "host_not_allowed"),
        ("http:///x", "host_not_allowed"),
        ("https://127.0.0.1:{port}/x", "scheme_not_allowed"),
        ("http://127.0.0.1:1/x", "scheme_not_allowed"),
    ],
)
def test_a_refused_target_opens_no_socket(up, connect_spy, url, kind):
    transport = pooled(up)
    with pytest.raises(TransportError) as err:
        get(transport, url.format(port=up.port))
    assert err.value.kind == kind
    assert connect_spy == [] and up.connections == 0 and transport._pools == {}


def test_an_unreachable_host_is_connect_and_the_message_has_no_url_or_headers():
    transport = PooledTransport({"127.0.0.1": AllowedHost("http", 9)})  # the discard port: refused locally
    with pytest.raises(TransportError) as err:
        transport.request(
            "GET", "http://127.0.0.1:9/p?q=CANARY-QUERY", headers={"Authorization": "CANARY-AUTH"},
            body=b"CANARY-BODY", timeouts=Timeouts(1.0, 1.0, 2.0), max_bytes=100,
        )
    assert err.value.kind == "connect"
    text = f"{err.value!s} {err.value!r}"
    for leaked in ("CANARY-QUERY", "CANARY-AUTH", "CANARY-BODY", "/p"):
        assert leaked not in text


def test_an_error_message_never_holds_a_header_or_body_of_the_response(up):
    transport = pooled(up)
    with pytest.raises(TransportError) as err:
        get(transport, up.url("/big"), max_bytes=10)
    assert "CANARY-HEADER" not in str(err.value) and "zzzz" not in str(err.value)
    transport.close()


def test_the_pool_refuses_a_nonsensical_size():
    with pytest.raises(ValueError):
        PooledTransport(max_connections=0)


def test_no_proxy_environment_is_read(up, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    transport = pooled(up)
    assert get(transport, up.url("/direct")).status == 200
    transport.close()


# ---------------------------------- the 61-01 transport rules, both transports


@pytest.fixture(params=["http_client", "pooled"])
def either(request, up):
    if request.param == "http_client":
        return HttpClientTransport(up.allowed())
    transport = pooled(up)
    request.addfinalizer(transport.close)
    return transport


def test_both_transports_return_status_headers_and_body(either, up):
    resp = get(either, up.url("/same"))
    assert (resp.status, resp.body) == (200, b"ok:/same") and resp.headers["x-canary"] == "CANARY-HEADER"


def test_both_transports_enforce_the_byte_cap(either, up):
    with pytest.raises(TransportError) as err:
        get(either, up.url("/big"), max_bytes=100)
    assert err.value.kind == "too_large"
    assert get(either, up.url("/big"), max_bytes=5000).body == b"z" * 5000


def test_both_transports_enforce_the_deadline(either, up):
    started = time.monotonic()
    with pytest.raises(TransportError) as err:
        get(either, up.url("/slow?t=2.0"), timeouts=Timeouts(2.0, 0.3, 0.5))
    assert err.value.kind == "timeout" and time.monotonic() - started < 1.5


def test_both_transports_refuse_redirects_and_unlisted_hosts(either, up):
    with pytest.raises(TransportError) as err:
        get(either, up.url("/redirect"))
    assert err.value.kind == "redirect"
    with pytest.raises(TransportError) as err:
        get(either, "http://other.example/x")
    assert err.value.kind == "host_not_allowed"


def test_the_pool_refuses_a_cut_short_body(up):
    # HttpClientTransport (61-01) hands back the partial body; only the relay's pool is held to this.
    transport = pooled(up)
    with pytest.raises(TransportError) as err:
        get(transport, up.url("/short"))
    assert err.value.kind == "protocol"
    transport.close()

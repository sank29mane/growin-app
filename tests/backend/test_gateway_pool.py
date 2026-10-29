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
import select
import socket
import ssl
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway" / "vm"))

from gateway_vm.pool import PooledTransport, _CountingSocket, _peer_has_spoken  # noqa: E402
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
                    if path.startswith("/reset"):  # read the request, then RST before any response byte
                        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                        self.close_connection = True
                        self.rfile.close()
                        self.wfile.close()
                        self.connection.close()
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


def wait_until_peer_closed(transport, timeout: float = 5.0) -> None:
    """Block until every idle socket is readable (the server's FIN or RST has arrived). No sleep guess."""
    for pool in transport._pools.values():
        for conn, _last_used in pool.idle:
            readable, _, _ = select.select([conn.sock], [], [], timeout)
            assert readable, "the server never closed the idle connection"


def test_a_server_that_closes_after_every_response_still_answers_five_gets(up):
    # The probe, not a resend, replaces each dead idle connection. The server's close is awaited:
    # a request written in the instant before the FIN lands is the unavoidable window, and is a
    # "protocol" error by design (see the double-submit tests), not something this test may paper over.
    transport = pooled(up)
    for i in range(5):
        resp = get(transport, up.url(f"/hangup{i}"))
        assert resp.status == 200 and resp.body == b"bye"
        wait_until_peer_closed(transport)
    assert up.requests == 5 and up.connections == 5  # exactly one request per request, never a replay
    transport.close()


def peer_closed_idle_connection(up, transport) -> None:
    """Leave one pooled connection that the server has already closed."""
    assert get(transport, up.url("/hangup")).body == b"bye"
    assert idle_count(transport) == 1  # the client could not know: no Connection: close was sent
    wait_until_peer_closed(transport)  # the server's close has reached the idle socket


def test_a_dead_idle_connection_is_replaced_once_for_a_get(up, connect_spy):
    transport = pooled(up)
    peer_closed_idle_connection(up, transport)
    connect_spy.clear()
    assert get(transport, up.url("/again")).body == b"ok:/again"
    assert up.connections == 2 and up.requests == 2
    assert connect_spy == [("127.0.0.1", up.port)]  # the probe discarded the dead socket: one fresh connect
    assert idle_count(transport) == 1
    transport.close()


def test_a_dead_idle_connection_is_discarded_by_the_probe_even_for_a_post(up):
    # Nothing is written to a connection the probe rejects, so a fresh one is not a resend.
    transport = pooled(up)
    peer_closed_idle_connection(up, transport)
    resp = get(transport, up.url("/order-like"), method="POST", body=b'{"x":1}')
    assert resp.status == 200
    assert up.requests == 2 and up.bodies[1:] == [b'{"x":1}']  # the body reached the server exactly once
    transport.close()


class _BrokenWriteSocket:
    """Delegates to a real socket, but every write fails the way a peer-closed one does."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.writes = 0

    def send(self, data, flags=0):  # the pool's counting wrapper sends through send, so zero bytes leave
        self.writes += 1
        raise BrokenPipeError

    def __getattr__(self, name):
        return getattr(self._inner, name)


def break_the_idle_socket_write(transport) -> _BrokenWriteSocket:
    """The idle connection still looks alive to the probe (not readable) but the write will fail."""
    (pool,) = transport._pools.values()
    ((conn, _last_used),) = pool.idle
    conn.sock = _BrokenWriteSocket(conn.sock)
    return conn.sock


def test_a_write_phase_failure_on_a_reused_get_is_retried_exactly_once(up, connect_spy):
    transport = pooled(up)
    get(transport, up.url("/first"))
    broken = break_the_idle_socket_write(transport)
    connect_spy.clear()
    assert get(transport, up.url("/again")).body == b"ok:/again"
    assert broken.writes == 1  # the dead write was attempted once, never repeated on that socket
    assert up.requests == 2 and up.connections == 2  # /first, then /again once on a fresh connection
    assert connect_spy == [("127.0.0.1", up.port)]
    transport.close()


def test_a_write_phase_failure_on_a_reused_post_is_connect_and_never_resent(up, connect_spy):
    transport = pooled(up)
    get(transport, up.url("/first"))
    break_the_idle_socket_write(transport)
    connect_spy.clear()
    with pytest.raises(TransportError) as err:
        get(transport, up.url("/order-like"), method="POST", body=b"{}")
    assert err.value.kind == "connect"
    assert up.requests == 1 and connect_spy == []  # no replacement connection was even opened
    assert idle_count(transport) == 0
    transport.close()


@pytest.mark.parametrize("method, body", [("GET", None), ("POST", b'{"x":1}')])
def test_a_reset_after_the_server_read_a_reused_request_is_protocol_and_never_resent(up, connect_spy, method, body):
    """The double-submit: the server read /again and died before any response byte."""
    transport = pooled(up)
    get(transport, up.url("/first"))
    assert idle_count(transport) == 1
    connect_spy.clear()
    with pytest.raises(TransportError) as err:
        get(transport, up.url("/reset/again"), method=method, body=body)
    assert err.value.kind == "protocol"
    assert up.requests == 2 and up.connections == 1  # /first and /reset/again, each exactly once
    assert connect_spy == []  # no replacement connection was opened to resend on
    assert idle_count(transport) == 0
    assert get(transport, up.url("/after")).status == 200  # the slot and the pool are healthy
    transport.close()


class _HeaderThenResetServer:
    """Raw loopback server. /first gets a 200; any other request is read up to the end
    of its headers (the body is never read), then the connection is reset with RST."""

    def __init__(self) -> None:
        self.request_lines: list[str] = []
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        buf = b""
        try:
            while True:
                while b"\r\n\r\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                head, _, buf = buf.partition(b"\r\n\r\n")
                line = head.split(b"\r\n")[0].decode()
                if b"x-test" not in head.lower():  # a stray local client
                    conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                    continue
                self.request_lines.append(line)
                if " /first" in line:
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                    continue
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                return
        except OSError:
            pass
        finally:
            conn.close()  # with SO_LINGER 0 set above this is the RST

    def stop(self) -> None:
        self.listener.close()


class _WaitForResetAfterFirstSend:
    """Real socket underneath. After the first send (the headers) it waits until the
    server's RST has arrived, so the next send (the body) fails with a real error."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.sends = 0

    def send(self, data, flags=0):
        self.sends += 1
        taken = self._inner.send(data, flags)  # raises for real once the RST has arrived
        if self.sends == 1:
            readable, _, _ = select.select([self._inner], [], [], 5)
            assert readable, "the server never reset the connection"
        return taken

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_a_reset_between_the_headers_and_the_body_is_protocol_and_never_resent(connect_spy, method):
    """http.client sends headers and body separately (Breeze sends GETs with bodies).

    The server read the headers, then reset before the body was written. The write
    raised, but the request was partly delivered, so it is neither retried nor 'connect'.
    """
    server = _HeaderThenResetServer()
    transport = PooledTransport({"127.0.0.1": AllowedHost("http", server.port)})
    try:
        url = f"http://127.0.0.1:{server.port}"
        assert get(transport, url + "/first").body == b"ok"
        (pool,) = transport._pools.values()
        ((conn, _last_used),) = pool.idle
        spy_sock = conn.sock = _WaitForResetAfterFirstSend(conn.sock)
        connect_spy.clear()
        with pytest.raises(TransportError) as err:
            get(transport, url + "/again", method=method, body=b'{"x":1}')
        assert err.value.kind == "protocol"
        assert spy_sock.sends == 2  # the headers went, then the body write failed for real
        assert server.request_lines == ["GET /first HTTP/1.1",f"{method} /again HTTP/1.1"]
        assert connect_spy == []  # no replacement connection was opened to resend on
        assert idle_count(transport) == 0
    finally:
        transport.close()
        server.stop()


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


# ------------------------------------------------------------- the probe


@pytest.fixture()
def socket_pair():
    a, b = socket.socketpair()
    yield a, b
    a.close()
    b.close()


def conn_with(sock) -> http.client.HTTPConnection:
    conn = http.client.HTTPConnection("127.0.0.1")
    conn.sock = sock
    return conn


def test_the_probe_calls_a_quiet_idle_socket_alive_and_a_spoken_or_closed_one_dead(socket_pair):
    a, b = socket_pair
    conn = conn_with(a)
    assert _peer_has_spoken(conn) is False
    b.send(b"x")  # bytes nobody asked for
    assert _peer_has_spoken(conn) is True
    a.recv(1)
    assert _peer_has_spoken(conn) is False
    b.close()  # EOF
    assert _peer_has_spoken(conn) is True


def test_the_probe_sees_through_the_counting_wrapper(socket_pair):
    a, b = socket_pair
    conn = conn_with(_CountingSocket(a))
    assert _peer_has_spoken(conn) is False
    b.close()
    assert _peer_has_spoken(conn) is True


def test_the_probe_treats_a_missing_or_closed_socket_as_dead(socket_pair):
    a, _b = socket_pair
    assert _peer_has_spoken(conn_with(None)) is True
    a.close()
    assert _peer_has_spoken(conn_with(a)) is True


def test_the_probe_works_for_descriptors_at_or_above_1024(socket_pair):
    """select.select raises ValueError there, which used to read as 'dead' and turned pooling off."""
    import fcntl
    import resource

    a, b = socket_pair
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = 2048
    if soft < want:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want if hard == resource.RLIM_INFINITY else min(want, hard), hard))
        except (ValueError, OSError):
            pytest.skip("cannot raise the descriptor limit")
    try:
        try:
            fd = fcntl.fcntl(a.fileno(), fcntl.F_DUPFD, 1100)
        except OSError:
            pytest.skip("cannot get a descriptor above 1024")
        high = socket.socket(fileno=fd)
        try:
            assert high.fileno() >= 1024
            conn = conn_with(high)
            assert _peer_has_spoken(conn) is False  # quiet and alive
            b.send(b"x")
            assert _peer_has_spoken(conn) is True
        finally:
            high.close()
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_the_probe_calls_a_tls_socket_with_buffered_decrypted_bytes_dead(socket_pair):
    """SSL can hold decrypted bytes the descriptor no longer shows as readable."""

    class FakeTLS(ssl.SSLSocket):
        buffered = 3

        def pending(self):
            return self.buffered

    a, _b = socket_pair
    fake = FakeTLS.__new__(FakeTLS)
    socket.socket.__init__(fake, fileno=a.detach())  # SSLSocket.__init__ refuses direct use; this is a quiet open fd
    try:
        conn = conn_with(fake)
        assert _peer_has_spoken(conn) is True
        FakeTLS.buffered = 0
        assert _peer_has_spoken(conn) is False
    finally:
        FakeTLS.buffered = 3
        fake.close()


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

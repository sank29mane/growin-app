"""HttpClientTransport against real loopback sockets (127.0.0.1 only).

Proves the allowlist, redirect refusal, deadlines, size cap and proxy
behaviour that the gateway design relies on. No external network.
"""

from __future__ import annotations

import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from gateway_vm import transport as tr  # noqa: E402

FAKE_TOKEN = "FAKE-session-token-abc123"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # keep test output quiet
        pass

    def _send(self, status: int, payload: bytes, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        self.server.paths.append(self.path)  # type: ignore[attr-defined]
        path = self.path.split("?", 1)[0]
        try:
            if path == "/ok":
                self._send(200, b"ok", {"X-Custom-Header": "Yes"})
            elif path == "/redirect":
                self._send(302, b"", {"Location": "/ok"})
            elif path == "/redirect-external":
                self._send(302, b"", {"Location": "http://other.invalid/steal"})
            elif path == "/server-error":
                self._send(500, b"oops")
            elif path == "/slow":
                time.sleep(3)
                self._send(200, b"late")
            elif path == "/trickle":
                self.send_response(200)
                self.send_header("Content-Length", "25")
                self.end_headers()
                for _ in range(25):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.2)
            elif path == "/big":
                self._send(200, b"z" * (2 * 1024 * 1024))
            elif path == "/bigstream":
                # No Content-Length: the size cap must trip while streaming.
                self.send_response(200)
                self.send_header("Connection", "close")
                self.end_headers()
                for _ in range(32):
                    self.wfile.write(b"z" * 65536)
                self.close_connection = True
            elif path == "/echo":
                length = int(self.headers.get("Content-Length", "0"))
                data = self.rfile.read(length) if length else b""
                self._send(200, f"len={length};".encode() + data)
            else:
                self._send(404, b"nope")
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.paths = []  # type: ignore[attr-defined]
    thread = threading.Thread(
        target=lambda: srv.serve_forever(poll_interval=0.02), daemon=True
    )
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def make(server) -> tuple[tr.HttpClientTransport, str]:
    port = server.server_address[1]
    transport = tr.HttpClientTransport({"127.0.0.1": tr.AllowedHost("http", port)})
    return transport, f"http://127.0.0.1:{port}"


def call(transport, url, *, headers=None, body=None, timeouts=None, max_bytes=1024 * 1024):
    return transport.request(
        "GET", url, headers=headers or {}, body=body,
        timeouts=timeouts or tr.Timeouts(connect_s=2.0, read_s=5.0, total_s=10.0),
        max_bytes=max_bytes,
    )


# ----------------------------------------------------------------- behaviour


def test_ok_returns_status_headers_and_body(server):
    transport, base = make(server)
    resp = call(transport, base + "/ok")
    assert resp.status == 200
    assert resp.body == b"ok"
    assert resp.headers["x-custom-header"] == "Yes"  # names are lower-cased


def test_non_redirect_error_statuses_are_returned_not_raised(server):
    transport, base = make(server)
    assert call(transport, base + "/server-error").status == 500
    assert call(transport, base + "/missing").status == 404


def test_redirect_is_refused_and_never_followed(server):
    transport, base = make(server)
    with pytest.raises(tr.TransportError) as err:
        call(transport, base + "/redirect", headers={"X-SessionToken": FAKE_TOKEN})
    assert err.value.kind == "redirect"
    assert "/ok" not in server.paths
    assert FAKE_TOKEN not in str(err.value)


def test_redirect_to_another_host_is_refused(server):
    transport, base = make(server)
    with pytest.raises(tr.TransportError) as err:
        call(transport, base + "/redirect-external")
    assert err.value.kind == "redirect"


def test_read_timeout(server):
    transport, base = make(server)
    start = time.monotonic()
    with pytest.raises(tr.TransportError) as err:
        call(transport, base + "/slow", timeouts=tr.Timeouts(2.0, 0.5, 10.0))
    assert err.value.kind == "timeout"
    assert time.monotonic() - start < 2.0


def test_total_deadline_stops_a_trickling_body(server):
    transport, base = make(server)
    start = time.monotonic()
    with pytest.raises(tr.TransportError) as err:
        call(transport, base + "/trickle", timeouts=tr.Timeouts(2.0, 5.0, 1.0))
    assert err.value.kind == "timeout"
    assert time.monotonic() - start < 2.5


def test_declared_oversize_body_is_rejected(server):
    transport, base = make(server)
    with pytest.raises(tr.TransportError) as err:
        call(transport, base + "/big", max_bytes=1024 * 1024)
    assert err.value.kind == "too_large"


def test_streamed_oversize_body_is_rejected(server):
    transport, base = make(server)
    with pytest.raises(tr.TransportError) as err:
        call(transport, base + "/bigstream", max_bytes=1024 * 1024)
    assert err.value.kind == "too_large"


def test_body_within_cap_is_returned_whole(server):
    transport, base = make(server)
    resp = call(transport, base + "/big", max_bytes=4 * 1024 * 1024)
    assert len(resp.body) == 2 * 1024 * 1024


def test_get_body_arrives_intact_with_content_length(server):
    transport, base = make(server)
    payload = b'{"SessionToken":"abc","AppKey":"k"}'
    resp = call(transport, base + "/echo", body=payload)
    assert resp.body == b"len=%d;" % len(payload) + payload


def test_connection_refused_is_a_connect_error():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    transport = tr.HttpClientTransport({"127.0.0.1": tr.AllowedHost("http", port)})
    with pytest.raises(tr.TransportError) as err:
        call(transport, f"http://127.0.0.1:{port}/ok", headers={"X-SessionToken": FAKE_TOKEN})
    assert err.value.kind == "connect"
    assert FAKE_TOKEN not in str(err.value)


# ------------------------------------------------------------- allowlisting


@pytest.fixture
def no_sockets(monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("socket.create_connection must not be reached")

    monkeypatch.setattr(socket, "create_connection", fail)


def test_unlisted_host_never_opens_a_socket(no_sockets):
    transport = tr.HttpClientTransport()
    with pytest.raises(tr.TransportError) as err:
        call(transport, "https://example.invalid/anything")
    assert err.value.kind == "host_not_allowed"


def test_scheme_and_port_must_match_the_allowlist(no_sockets):
    transport = tr.HttpClientTransport()
    for url in (
        "http://api.icicidirect.com/x",  # https-only host over http
        "https://api.icicidirect.com:8443/x",  # wrong port
        "https://metadata.google.internal/x",  # http-only host over https
    ):
        with pytest.raises(tr.TransportError) as err:
            call(transport, url)
        assert err.value.kind == "scheme_not_allowed", url


def test_userinfo_urls_are_rejected(no_sockets):
    transport = tr.HttpClientTransport()
    with pytest.raises(tr.TransportError) as err:
        call(transport, "https://user:pw@api.icicidirect.com/x")
    assert err.value.kind == "host_not_allowed"


def test_userinfo_cannot_smuggle_an_allowed_host(no_sockets):
    transport = tr.HttpClientTransport()
    with pytest.raises(tr.TransportError):
        call(transport, "https://api.icicidirect.com@example.invalid/x")


def test_default_allowlist_is_exactly_the_documented_hosts():
    hosts = dict(tr.DEFAULT_ALLOWED_HOSTS)
    assert set(hosts) == {
        "api.icicidirect.com", "breezeapi.icicidirect.com", "directlink.icicidirect.com",
        "secretmanager.googleapis.com", "api.ipify.org", "metadata.google.internal",
    }
    for host, rule in hosts.items():
        if host == "metadata.google.internal":
            assert (rule.scheme, rule.port) == ("http", 80)
        else:
            assert (rule.scheme, rule.port) == ("https", 443)


def test_proxy_environment_is_ignored(server, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    transport, base = make(server)
    assert call(transport, base + "/ok").body == b"ok"


# ---------------------------------------------------------------------- TLS


def test_tls_context_is_strict():
    ctx = tr._tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_https_requests_use_that_context(monkeypatch):
    built = []
    real = tr._tls_context

    def spy():
        ctx = real()
        built.append(ctx)
        return ctx

    def refuse(*_a, **_k):
        raise OSError("blocked in test")

    monkeypatch.setattr(tr, "_tls_context", spy)
    monkeypatch.setattr(socket, "create_connection", refuse)
    transport = tr.HttpClientTransport()
    with pytest.raises(tr.TransportError) as err:
        call(transport, "https://api.icicidirect.com/x")
    assert err.value.kind == "connect"
    assert len(built) == 1
    assert built[0].verify_mode == ssl.CERT_REQUIRED


def test_error_text_carries_only_kind_and_host():
    err = tr.TransportError("timeout", "api.icicidirect.com")
    assert str(err) == "timeout:api.icicidirect.com"
    assert err.kind == "timeout" and err.host == "api.icicidirect.com"


# -------------------------------------------------------------------- clock


def test_system_clock_is_utc_aware_and_monotonic():
    clock = tr.SystemClock()
    now = clock.now_utc()
    assert now.tzinfo is not None and now.utcoffset().total_seconds() == 0
    a = clock.monotonic()
    assert clock.monotonic() >= a

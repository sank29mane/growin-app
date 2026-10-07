"""Metadata kill reader against a loopback fake (Phase 63-01, Task 2).

The fake listens on 127.0.0.1 only. The real metadata server is never touched:
the default base URL is asserted, never used.
"""

from __future__ import annotations

import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from gateway_vm.orders import kill  # noqa: E402

INSTANCE_PATH = "/computeMetadata/v1/instance/attributes/growin-order-relay"
PROJECT_PATH = "/computeMetadata/v1/project/attributes/growin-order-relay"


class Fake(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.mode = {"status": 200, "body": b"enabled", "delay": 0.0, "headers": {}}
        self.project = (200, b"enabled")
        self.requests: list[tuple[str, dict[str, str]]] = []

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):  # noqa: N802
        server: Fake = self.server  # type: ignore[assignment]
        server.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        if self.path == PROJECT_PATH:
            status, body = server.project
        elif self.path == INSTANCE_PATH:
            if server.mode["delay"]:
                time.sleep(server.mode["delay"])
            status, body = server.mode["status"], server.mode["body"]
        else:
            status, body = 404, b"not found"
        try:
            self.send_response(status)
            for key, value in server.mode["headers"].items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture
def fake():
    server = Fake()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def read(server: Fake) -> kill.KillState:
    return kill.MetadataKillReader(server.base).read()


def test_exact_enabled_body_passes_and_sends_the_flavor_header(fake):
    state = read(fake)
    assert state.enabled is True and state.label == "enabled"
    path, headers = fake.requests[0]
    assert path == INSTANCE_PATH
    assert headers["metadata-flavor"] == "Google"


@pytest.mark.parametrize(
    "status,body",
    [
        (404, b"not found"),
        (404, b"enabled"),
        (503, b"enabled"),
        (500, b"enabled"),
        (204, b""),
        (200, b"ENABLED"),
        (200, b"Enabled"),
        (200, b"enabled\n"),
        (200, b" enabled"),
        (200, b"enabled "),
        (200, b"enabled\r\n"),
        (200, b""),
        (200, b"disabled"),
        (200, b"true"),
        (200, b"enabledenabled"),
        (200, b"enabled" + b"x" * 200),
    ],
)
def test_anything_but_the_exact_body_blocks(fake, status, body):
    fake.mode.update(status=status, body=body)
    assert read(fake).enabled is False


def test_slow_metadata_server_blocks_on_the_timeout(fake):
    fake.mode["delay"] = 1.5
    started = time.monotonic()
    state = read(fake)
    assert state.enabled is False and state.label == "unreadable"
    assert time.monotonic() - started < 1.45  # 1 s timeout, not the 1.5 s reply


def test_redirect_is_not_followed(fake):
    fake.mode.update(status=302, body=b"", headers={"Location": PROJECT_PATH})
    assert read(fake).enabled is False
    assert [p for p, _ in fake.requests] == [INSTANCE_PATH]


def test_instance_key_missing_while_project_key_exists_blocks(fake):
    fake.mode.update(status=404, body=b"not found")
    fake.project = (200, b"enabled")
    assert read(fake).enabled is False
    assert PROJECT_PATH not in [p for p, _ in fake.requests]  # never even consulted


def test_connection_refused_blocks():
    server = Fake()
    base = server.base
    server.server_close()  # nothing listening now
    assert kill.MetadataKillReader(base).read().enabled is False


def test_every_read_hits_the_server_and_nothing_is_cached(fake):
    reader = kill.MetadataKillReader(fake.base)
    assert reader.read().enabled is True
    assert reader.read().enabled is True
    assert len(fake.requests) == 2
    fake.mode["body"] = b"blocked"
    assert reader.read().enabled is False  # flips immediately, no stale enabled
    fake.mode["body"] = b"enabled"
    assert reader.read().enabled is True
    assert len(fake.requests) == 4


@pytest.mark.parametrize(
    "url",
    ["https://metadata.google.internal", "ftp://x", "http://user:pw@127.0.0.1:1", "http://", "metadata"],
)
def test_reader_refuses_odd_base_urls(url):
    with pytest.raises(ValueError):
        kill.MetadataKillReader(url)


def test_default_points_at_the_documented_metadata_host_and_path_only():
    assert kill.DEFAULT_BASE_URL == "http://metadata.google.internal"
    assert kill.KILL_PATH == INSTANCE_PATH
    assert kill.TIMEOUT_SECONDS == 1.0 and kill.ENABLED_BODY == b"enabled"

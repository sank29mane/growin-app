"""Phase 64-04 Task 2: the fake signer and the loopback-only fakes the D-09 offline exercises stand on.

TEST ONLY. This module lives under ``tests/`` and nothing under ``backend/`` or ``gateway/`` may import it
(``test_shadow_india_exercises`` scans for that). What is here:

* ``FakeSigner``: a P-256 key generated in memory for ONE test. Only the public half is ever written, and only under
  that test's ``tmp_path``. It is never a committed vector key, never a Keychain item and never a ``private/`` file.
* ``ExerciseStack``: the 63 order stack with the fake signer's public key pinned as the VM's order key, and a kill
  reader whose base URL has to be loopback (``require_loopback``) before the VM sees it.
* ``ExerciseMetadata``: a loopback metadata server that can answer 200, 404 or 503, serve any body, or answer too
  slowly (a timeout). GET only; any other method is recorded and refused.
* ``ConnectionSpy``: every ``http.client`` connection the process opens is recorded, and one that is not loopback
  is refused before a socket exists, so a mistake here cannot reach a real metadata server.

Nothing here reaches a network, a broker, the VM, GCP or a secret.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from test_gateway_orders_routes import KILL_PATH, MASTER_URL, MetadataFake, OrderStack  # first: it puts gateway_vm on sys.path
from gateway_vm.orders.routes import OrderSettings, build_order_service
from gateway_vm.orders.verify import TEST_ONLY_KEY_X963_HEX

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "backend" / "fixtures" / "relay_orders"


class NotLoopback(AssertionError):
    """An exercise tried to point something at an address that is not this machine."""


def require_loopback(url: str) -> str:
    """``url`` unchanged when its host is a loopback IP literal. Hostnames are refused: nothing here resolves a name."""

    host = urlsplit(url).hostname
    try:
        loopback = host is not None and ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise NotLoopback(f"not a loopback address: {host!r}")
    return url


def is_loopback_host(host: str | None) -> bool:
    try:
        return host is not None and ip_address(host).is_loopback
    except ValueError:
        return False


# --- the fake signer ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeSigner:
    """A key made for one test. ``as_vector`` has the shape ``ShadowWorld`` and ``sign`` take (same field names)."""

    private_scalar_hex: str
    public_key_x963_hex: str
    key_id: str

    @classmethod
    def generate(cls) -> "FakeSigner":
        private = ec.generate_private_key(ec.SECP256R1())
        x963 = private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return cls(
            private_scalar_hex=format(private.private_numbers().private_value, "064x"),
            public_key_x963_hex=x963.hex(),
            key_id=hashlib.sha256(x963).hexdigest(),
        )

    def as_vector(self) -> dict[str, str]:
        return {
            "key_id": self.key_id,
            "public_key_x963_hex": self.public_key_x963_hex,
            "private_scalar_hex": self.private_scalar_hex,
        }

    def sign(self, message: bytes) -> bytes:
        private = ec.derive_private_key(int(self.private_scalar_hex, 16), ec.SECP256R1())
        return private.sign(message, ec.ECDSA(hashes.SHA256()))

    def sign_b64(self, message: bytes) -> str:
        return base64.b64encode(self.sign(message)).decode("ascii")

    def write_vm_key(self, path: Path) -> Path:
        """The VM's pinned-key file: the PUBLIC key and its id, mode 0600, under the caller's tmp directory."""

        body = json.dumps({"key_id": self.key_id, "public_key_x963_hex": self.public_key_x963_hex})
        path.write_text(body, encoding="ascii")
        os.chmod(path, 0o600)
        return path


def pinned_fixture_keys() -> tuple[set[str], set[str]]:
    """``(key_ids, public key hex strings)`` of every key a committed fixture or the VM's test list pins."""

    ids: set[str] = set()
    public: set[str] = set(TEST_ONLY_KEY_X963_HEX)
    vectors = json.loads((FIXTURES / "signing_vectors.json").read_text(encoding="utf-8"))
    for key in vectors["keys"].values():
        ids.add(key["key_id"])
        public.add(key["public_key_x963_hex"])
    swift = json.loads((FIXTURES / "swift_signature.json").read_text(encoding="utf-8"))
    ids.add(swift["key_id"])
    public.add(swift["public_key_x963_hex"])
    return ids, public


# --- loopback metadata ----------------------------------------------------------------------------------------------


class _ExerciseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def _record(self) -> None:
        self.server.methods.append(self.command)  # type: ignore[attr-defined]

    def do_GET(self):  # noqa: N802
        import time

        self._record()
        server = self.server
        if server.delay:  # type: ignore[attr-defined]
            time.sleep(server.delay)  # type: ignore[attr-defined]
        status = server.status  # type: ignore[attr-defined]
        data = server.body if server.body is not None else b""  # type: ignore[attr-defined]
        if self.path != KILL_PATH:
            status, data = 404, b"not found"
        try:
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:  # the VM's reader gave up first: that is the timeout the row exercises
            pass

    def _refuse(self) -> None:
        self._record()
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _refuse  # noqa: N815


class ExerciseMetadata(MetadataFake):
    """The 63 metadata fake with a status and a delay. ``body`` b"blocked" is the production kill flag's value."""

    def __init__(self, *, body: bytes | None = b"blocked", status: int = 200, delay: float = 0.0) -> None:
        super().__init__()
        self.RequestHandlerClass = _ExerciseHandler
        self.body = body
        self.status = status
        self.delay = delay

    def handle_error(self, request, client_address):  # a reader that hung up (the timeout row) is not an error here
        pass


# --- the connection spy ---------------------------------------------------------------------------------------------


class ConnectionSpy:
    """Records every ``http.client`` connection's (host, port). A non-loopback host is recorded and refused."""

    def __init__(self, monkeypatch) -> None:
        self.connections: list[tuple[str, int | None]] = []
        self.refused: list[str] = []
        real = http.client.HTTPConnection.__init__
        spy = self

        def init(conn, host, port=None, *args, **kwargs):
            spy.connections.append((host, port))
            if not is_loopback_host(host):
                spy.refused.append(str(host))
                raise NotLoopback(f"refused a connection to {host!r} before a socket existed")
            real(conn, host, port, *args, **kwargs)

        monkeypatch.setattr(http.client.HTTPConnection, "__init__", init)

    def assert_all_loopback(self, metadata: MetadataFake) -> None:
        assert self.refused == [], f"a non-loopback host was asked for: {self.refused}"
        port = metadata.server_address[1]
        assert {(host, p) for host, p in self.connections} <= {("127.0.0.1", port)}


# --- the VM stack ---------------------------------------------------------------------------------------------------


class ExerciseStack(OrderStack):
    """``OrderStack`` pinned to a fake signer's public key, with the kill reader aimed at loopback only.

    ``signer`` is set by ``ExerciseStack.with_signer``: ``OrderStack`` builds its service inside ``__init__``, so the
    signer has to be known before that runs.
    """

    signer: FakeSigner | None = None

    @classmethod
    def with_signer(cls, signer: FakeSigner):
        class Bound(cls):  # type: ignore[valid-type, misc]
            pass

        Bound.signer = signer
        return Bound

    def kill_base_url(self) -> str:
        return require_loopback(self.metadata.base)

    def build_orders(self):
        assert self.signer is not None, "an exercise stack is pinned to a fake signer made for its own test"
        self.signer.write_vm_key(self.key_path)
        return build_order_service(
            OrderSettings(
                state_dir=self.state_dir,
                limits_path=str(self.limits_path),
                key_path=str(self.key_path),
                owner_uid=os.getuid(),
                allow_test_key=False,  # the fake key is not a committed key: the production loader's rule applies
                kill_base_url=self.kill_base_url(),
                holidays=self.holidays,
            ),
            session=self.session,
            breeze=self.breeze,
            credentials=self.credentials,
            budget=self.budget,
            events=self.events,
            clock=self.clock,
            security_master_url=MASTER_URL,
        )


def stack_state_dir_is_under(stack: Any, root: Path) -> bool:
    return Path(stack.state_dir).resolve().is_relative_to(root.resolve())

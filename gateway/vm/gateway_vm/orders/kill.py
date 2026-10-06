"""Metadata kill switch reader (D-03, P-11, T-63-05).

GET <base>/computeMetadata/v1/instance/attributes/growin-order-relay with
``Metadata-Flavor: Google`` and a 1 s timeout, on every order check, with no
cache. Only HTTP 200 with the exact body ``enabled`` passes. A 404, 503, a
timeout, a redirect, any other body (``ENABLED``, ``enabled\\n``, `` enabled``,
empty) or any exception blocks. Only the instance-level key is read: a
project-level key of the same name is never consulted, so it cannot enable.

The base URL is injectable so tests talk to a loopback fake. The default
points at the real metadata server and must never be used from a test.
"""

from __future__ import annotations

import http.client
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "http://metadata.google.internal"
KILL_PATH = "/computeMetadata/v1/instance/attributes/growin-order-relay"
ENABLED_BODY = b"enabled"
TIMEOUT_SECONDS = 1.0
_MAX_BODY = 64


@dataclass(frozen=True)
class KillState:
    enabled: bool
    label: str  # "enabled" | "blocked" | "unreadable"


BLOCKED = KillState(False, "blocked")
UNREADABLE = KillState(False, "unreadable")
ENABLED = KillState(True, "enabled")


class MetadataKillReader:
    def __init__(
        self, base_url: str = DEFAULT_BASE_URL, *, timeout: float = TIMEOUT_SECONDS
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "http" or not parts.hostname or parts.username or parts.password:
            raise ValueError("kill reader needs a plain http base URL with no userinfo")
        self._host = parts.hostname
        self._port = parts.port or 80
        self._timeout = timeout

    def read(self) -> KillState:
        """Never raises. Anything but the exact enabled body is blocked."""
        conn: http.client.HTTPConnection | None = None
        try:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
            conn.request("GET", KILL_PATH, headers={"Metadata-Flavor": "Google"})
            response = conn.getresponse()
            body = response.read(_MAX_BODY + 1)
            if response.status == 200 and body == ENABLED_BODY:
                return ENABLED
            return BLOCKED
        except Exception:
            return UNREADABLE
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # pragma: no cover
                    pass

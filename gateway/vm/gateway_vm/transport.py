"""HTTP transport and clock seams for the VM gateway.

Two I/O seams live here: the network (Transport) and time (Clock). The real
transport is built on http.client so that it can enforce, in code, what the
gateway design promises:

- only allowlisted hosts, each with a fixed scheme and port;
- no redirect is ever followed (any 3xx raises before Location is read, so a
  session header cannot be replayed to another host);
- proxy environment variables are ignored (http.client never reads them);
- explicit connect, read and total deadlines and a byte cap per response.

There is no streaming API. Each upstream body is held in memory for one
request, capped by max_bytes, and never written anywhere (OD-17).
"""

from __future__ import annotations

import http.client
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping, Protocol
from urllib.parse import urlsplit

_CHUNK = 64 * 1024


@dataclass(frozen=True)
class AllowedHost:
    scheme: str
    port: int


@dataclass(frozen=True)
class Timeouts:
    connect_s: float = 5.0
    read_s: float = 30.0
    total_s: float = 35.0


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]  # header names lower-cased
    body: bytes


class TransportError(Exception):
    """kind: host_not_allowed | scheme_not_allowed | connect | timeout | tls |
    redirect | too_large | protocol.

    The message carries only the kind and the host name, never headers, body
    or query string.
    """

    def __init__(self, kind: str, host: str = "") -> None:
        self.kind = kind
        self.host = host
        super().__init__(f"{kind}:{host}" if host else kind)


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeouts: Timeouts,
        max_bytes: int,
    ) -> HttpResponse: ...


DEFAULT_ALLOWED_HOSTS: Mapping[str, AllowedHost] = MappingProxyType(
    {
        "api.icicidirect.com": AllowedHost("https", 443),
        "breezeapi.icicidirect.com": AllowedHost("https", 443),
        "directlink.icicidirect.com": AllowedHost("https", 443),
        "secretmanager.googleapis.com": AllowedHost("https", 443),
        "api.ipify.org": AllowedHost("https", 443),
        "metadata.google.internal": AllowedHost("http", 80),
    }
)


def _tls_context() -> ssl.SSLContext:
    """The TLS context every https request uses. Verification is never off."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def now_utc(self) -> datetime: ...  # tz-aware UTC


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class HttpClientTransport:
    """Transport implemented on http.client. One connection per request."""

    def __init__(self, allowed: Mapping[str, AllowedHost] = DEFAULT_ALLOWED_HOSTS) -> None:
        self._allowed = dict(allowed)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeouts: Timeouts,
        max_bytes: int,
    ) -> HttpResponse:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if "@" in parts.netloc or not host:
            raise TransportError("host_not_allowed", host)
        rule = self._allowed.get(host)
        if rule is None:
            raise TransportError("host_not_allowed", host)
        try:
            port = parts.port
        except ValueError:
            raise TransportError("protocol", host) from None
        if port is None:
            port = 443 if parts.scheme == "https" else 80
        if parts.scheme != rule.scheme or port != rule.port:
            raise TransportError("scheme_not_allowed", host)

        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        deadline = time.monotonic() + timeouts.total_s
        if rule.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, port, timeout=timeouts.connect_s, context=_tls_context()
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeouts.connect_s)

        try:
            self._connect(conn, host, timeouts, deadline)
            self._set_timeout(conn, host, timeouts, deadline)
            try:
                conn.request(method, path, body=body, headers=dict(headers))
                self._set_timeout(conn, host, timeouts, deadline)
                resp = conn.getresponse()
            except TransportError:
                raise
            except TimeoutError:
                raise TransportError("timeout", host) from None
            except ssl.SSLError:
                raise TransportError("tls", host) from None
            except (http.client.HTTPException, OSError, ValueError):
                raise TransportError("protocol", host) from None

            if 300 <= resp.status < 400:
                raise TransportError("redirect", host)

            declared = resp.getheader("content-length")
            if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                raise TransportError("too_large", host)

            chunks: list[bytes] = []
            total = 0
            try:
                while True:
                    self._set_timeout(conn, host, timeouts, deadline)
                    chunk = resp.read1(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise TransportError("too_large", host)
                    chunks.append(chunk)
            except TransportError:
                raise
            except TimeoutError:
                raise TransportError("timeout", host) from None
            except ssl.SSLError:
                raise TransportError("tls", host) from None
            except (http.client.HTTPException, OSError, ValueError):
                raise TransportError("protocol", host) from None

            return HttpResponse(
                status=resp.status,
                headers={k.lower(): v for k, v in resp.getheaders()},
                body=b"".join(chunks),
            )
        finally:
            conn.close()

    @staticmethod
    def _connect(
        conn: http.client.HTTPConnection, host: str, timeouts: Timeouts, deadline: float
    ) -> None:
        try:
            conn.connect()
        except TimeoutError:
            raise TransportError("timeout", host) from None
        except ssl.SSLError:
            raise TransportError("tls", host) from None
        except OSError:
            raise TransportError("connect", host) from None

    @staticmethod
    def _set_timeout(
        conn: http.client.HTTPConnection, host: str, timeouts: Timeouts, deadline: float
    ) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransportError("timeout", host)
        if conn.sock is not None:
            conn.sock.settimeout(min(timeouts.read_s, remaining))

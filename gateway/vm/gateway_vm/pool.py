"""Persistent upstream connections for the relay (OD-3, OD-7).

PooledTransport is a drop-in Transport with the same rules as
HttpClientTransport (host allowlist with fixed scheme and port, no redirect
followed, no proxy environment, byte caps, explicit deadlines, error kinds that
carry no header or body). It differs in one way: per upstream host (scheme,
host, port) it keeps up to `max_connections` http.client connections open and
reuses them, so a tunnel burst does not pay a TLS handshake per call and at
most that many calls are in flight at once.

A caller takes a slot with a bounded wait (`pool_wait_s`); no slot in time is
TransportError("timeout"), which the relay answers as 504. A connection goes
back to the pool only after a complete response that did not ask to close; a
too-large, timed-out, cut-short, redirected or Connection: close response
closes it. A reused connection that fails before any response byte arrived is
replaced once, for GET only. Nothing else is ever resent.

Nothing is written to disk and no body is kept past the call (OD-17).
"""

from __future__ import annotations

import http.client
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit

from . import transport as _transport
from .transport import (
    DEFAULT_ALLOWED_HOSTS,
    AllowedHost,
    Clock,
    HttpClientTransport,
    HttpResponse,
    SystemClock,
    Timeouts,
    TransportError,
)

_CHUNK = 64 * 1024

# A reused connection the peer already closed fails in one of these ways before
# any response byte arrives. Timeouts are deliberately not on the list: a
# request that timed out may have been processed, so it is never replayed.
_STALE_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.CannotSendRequest,
    http.client.CannotSendHeader,
    http.client.NotConnected,
    ConnectionError,  # reset, aborted, broken pipe
)


class _StaleConnection(Exception):
    """A reused connection died before any response byte arrived."""


@dataclass
class _HostPool:
    slots: threading.BoundedSemaphore
    lock: threading.Lock = field(default_factory=threading.Lock)
    idle: list[tuple[http.client.HTTPConnection, float]] = field(default_factory=list)


class PooledTransport:
    """Implements Transport with bounded, persistent connections."""

    def __init__(
        self,
        allowed: Mapping[str, AllowedHost] = DEFAULT_ALLOWED_HOSTS,
        *,
        max_connections: int = 4,
        pool_wait_s: float = 10.0,
        idle_s: float = 4.0,
        clock: Clock = SystemClock(),
    ) -> None:
        if max_connections < 1:
            raise ValueError("max_connections must be at least 1")
        self._allowed = dict(allowed)
        self._max = max_connections
        self._pool_wait_s = pool_wait_s
        self._idle_s = idle_s
        self._clock = clock
        self._guard = threading.Lock()
        self._pools: dict[tuple[str, str, int], _HostPool] = {}
        self._ctx: ssl.SSLContext | None = None
        self._closed = False

    # ------------------------------------------------------------ plumbing

    def _target(self, url: str) -> tuple[str, AllowedHost, int, str]:
        """The same checks HttpClientTransport.request makes before it opens a socket."""
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
        return host, rule, port, path

    def _pool_for(self, key: tuple[str, str, int]) -> _HostPool:
        with self._guard:
            if self._closed:
                raise TransportError("connect", key[1])
            pool = self._pools.get(key)
            if pool is None:
                pool = _HostPool(slots=threading.BoundedSemaphore(self._max))
                self._pools[key] = pool
            return pool

    def _tls(self) -> ssl.SSLContext:
        with self._guard:
            if self._ctx is None:
                self._ctx = _transport._tls_context()
            return self._ctx

    def _checkout(
        self, pool: _HostPool, host: str, rule: AllowedHost, port: int, timeouts: Timeouts
    ) -> tuple[http.client.HTTPConnection, bool]:
        """An idle connection younger than idle_s, else a new one. (conn, reused)."""
        now = self._clock.monotonic()
        while True:
            with pool.lock:
                if not pool.idle:
                    break
                conn, last_used = pool.idle.pop()
            if now - last_used < self._idle_s:
                return conn, True
            conn.close()  # idle too long: the peer has probably closed it
        if rule.scheme == "https":
            fresh: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, port, timeout=timeouts.connect_s, context=self._tls()
            )
        else:
            fresh = http.client.HTTPConnection(host, port, timeout=timeouts.connect_s)
        return fresh, False

    def _checkin(self, pool: _HostPool, conn: http.client.HTTPConnection) -> None:
        with pool.lock:
            if not self._closed:
                pool.idle.append((conn, self._clock.monotonic()))
                return
        conn.close()

    # ------------------------------------------------------------- request

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
        host, rule, port, path = self._target(url)
        pool = self._pool_for((rule.scheme, host, port))
        if not pool.slots.acquire(timeout=self._pool_wait_s):
            raise TransportError("timeout", host)  # no upstream connection freed up in time
        try:
            deadline = time.monotonic() + timeouts.total_s
            for attempt in (0, 1):
                conn, reused = self._checkout(pool, host, rule, port, timeouts)
                keep = False
                try:
                    if not reused:
                        HttpClientTransport._connect(conn, host, timeouts, deadline)
                    response, keep = self._exchange(
                        conn, host, method, path, body, headers, timeouts, deadline, max_bytes, reused
                    )
                    return response
                except _StaleConnection:
                    if method == "GET" and attempt == 0:
                        continue  # one transparent replacement, reads only
                    raise TransportError("connect", host) from None
                finally:
                    if keep:
                        self._checkin(pool, conn)
                    else:
                        conn.close()
            raise TransportError("connect", host)  # unreachable: attempt 1 never raises _StaleConnection
        finally:
            pool.slots.release()

    def _exchange(
        self,
        conn: http.client.HTTPConnection,
        host: str,
        method: str,
        path: str,
        body: bytes | None,
        headers: Mapping[str, str],
        timeouts: Timeouts,
        deadline: float,
        max_bytes: int,
        reused: bool,
    ) -> tuple[HttpResponse, bool]:
        """Send one request. Returns the response and whether the connection may be reused."""
        set_timeout = HttpClientTransport._set_timeout
        try:
            set_timeout(conn, host, timeouts, deadline)
            conn.request(method, path, body=body, headers=dict(headers))
            set_timeout(conn, host, timeouts, deadline)
            resp = conn.getresponse()
        except TransportError:
            raise
        except TimeoutError:
            raise TransportError("timeout", host) from None
        except ssl.SSLError:
            raise TransportError("tls", host) from None
        except _STALE_ERRORS:
            if reused:
                raise _StaleConnection from None
            raise TransportError("protocol", host) from None
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
                set_timeout(conn, host, timeouts, deadline)
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

        if resp.length:  # fixed-length body that ended early: never hand out a partial body
            raise TransportError("protocol", host)
        complete = resp.isclosed() and not resp.will_close
        return (
            HttpResponse(
                status=resp.status,
                headers={k.lower(): v for k, v in resp.getheaders()},
                body=b"".join(chunks),
            ),
            complete,
        )

    # --------------------------------------------------------------- close

    def close(self) -> None:
        """Close every idle connection. Connections still in flight close when they finish."""
        with self._guard:
            self._closed = True
            pools = list(self._pools.values())
        for pool in pools:
            with pool.lock:
                idle, pool.idle = pool.idle, []
            for conn, _last_used in idle:
                conn.close()

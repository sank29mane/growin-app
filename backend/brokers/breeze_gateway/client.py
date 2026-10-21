"""The one Mac-side client for relay contract growin-relay/1 (C1 to C9).

Every response is checked for the contract header before anything else, upstream
bytes are checked against their sha256 header in constant time, and every
failure maps to one RelayError subclass. The client never retries a request and
logs nothing. The vendor Breeze SDK is never imported on the Mac.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

import httpx

from .config import GatewayConfig, load_gateway_config
from .errors import (
    CODE_TO_CLASS,
    BudgetExhausted,
    BudgetUnavailable,
    EgressMismatch,
    IdentityMismatch,
    InternalRelayError,
    LoginAttemptInvalid,
    NoSession,
    RelayArgumentError,
    RelayContractMismatch,
    RelayError,
    RelayHashMismatch,
    RelayProtocolError,
    RelayUnavailable,
    SessionExpired,
    TunnelError,
    Unauthorized,
    UpstreamError,
    UpstreamTimeout,
)
from .tunnel import Tunnel
from .token_store import RELAY_TOKEN_RE, FileTokenStore, MemoryTokenStore, TokenStore, same_token

CONTRACT = "growin-relay/1"

# ---------------------------------------------------------------- headers

HDR_CONTRACT = "X-Growin-Relay-Contract"
HDR_REQUEST_ID = "X-Growin-Relay-Request-Id"
HDR_CACHE_CONTROL = "Cache-Control"
HDR_AUTHORIZATION = "Authorization"

HDR_UP_SHA256 = "X-Growin-Upstream-Sha256"
HDR_UP_FETCHED_AT = "X-Growin-Upstream-Fetched-At"
HDR_UP_API = "X-Growin-Upstream-Api"
HDR_UP_REQUEST = "X-Growin-Upstream-Request"
HDR_UP_INTERVAL = "X-Growin-Upstream-Interval"
HDR_BREEZE_STATUS = "X-Growin-Breeze-Status"
HDR_EMPTY = "X-Growin-Empty"
HDR_UP_LAST_MODIFIED = "X-Growin-Upstream-Last-Modified"

# ----------------------------------------------------------------- routes

ROUTE_HEALTH = "/v1/health"
ROUTE_BUDGET = "/v1/budget"
ROUTE_LOGIN_ARM = "/v1/login/arm"
ROUTE_LOGIN_COMPLETE = "/v1/login/complete"
ROUTE_SESSION_REVOKE = "/v1/session/revoke"
ROUTE_BARS_DAILY = "/v1/bars/daily"
ROUTE_QUOTES = "/v1/quotes"
ROUTE_PREVIEW_ORDER = "/v1/preview-order"
ROUTE_SECURITY_MASTER = "/v1/security-master"
ROUTES = frozenset(
    {
        ROUTE_HEALTH,
        ROUTE_BUDGET,
        ROUTE_LOGIN_ARM,
        ROUTE_LOGIN_COMPLETE,
        ROUTE_SESSION_REVOKE,
        ROUTE_BARS_DAILY,
        ROUTE_QUOTES,
        ROUTE_PREVIEW_ORDER,
        ROUTE_SECURITY_MASTER,
    }
)

API_BARS_V2 = "breeze-historicalcharts-v2"
API_QUOTES_V1 = "breeze-quotes-v1"
API_PREVIEW_V1 = "breeze-preview-order-v1"
API_SECURITY_MASTER = "icici-security-master-zip"
DAILY_INTERVAL = "1day"

# ----------------------------------------------------------------- limits

MAX_BODY_BYTES = 64 * 1024 * 1024
MAX_IN_FLIGHT = 4
MAX_RANGE_DAYS = 3000
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 150.0
WRITE_TIMEOUT_S = 10.0
POOL_TIMEOUT_S = 5.0

# --------------------------------------------------------------- patterns

REQUEST_ID_RE = re.compile(r"^[0-9a-f]{16}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FETCHED_AT_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$")
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
STOCK_CODE_RE = re.compile(r"^[A-Z0-9]{1,10}$")
API_SESSION_RE = re.compile(r"^[A-Za-z0-9]{6,64}$")
REASON_RE = re.compile(r"^[a-z_]{1,40}$")
HEALTH_STATES = frozenset({"none", "active", "expired", "suspended_egress"})

# After one of these the server has disarmed (or never had) the attempt: it cannot be completed.
_ATTEMPT_SPENT = (
    LoginAttemptInvalid,
    IdentityMismatch,
    EgressMismatch,
    BudgetExhausted,
    BudgetUnavailable,
    UpstreamError,
    UpstreamTimeout,
    InternalRelayError,
    RelayProtocolError,  # the answer was unusable, so the outcome is unknown
)

_AUTH_NONE = "none"
_AUTH_SESSION = "session"  # the session token, when this client holds one
_AUTH_ATTEMPT = "attempt"  # the token the last arm returned


class TunnelHandleLike(Protocol):
    local_port: int

    def alive(self) -> bool: ...

    def stop(self) -> None: ...


# -------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class UpstreamPayload:
    body: bytes
    sha256: str
    fetched_at_utc: datetime
    upstream_api: str
    upstream_request: Mapping[str, str]
    upstream_interval: str | None
    breeze_status: int | None
    empty: bool
    upstream_last_modified: str | None
    request_id: str
    route: str
    query: Mapping[str, str]
    contract: str

    def provenance(self) -> dict:
        return {
            "source": "breeze_relay",
            "contract": self.contract,
            "route": self.route,
            "query": dict(self.query),
            "upstream_api": self.upstream_api,
            "upstream_request": dict(self.upstream_request),
            "fetched_at_utc": self.fetched_at_utc.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "sha256": self.sha256,
            "request_id": self.request_id,
        }


@dataclass(frozen=True)
class RelayHealth:
    state: str
    login_pending: bool
    expires_at_ist: datetime | None
    egress_ok: bool | None
    egress_checked_at_utc: datetime | None
    server_time_utc: datetime
    version: str


@dataclass(frozen=True)
class RelayBudget:
    calls_last_minute: int
    calls_today_ist: int
    per_minute_limit: int
    per_day_limit: int
    remaining_today: int
    ist_date: date


@dataclass(frozen=True)
class LoginAttempt:
    attempt_id: str
    login_url: str = field(repr=False)
    relay_token: str = field(repr=False)
    expires_at_utc: datetime


@dataclass(frozen=True)
class SessionStatus:
    state: str
    expires_at_ist: datetime | None


@dataclass(frozen=True)
class _Reply:
    request_id: str
    headers: httpx.Headers
    body: bytes


@dataclass(frozen=True)
class _Attempt:
    """The armed login attempt this client may complete: its id and the token bound to it."""

    attempt_id: str
    token: str = field(repr=False)


class _Pool:
    """One httpx client plus the requests that hold it.

    A retired pool is closed by whoever lets go of it last, so a restart never closes a
    client under a request that already picked it.
    """

    __slots__ = ("http", "inflight", "retired")

    def __init__(self, http: httpx.Client) -> None:
        self.http = http
        self.inflight = 0
        self.retired = False


@dataclass(frozen=True)
class _ByteRoute:
    api: str
    media_type: str
    breeze: bool
    interval: str | None = None


_BYTE_ROUTES: Mapping[str, _ByteRoute] = {
    ROUTE_BARS_DAILY: _ByteRoute(API_BARS_V2, "application/json", True, DAILY_INTERVAL),
    ROUTE_QUOTES: _ByteRoute(API_QUOTES_V1, "application/json", True),
    ROUTE_PREVIEW_ORDER: _ByteRoute(API_PREVIEW_V1, "application/json", True),
    ROUTE_SECURITY_MASTER: _ByteRoute(API_SECURITY_MASTER, "application/zip", False),
}


# ---------------------------------------------------------------- parsing


def _protocol(reason: str, request_id: str | None = None) -> RelayProtocolError:
    return RelayProtocolError(reason=reason, request_id=request_id)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _aware(value: object, rid: str, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise _protocol("bad_field", rid)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _protocol("bad_field", rid) from None
    if parsed.tzinfo is None:
        raise _protocol("bad_field", rid)
    return parsed


def _field(data: Mapping[str, Any], name: str, kind: type, rid: str) -> Any:
    value = data.get(name)
    if kind is int:
        if not _is_int(value):
            raise _protocol("bad_field", rid)
    elif not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
        raise _protocol("bad_field", rid)
    return value


def _media_type(headers: httpx.Headers) -> str:
    return headers.get("content-type", "").split(";")[0].strip().lower()


def _upstream_request(raw: str, rid: str) -> Mapping[str, str]:
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise _protocol("bad_header", rid) from None
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise _protocol("bad_header", rid)
    return MappingProxyType(dict(parsed))


# ------------------------------------------------------------ arguments


def _stock_code(value: str) -> str:
    if not isinstance(value, str) or not STOCK_CODE_RE.fullmatch(value):
        raise RelayArgumentError("stock_code")
    return value


def _exchange(value: str) -> str:
    if value != "NSE":
        raise RelayArgumentError("exchange")
    return value


def _day(value: date, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise RelayArgumentError(name)
    return value


def _price(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise RelayArgumentError("price")
    try:
        cents = value.quantize(Decimal("0.01"))
    except InvalidOperation:
        raise RelayArgumentError("price") from None
    if cents != value or cents <= 0 or cents > Decimal(1_000_000):
        raise RelayArgumentError("price")
    return format(cents, "f")


class RelayClient:
    """Client for growin-relay/1. Build it with `with RelayClient.open() as relay:`."""

    CONTRACT = CONTRACT

    def __init__(
        self,
        *,
        tunnel: TunnelHandleLike | None = None,
        http: httpx.Client | None = None,
        token_store: TokenStore | None = None,
        http_factory: Callable[[int], httpx.Client] | None = None,
        restart: Callable[[], TunnelHandleLike] | None = None,
    ) -> None:
        """`http_factory` and `restart` are supervisor seams used by open().

        With `restart` the client owns its tunnel: it starts one replacement when the
        child has exited, and close() stops it. Without it a dead tunnel is an error.
        """
        if (tunnel is None) == (http is None):
            raise TypeError("RelayClient needs exactly one of tunnel or http")
        self._tunnel: TunnelHandleLike | None = tunnel
        self._http_factory = http_factory if http_factory is not None else self._build_http
        self._restart = restart
        self._pool = _Pool(
            http if http is not None else self._http_factory(tunnel.local_port)  # type: ignore[union-attr]
        )
        self._store: TokenStore = token_store if token_store is not None else MemoryTokenStore()
        self._slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)
        self._lock = threading.RLock()  # guards the fields below; never held across a request
        self._login_lock = threading.RLock()  # one login transition (arm or complete) at a time
        self._closed = False
        self._session_token: str | None = None
        self._attempt: _Attempt | None = None
        self._token_saved: bool | None = None
        try:
            stored = self._store.load()
            if stored is not None:
                self._session_token = stored.token
            else:
                self._store.clear()  # an expired or invalid saved token is dropped, not kept around
        except OSError:
            pass

    # ----------------------------------------------------------- transport

    @staticmethod
    def _build_http(port: int) -> httpx.Client:
        return httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            limits=httpx.Limits(
                max_connections=MAX_IN_FLIGHT, max_keepalive_connections=MAX_IN_FLIGHT
            ),
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_S,
                read=READ_TIMEOUT_S,
                write=WRITE_TIMEOUT_S,
                pool=POOL_TIMEOUT_S,
            ),
            follow_redirects=False,
            trust_env=False,
        )

    @property
    def _http(self) -> httpx.Client:
        return self._pool.http

    @property
    def token_saved(self) -> bool | None:
        """Whether the last completed login reached the token store; None before any."""
        return self._token_saved

    def _acquire(self) -> _Pool:
        """The current pool with this request counted on it. Pair with _release()."""
        with self._lock:
            if self._closed:
                raise RelayUnavailable(reason="closed")
            if self._tunnel is not None and not self._tunnel.alive():
                if self._restart is None:
                    raise RelayUnavailable(reason="tunnel_down")
                self._restart_tunnel()
            pool = self._pool
            pool.inflight += 1
            return pool

    def _release(self, pool: _Pool) -> None:
        with self._lock:
            pool.inflight -= 1
            last = pool.retired and pool.inflight == 0
        if last:
            pool.http.close()  # idempotent: close() may have closed it already

    def _restart_tunnel(self) -> None:
        """One replacement tunnel on a fresh port. A transport restart, never a request retry."""
        assert self._restart is not None and self._tunnel is not None
        self._tunnel.stop()
        try:
            handle = self._restart()
        except TunnelError:
            raise RelayUnavailable(reason="tunnel_restart_failed") from None
        old = self._pool
        self._tunnel = handle
        self._pool = _Pool(self._http_factory(handle.local_port))
        old.retired = True
        if old.inflight == 0:
            old.http.close()  # otherwise the last request holding it closes it

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pool, tunnel = self._pool, self._tunnel
            pool.retired = True
            owned = self._restart is not None
        try:
            pool.http.close()
        finally:
            if owned and tunnel is not None:
                tunnel.stop()

    @classmethod
    def open(
        cls,
        config: GatewayConfig | None = None,
        *,
        token_store: TokenStore | None = None,
        tunnel: Tunnel | None = None,
        http_factory: Callable[[int], httpx.Client] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> "RelayClient":
        """Start this client's own IAP tunnel, wait for /v1/health and return the client.

        `tunnel`, `http_factory`, `sleep` and `monotonic` are test seams. A TunnelError
        from the tunnel is raised unchanged.
        """
        cfg = config if config is not None else load_gateway_config()
        store = token_store if token_store is not None else FileTokenStore()
        supervisor = tunnel if tunnel is not None else Tunnel(cfg)
        handle = supervisor.start()
        try:
            relay = cls(
                tunnel=handle,
                token_store=store,
                http_factory=http_factory,
                restart=supervisor.start,
            )
        except BaseException:
            handle.stop()
            raise
        try:
            deadline = monotonic() + cfg.tunnel.ready_timeout_s
            while True:
                try:
                    relay.health()
                    break
                except RelayUnavailable:
                    if monotonic() >= deadline:
                        raise RelayUnavailable(reason="relay_not_ready") from None
                    sleep(0.5)
            # The port must still belong to our own child once the relay has answered.
            if relay._tunnel is None or not relay._tunnel.alive():
                raise RelayUnavailable(reason="tunnel_down")
        except BaseException:
            relay.close()
            raise
        return relay

    def __enter__(self) -> "RelayClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- tokens

    def _bearer(self, auth: str) -> str | None:
        with self._lock:
            if auth == _AUTH_SESSION:
                return self._session_token
            if auth == _AUTH_ATTEMPT:
                return self._attempt.token if self._attempt is not None else None
            return None

    def _forget(self, sent: str | None) -> None:
        """Drop the token this client sent, but only where it is still the current one."""
        if sent is None:
            return
        with self._lock:
            if self._session_token is not None and same_token(self._session_token, sent):
                self._session_token = None
            if self._attempt is not None and same_token(self._attempt.token, sent):
                self._attempt = None
        try:
            stored = self._store.load()
            if stored is not None and same_token(stored.token, sent):
                self._store.clear()
        except OSError:
            pass  # the relay already refuses this token; a stuck file never masks the relay error

    # ------------------------------------------------------------ requests

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        body: Mapping[str, str] | None = None,
        auth: str,
    ) -> _Reply:
        pool = self._acquire()
        try:
            return self._send_on(pool, method, path, params=params, body=body, auth=auth)
        finally:
            self._release(pool)

    def _send_on(
        self,
        pool: _Pool,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | None,
        body: Mapping[str, str] | None,
        auth: str,
    ) -> _Reply:
        headers = {HDR_CONTRACT: CONTRACT}
        sent = self._bearer(auth)
        if sent is not None:
            headers[HDR_AUTHORIZATION] = f"Bearer {sent}"
        content: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body, separators=(",", ":")).encode("utf-8")
        try:
            with self._slots:
                if pool.retired:  # the tunnel was replaced (or the client closed) while we waited
                    raise RelayUnavailable(reason="closed" if self._closed else "tunnel_restarted")
                with pool.http.stream(
                    method, path, params=params, content=content, headers=headers
                ) as response:
                    return self._read(response)
        except (Unauthorized, NoSession, SessionExpired):
            self._forget(sent)
            raise
        except httpx.TimeoutException:
            raise RelayUnavailable(reason="timeout") from None
        except httpx.TransportError:
            raise RelayUnavailable(reason="connect_error") from None
        except RuntimeError:
            if pool.retired:  # httpx refuses a request on a client that was closed under it
                raise RelayUnavailable(
                    reason="closed" if self._closed else "tunnel_restarted"
                ) from None
            raise

    @staticmethod
    def _read(response: httpx.Response) -> _Reply:
        # C2: the contract header comes before the status, the id and the body.
        if response.headers.get(HDR_CONTRACT) != CONTRACT:
            raise RelayContractMismatch(reason="contract_header", http_status=response.status_code)
        rid = response.headers.get(HDR_REQUEST_ID, "")
        if not REQUEST_ID_RE.fullmatch(rid):
            raise RelayProtocolError(reason="request_id", http_status=response.status_code)
        status = response.status_code
        if 300 <= status < 400:
            raise RelayProtocolError(reason="redirect", http_status=status, request_id=rid)
        length = response.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            raise RelayProtocolError(reason="body_too_large", http_status=status, request_id=rid)
        buffer = bytearray()
        for chunk in response.iter_bytes():
            buffer += chunk
            if len(buffer) > MAX_BODY_BYTES:
                raise RelayProtocolError(
                    reason="body_too_large", http_status=status, request_id=rid
                )
        body = bytes(buffer)
        if status != 200:
            raise RelayClient._error_from(status, body, rid)
        return _Reply(request_id=rid, headers=response.headers, body=body)

    @staticmethod
    def _error_from(status: int, body: bytes, rid: str) -> RelayError:
        try:
            data = json.loads(body)
        except ValueError:
            return RelayProtocolError(reason="bad_error_body", http_status=status, request_id=rid)
        if not isinstance(data, dict):
            return RelayProtocolError(reason="bad_error_body", http_status=status, request_id=rid)
        if data.get("contract") != CONTRACT:
            return RelayContractMismatch(reason="body_contract", http_status=status, request_id=rid)
        err = data.get("error")
        if not isinstance(err, dict):
            return RelayProtocolError(reason="bad_error_body", http_status=status, request_id=rid)
        code = err.get("code")
        cls = CODE_TO_CLASS.get(code) if isinstance(code, str) else None
        if cls is None:
            return RelayProtocolError(reason="unknown_code", http_status=status, request_id=rid)
        reason = err.get("reason")
        breeze_status = err.get("breeze_status")
        retryable = err.get("retryable")
        retry_after = err.get("retry_after_s")
        if (
            (reason is not None and not (isinstance(reason, str) and REASON_RE.fullmatch(reason)))
            or (breeze_status is not None and not _is_int(breeze_status))
            or not isinstance(retryable, bool)
            or (retry_after is not None and not _is_int(retry_after))
        ):
            return RelayProtocolError(reason="bad_error_body", http_status=status, request_id=rid)
        return cls(
            code,
            http_status=status,
            reason=reason,
            breeze_status=breeze_status,
            retryable=retryable,
            retry_after_s=retry_after,
            request_id=rid,
        )

    def _json(
        self,
        method: str,
        path: str,
        *,
        auth: str,
        body: Mapping[str, str] | None = None,
    ) -> tuple[dict, str]:
        reply = self._send(method, path, body=body, auth=auth)
        rid = reply.request_id
        if _media_type(reply.headers) != "application/json":
            raise _protocol("content_type", rid)
        try:
            data = json.loads(reply.body)
        except ValueError:
            raise _protocol("bad_json", rid) from None
        if not isinstance(data, dict):
            raise _protocol("bad_json", rid)
        if data.get("contract") != CONTRACT:
            raise RelayContractMismatch(reason="body_contract", request_id=rid)
        return data, rid

    def _bytes(self, path: str, params: list[tuple[str, str]]) -> UpstreamPayload:
        spec = _BYTE_ROUTES[path]
        reply = self._send("GET", path, params=params, auth=_AUTH_SESSION)
        rid, headers, body = reply.request_id, reply.headers, reply.body
        if _media_type(headers) != spec.media_type:
            raise _protocol("content_type", rid)

        digest = headers.get(HDR_UP_SHA256, "")
        fetched_raw = headers.get(HDR_UP_FETCHED_AT, "")
        request_raw = headers.get(HDR_UP_REQUEST)
        if (
            not SHA256_RE.fullmatch(digest)
            or not FETCHED_AT_RE.fullmatch(fetched_raw)
            or request_raw is None
        ):
            raise _protocol("missing_header", rid)
        if headers.get(HDR_UP_API) != spec.api:
            raise _protocol("bad_header", rid)
        interval = headers.get(HDR_UP_INTERVAL)
        if spec.interval is not None and interval != spec.interval:
            raise _protocol("bad_header", rid)
        breeze_status: int | None = None
        empty = False
        if spec.breeze:
            if headers.get(HDR_BREEZE_STATUS) != "200":
                raise _protocol("bad_header", rid)
            breeze_status = 200
            flag = headers.get(HDR_EMPTY)
            if flag not in ("true", "false"):
                raise _protocol("bad_header", rid)
            empty = flag == "true"
        try:
            fetched = datetime.fromisoformat(fetched_raw[:-1] + "+00:00")
        except ValueError:
            raise _protocol("bad_header", rid) from None
        upstream_request = _upstream_request(request_raw, rid)

        actual = hashlib.sha256(body).hexdigest()
        if not hmac.compare_digest(actual.encode("ascii"), digest.encode("ascii")):
            raise RelayHashMismatch(reason="sha256", request_id=rid)

        return UpstreamPayload(
            body=body,
            sha256=digest,
            fetched_at_utc=fetched,
            upstream_api=spec.api,
            upstream_request=upstream_request,
            upstream_interval=interval if spec.interval is not None else None,
            breeze_status=breeze_status,
            empty=empty,
            upstream_last_modified=headers.get(HDR_UP_LAST_MODIFIED),
            request_id=rid,
            route=path,
            query=MappingProxyType(dict(params)),
            contract=CONTRACT,
        )

    # --------------------------------------------------------------- routes

    def health(self) -> RelayHealth:
        data, rid = self._json("GET", ROUTE_HEALTH, auth=_AUTH_NONE)
        state = _field(data, "state", str, rid)
        egress = _field(data, "egress", dict, rid)
        ok = egress.get("ok")
        if state not in HEALTH_STATES or (ok is not None and not isinstance(ok, bool)):
            raise _protocol("bad_field", rid)
        return RelayHealth(
            state=state,
            login_pending=_field(data, "login_pending", bool, rid),
            expires_at_ist=_aware(data.get("expires_at_ist"), rid, nullable=True),
            egress_ok=ok,
            egress_checked_at_utc=_aware(egress.get("checked_at_utc"), rid, nullable=True),
            server_time_utc=_aware(data.get("server_time_utc"), rid),  # type: ignore[arg-type]
            version=_field(data, "version", str, rid),
        )

    def budget(self) -> RelayBudget:
        data, rid = self._json("GET", ROUTE_BUDGET, auth=_AUTH_SESSION)
        try:
            ist_date = date.fromisoformat(_field(data, "ist_date", str, rid))
        except ValueError:
            raise _protocol("bad_field", rid) from None
        return RelayBudget(
            calls_last_minute=_field(data, "calls_last_minute", int, rid),
            calls_today_ist=_field(data, "calls_today_ist", int, rid),
            per_minute_limit=_field(data, "per_minute_limit", int, rid),
            per_day_limit=_field(data, "per_day_limit", int, rid),
            remaining_today=_field(data, "remaining_today", int, rid),
            ist_date=ist_date,
        )

    def login_arm(self) -> LoginAttempt:
        with self._login_lock:  # never two arms in flight: the later answer must be the newer one
            return self._login_arm()

    def _login_arm(self) -> LoginAttempt:
        data, rid = self._json("POST", ROUTE_LOGIN_ARM, auth=_AUTH_SESSION, body={})
        attempt_id = _field(data, "attempt_id", str, rid)
        login_url = _field(data, "login_url", str, rid)
        token = _field(data, "relay_token", str, rid)
        # The token goes into a header and the URL goes to a browser: both are checked here.
        if (
            not ATTEMPT_ID_RE.fullmatch(attempt_id)
            or not RELAY_TOKEN_RE.fullmatch(token)
            or not login_url.startswith("https://")
        ):
            raise _protocol("bad_field", rid)
        expires = _aware(data.get("expires_at_utc"), rid)
        with self._lock:
            self._attempt = _Attempt(attempt_id, token)  # replaces any older attempt
        return LoginAttempt(
            attempt_id=attempt_id,
            login_url=login_url,
            relay_token=token,
            expires_at_utc=expires,  # type: ignore[arg-type]
        )

    def login_complete(self, attempt_id: str, apisession: str) -> SessionStatus:
        if not isinstance(attempt_id, str) or not ATTEMPT_ID_RE.fullmatch(attempt_id):
            raise RelayArgumentError("attempt_id")
        if not isinstance(apisession, str) or not API_SESSION_RE.fullmatch(apisession):
            raise RelayArgumentError("apisession")
        with self._login_lock:
            return self._login_complete(attempt_id, apisession)

    def _login_complete(self, attempt_id: str, apisession: str) -> SessionStatus:
        with self._lock:
            attempt = self._attempt
        # A 409 from the relay ends an attempt for good (expired, superseded, revoked or
        # dropped), and so does a completed login. Only the current attempt is ever sent.
        if attempt is None or not same_token(attempt.attempt_id, attempt_id):
            raise LoginAttemptInvalid(reason="attempt_not_current")
        sent = attempt.token
        try:
            data, rid = self._json(
                "POST",
                ROUTE_LOGIN_COMPLETE,
                auth=_AUTH_ATTEMPT,
                body={"attempt_id": attempt_id, "apisession": apisession},
            )
            status = self._session(data, rid)
            if status.state != "active" or status.expires_at_ist is None:
                raise _protocol("bad_field", rid)
        except _ATTEMPT_SPENT:
            self._end_attempt(attempt)
            raise
        with self._lock:
            self._session_token = sent
        self._end_attempt(attempt)
        try:
            self._store.save(sent, status.expires_at_ist)
            self._token_saved = True
        except OSError:
            self._token_saved = False  # in memory the token still works; the file is a convenience
        return status

    def _end_attempt(self, attempt: _Attempt) -> None:
        with self._lock:
            if self._attempt is attempt:
                self._attempt = None

    def revoke(self) -> SessionStatus:
        data, rid = self._json("POST", ROUTE_SESSION_REVOKE, auth=_AUTH_SESSION, body={})
        status = self._session(data, rid)
        with self._lock:
            self._session_token = None
            self._attempt = None
        try:
            self._store.clear()
        except OSError:
            pass
        return status

    @staticmethod
    def _session(data: Mapping[str, Any], rid: str) -> SessionStatus:
        state = _field(data, "state", str, rid)
        if state not in HEALTH_STATES:
            raise _protocol("bad_field", rid)
        return SessionStatus(
            state=state, expires_at_ist=_aware(data.get("expires_at_ist"), rid, nullable=True)
        )

    def daily_bars(
        self, stock_code: str, from_date: date, to_date: date, *, exchange: str = "NSE"
    ) -> UpstreamPayload:
        code = _stock_code(stock_code)
        start = _day(from_date, "from")
        end = _day(to_date, "to")
        if end < start or (end - start).days > MAX_RANGE_DAYS:
            raise RelayArgumentError("from")
        return self._bytes(
            ROUTE_BARS_DAILY,
            [
                ("stock_code", code),
                ("from", start.isoformat()),
                ("to", end.isoformat()),
                ("exchange", _exchange(exchange)),
            ],
        )

    def quote(self, stock_code: str, *, exchange: str = "NSE") -> UpstreamPayload:
        return self._bytes(
            ROUTE_QUOTES, [("stock_code", _stock_code(stock_code)), ("exchange", _exchange(exchange))]
        )

    def preview_order(
        self,
        stock_code: str,
        *,
        action: str,
        quantity: int,
        price: Decimal,
        exchange: str = "NSE",
    ) -> UpstreamPayload:
        if action not in ("buy", "sell"):
            raise RelayArgumentError("action")
        if not _is_int(quantity) or not 1 <= quantity <= 100_000:
            raise RelayArgumentError("quantity")
        return self._bytes(
            ROUTE_PREVIEW_ORDER,
            [
                ("stock_code", _stock_code(stock_code)),
                ("action", action),
                ("quantity", str(quantity)),
                ("price", _price(price)),
                ("exchange", _exchange(exchange)),
            ],
        )

    def security_master(self) -> UpstreamPayload:
        return self._bytes(ROUTE_SECURITY_MASTER, [])

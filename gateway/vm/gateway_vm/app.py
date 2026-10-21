"""The relay's FastAPI app: guard middleware, error envelope, login, bars.

Route handlers are plain `def` (FastAPI runs them in its threadpool) because
BreezeClient is synchronous. Order on every Breeze-backed route: authorize the
relay token and read the session in one step (authorize), validate parameters,
require_active() on what authorize returned, charge the budget, then the one
Breeze call. There is no order route and no funds, holdings, positions or
trades route here (Phase 63 owns order relay).

Nothing is written to disk and no body, query value, token, code or Breeze
Error text reaches a response or a log line.
"""

import hashlib
import json
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from .breeze_client import BreezeClient, BreezeError, V2_DAILY_INTERVAL, classify_payload
from .contract import (
    ALLOWED_BODY_FIELDS,
    ALLOWED_QUERY_BY_ROUTE,
    API_BARS_V2,
    API_SESSION_RE,
    ATTEMPT_ID_RE,
    BARS_EXCHANGE,
    BARS_FROM_PAD_DAYS,
    BARS_REQUEST_KNOBS,
    BARS_TO_PAD_DAYS,
    BEARER_RE,
    CACHE_CONTROL_VALUE,
    CONTRACT,
    DAILY_INTERVAL_SEMANTIC,
    DATE_RE,
    HDR_BREEZE_STATUS,
    HDR_CACHE_CONTROL,
    HDR_CONTRACT,
    HDR_EMPTY,
    HDR_REQUEST_ID,
    HDR_UP_API,
    HDR_UP_FETCHED_AT,
    HDR_UP_INTERVAL,
    HDR_UP_REQUEST,
    HDR_UP_SHA256,
    HOST_RE,
    MAX_RANGE_DAYS,
    ROUTE_BARS_DAILY,
    ROUTE_HEALTH,
    ROUTE_LOGIN_ARM,
    ROUTE_LOGIN_COMPLETE,
    ROUTE_SESSION_REVOKE,
    STOCK_CODE_RE,
)
from .events import ROUTE_VALUES, EventLog
from .session import (
    Credentials,
    RelayFailure,
    SessionManager,
    breeze_exception_failure,
    charge_budget,
)
from .session import BudgetGate
from .transport import Clock


# ------------------------------------------------------------- bodies


class _EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _CompleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    attempt_id: str = Field(pattern=ATTEMPT_ID_RE.pattern)
    apisession: str = Field(pattern=API_SESSION_RE.pattern)


# ------------------------------------------------------------ helpers


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _error_response(failure: RelayFailure) -> JSONResponse:
    body = {
        "contract": CONTRACT,
        "error": {
            "code": failure.code,
            "reason": failure.reason,
            "breeze_status": failure.breeze_status,
            "retryable": failure.retryable,
            "retry_after_s": failure.retry_after_s,
            "fields": failure.fields,
        },
    }
    return JSONResponse(body, status_code=failure.http)


def _record(scope: dict, failure: RelayFailure) -> None:
    scope["state"]["failure"] = failure


def _bearer(authorization: Optional[str] = Header(default=None)) -> Optional[str]:
    """The presented relay token, or None for anything that is not a well-formed bearer."""
    if authorization is None:
        return None
    match = BEARER_RE.fullmatch(authorization)
    return match.group(1) if match else None


def _require_json(request: Request) -> None:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";")[0].strip().lower() != "application/json":
        raise RelayFailure("VALIDATION", fields=["body"])


def _read_query(request: Request, route: str) -> tuple[dict[str, str], set[str]]:
    """C3: only the route's listed names, each at most once.

    Returns the accepted values and the offending parameter names. A name that is
    not listed is reported as "unknown": the submitted name is never echoed.
    """
    allowed = ALLOWED_QUERY_BY_ROUTE[route]
    seen: dict[str, str] = {}
    bad: set[str] = set()
    for name, value in request.query_params.multi_items():
        if name not in allowed:
            bad.add("unknown")
        elif name in seen:
            bad.add(name)
        else:
            seen[name] = value
    return seen, bad


def _query_allowlist(route: str):
    """Dependency for a route that takes no query parameters at all."""

    def check(request: Request) -> None:
        _seen, bad = _read_query(request, route)
        if bad:
            raise RelayFailure("VALIDATION", fields=sorted(bad))

    return check


def _parse_bars_query(request: Request) -> tuple[str, date, date]:
    """Strict C3 rules. A violation lists parameter names only, never values."""
    seen, bad = _read_query(request, ROUTE_BARS_DAILY)
    for required in ("stock_code", "from", "to"):
        if required not in seen:
            bad.add(required)

    stock_code = seen.get("stock_code", "")
    if "stock_code" in seen and not STOCK_CODE_RE.fullmatch(stock_code):
        bad.add("stock_code")
    if "exchange" in seen and seen["exchange"] != BARS_EXCHANGE:
        bad.add("exchange")

    parsed: dict[str, date] = {}
    for name in ("from", "to"):
        raw = seen.get(name)
        if raw is None:
            continue
        try:
            if not DATE_RE.fullmatch(raw):
                raise ValueError
            parsed[name] = date.fromisoformat(raw)
        except ValueError:
            bad.add(name)
    if "from" in parsed and "to" in parsed:
        span = (parsed["to"] - parsed["from"]).days
        if span < 0 or span > MAX_RANGE_DAYS:
            bad.update(("from", "to"))
    if bad:
        raise RelayFailure("VALIDATION", fields=sorted(bad))
    return stock_code, parsed["from"], parsed["to"]


def _is_empty_success(body: bytes) -> bool:
    success = json.loads(body).get("Success")
    return isinstance(success, (list, dict)) and len(success) == 0


# --------------------------------------------------------- middleware


class _Guard:
    """Outermost app middleware (pure ASGI).

    Refuses a non-loopback Host or a wrong contract header before routing, adds
    the C2 headers to every response (including Starlette's own 404 and 405),
    turns any escaped exception into 500 INTERNAL, and emits one request event.
    """

    def __init__(self, app, *, events: EventLog, clock: Clock) -> None:
        self.app = app
        self._events = events
        self._clock = clock

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return

        request_id = secrets.token_hex(8)
        scope["state"] = {**(scope.get("state") or {}), "request_id": request_id}
        started_at = self._clock.monotonic()
        sent = {"status": 0}

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                sent["status"] = message["status"]
                headers = MutableHeaders(scope=message)
                headers[HDR_CONTRACT] = CONTRACT
                headers[HDR_REQUEST_ID] = request_id
                headers[HDR_CACHE_CONTROL] = CACHE_CONTROL_VALUE
            await send(message)

        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        contracts = headers.getlist(HDR_CONTRACT)
        refusal: RelayFailure | None = None
        if len(hosts) != 1 or not HOST_RE.fullmatch(hosts[0]):
            refusal = RelayFailure("FORBIDDEN_CLIENT")
        elif not contracts:
            refusal = RelayFailure("FORBIDDEN_CLIENT")
        elif len(contracts) != 1 or contracts[0] != CONTRACT:
            refusal = RelayFailure("CONTRACT_MISMATCH")

        if refusal is not None:
            _record(scope, refusal)
            await _error_response(refusal)(scope, receive, send_with_headers)
        else:
            try:
                await self.app(scope, receive, send_with_headers)
            except Exception:  # nothing may reach the server's error logger
                failure = RelayFailure("INTERNAL")
                _record(scope, failure)
                if sent["status"] == 0:
                    await _error_response(failure)(scope, receive, send_with_headers)

        self._emit(scope, sent["status"], started_at)

    def _emit(self, scope, status: int, started_at: float) -> None:
        state = scope["state"]
        failure: RelayFailure | None = state.get("failure")
        path = scope.get("path", "")
        if failure is not None:
            result, reason, breeze_status = (
                failure.event_result,
                failure.event_reason,
                failure.breeze_status,
            )
        else:
            result = "ok" if status < 400 else "rejected"
            reason, breeze_status = None, state.get("breeze_status")
        self._events.emit(
            "request",
            route=path if path in ROUTE_VALUES else "other",
            result=result,
            reason=reason,
            http=status,
            breeze_status=breeze_status,
            ms=max(0, int((self._clock.monotonic() - started_at) * 1000)),
            request_id=state["request_id"],
        )


# ---------------------------------------------------------------- app


def create_app(
    *,
    session: SessionManager,
    breeze: BreezeClient,
    credentials: Credentials,
    budget: BudgetGate,
    events: EventLog,
    clock: Clock,
    version: str = "61.0.0",
) -> FastAPI:
    app = FastAPI(
        title="growin-relay",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    app.add_middleware(_Guard, events=events, clock=clock)

    @app.exception_handler(RelayFailure)
    async def _relay_failure(request: Request, exc: RelayFailure):
        _record(request.scope, exc)
        return _error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        names: set[str] = set()
        for err in exc.errors():
            loc = err.get("loc") or ()
            last = loc[-1] if loc else None
            names.add(last if last in ALLOWED_BODY_FIELDS else "body")
        failure = RelayFailure("VALIDATION", fields=sorted(names) or ["body"])
        _record(request.scope, failure)
        return _error_response(failure)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            failure = RelayFailure("NOT_FOUND")
        elif exc.status_code == 405:
            failure = RelayFailure("METHOD_NOT_ALLOWED")
        elif 400 <= exc.status_code < 500:
            failure = RelayFailure("VALIDATION", fields=["body"])
        else:
            failure = RelayFailure("INTERNAL")
        _record(request.scope, failure)
        return _error_response(failure)

    # ------------------------------------------------------------ health

    @app.get(ROUTE_HEALTH, dependencies=[Depends(_query_allowlist(ROUTE_HEALTH))])
    def health():
        snap = session.snapshot(refresh_egress_if_stale=True)
        return JSONResponse(
            {
                "contract": CONTRACT,
                **snap,
                "server_time_utc": _iso_utc(clock.now_utc()),
                "version": version,
            }
        )

    # ------------------------------------------------------------- login

    @app.post(
        ROUTE_LOGIN_ARM,
        dependencies=[Depends(_require_json), Depends(_query_allowlist(ROUTE_LOGIN_ARM))],
    )
    def login_arm(body: _EmptyBody, token: Optional[str] = Depends(_bearer)):
        attempt_id, login_url, relay_token, expires = session.arm(token)
        return JSONResponse(
            {
                "contract": CONTRACT,
                "attempt_id": attempt_id,
                "login_url": login_url,
                "relay_token": relay_token,
                "expires_at_utc": _iso_utc(expires),
            }
        )

    @app.post(
        ROUTE_LOGIN_COMPLETE,
        dependencies=[Depends(_require_json), Depends(_query_allowlist(ROUTE_LOGIN_COMPLETE))],
    )
    def login_complete(body: _CompleteBody, token: Optional[str] = Depends(_bearer)):
        expires = session.complete(body.attempt_id, body.apisession, token)
        return JSONResponse(
            {"contract": CONTRACT, "state": "active", "expires_at_ist": expires.isoformat()}
        )

    @app.post(
        ROUTE_SESSION_REVOKE,
        dependencies=[Depends(_require_json), Depends(_query_allowlist(ROUTE_SESSION_REVOKE))],
    )
    def session_revoke(body: _EmptyBody, token: Optional[str] = Depends(_bearer)):
        session.revoke(token)
        return JSONResponse({"contract": CONTRACT, "state": "none", "expires_at_ist": None})

    # -------------------------------------------------------------- bars

    @app.get(ROUTE_BARS_DAILY)
    def bars_daily(request: Request, token: Optional[str] = Depends(_bearer)):
        authorization = session.authorize(token)  # token check and session read, one lock hold
        stock_code, from_day, to_day = _parse_bars_query(request)
        session_token = session.require_active(authorization)
        charge_budget(budget, events)

        from_iso = (from_day - timedelta(days=BARS_FROM_PAD_DAYS)).isoformat() + "T00:00:00.000Z"
        to_iso = (to_day + timedelta(days=BARS_TO_PAD_DAYS)).isoformat() + "T23:59:59.000Z"
        try:
            raw = breeze.daily_bars_v2_raw(
                app_key=credentials.app_key,
                session_token=session_token,
                stock_code=stock_code,
                exch_code=BARS_EXCHANGE,
                from_iso=from_iso,
                to_iso=to_iso,
                interval=V2_DAILY_INTERVAL,
                **BARS_REQUEST_KNOBS,
            )
        except BreezeError as exc:
            raise breeze_exception_failure(exc) from None

        verdict = classify_payload(raw.http_status, raw.body)
        request.scope["state"]["breeze_status"] = verdict.breeze_status
        if not verdict.ok:
            raise session.note_breeze_error(
                verdict.error_class,
                breeze_status=verdict.breeze_status,
                http_status=raw.http_status,
            )

        upstream_request = json.dumps(
            {
                "exch_code": BARS_EXCHANGE,
                "from_date": from_iso,
                "interval": V2_DAILY_INTERVAL,
                "product_type": "cash",
                "stock_code": stock_code,
                "to_date": to_iso,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return Response(
            content=raw.body,
            media_type="application/json",
            headers={
                HDR_UP_SHA256: hashlib.sha256(raw.body).hexdigest(),
                HDR_UP_FETCHED_AT: _iso_utc(raw.fetched_at_utc),
                HDR_UP_API: API_BARS_V2,
                HDR_UP_REQUEST: upstream_request,
                HDR_UP_INTERVAL: DAILY_INTERVAL_SEMANTIC,
                HDR_BREEZE_STATUS: "200",
                HDR_EMPTY: "true" if _is_empty_success(raw.body) else "false",
            },
        )

    return app

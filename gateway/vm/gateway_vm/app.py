"""The relay's FastAPI app: guard middleware, error envelope, login, data routes.

Route handlers are plain `def` (FastAPI runs them in its threadpool) because
BreezeClient is synchronous. Order on every Breeze-backed route: authorize the
relay token and read the session in one step (authorize), validate parameters,
require_active() on what authorize returned, charge the budget, then the one
Breeze call. The data routes are bars, quotes, preview-order (a brokerage
calculator that places nothing) and the security master. There is no funds,
holdings, positions or trades route here. The five growin-orders/1 routes
(/v1/orders/...) are mounted from orders/routes.py when an OrderService is given;
they speak their own contract string and place nothing (63-05).

Nothing is written to disk and no body, query value, token, code or Breeze
Error text reaches a response or a log line.
"""

import hashlib
import json
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from .breeze_client import (
    V2_DAILY_INTERVAL,
    V2_DEFAULT_EXCHANGE_PARAM,
    V2_INTRADAY_INTERVAL,
    BreezeClient,
    BreezeError,
    RawUpstream,
    classify_payload,
    preview_payload,
    quote_payload,
)
from .contract import (
    ALLOWED_BODY_FIELDS,
    ALLOWED_QUERY_BY_ROUTE,
    API_BARS_V2,
    API_PREVIEW_V1,
    API_QUOTES_V1,
    API_SECURITY_MASTER,
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
    HDR_UP_LAST_MODIFIED,
    HDR_UP_REQUEST,
    HDR_UP_SHA256,
    HOST_RE,
    HTTP_DATE_RE,
    INTRADAY_INTERVAL_SEMANTIC,
    INTRADAY_INTERVALS,
    MAX_QUANTITY,
    MAX_RANGE_DAYS,
    PREVIEW_ACTIONS,
    PRICE_MAX,
    PRICE_MIN_EXCLUSIVE,
    PRICE_RE,
    QUANTITY_RE,
    ROUTE_BARS_DAILY,
    ROUTE_BARS_INTRADAY,
    ROUTE_BUDGET,
    ROUTE_HEALTH,
    ROUTE_LOGIN_ARM,
    ROUTE_LOGIN_COMPLETE,
    ROUTE_PREVIEW_ORDER,
    ROUTE_QUOTES,
    ROUTE_SECURITY_MASTER,
    ROUTE_SESSION_REVOKE,
    SECURITY_MASTER_MAX_BYTES,
    SECURITY_MASTER_TOTAL_S,
    STOCK_CODE_RE,
    ZIP_MAGIC,
)
from .events import ROUTE_VALUES, EventLog
from .orders import CONTRACT as ORDERS_CONTRACT
from .orders import ORDERS_PATH_PREFIX, OrderRefusal
from .orders.routes import OrderFailure, OrderService, mount_order_routes, order_error_response
from .session import (
    Credentials,
    RelayFailure,
    SessionManager,
    breeze_exception_failure,
    charge_budget,
)
from .session import BudgetGate
from .transport import Clock, Timeouts


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


def _contract_for(path: str) -> str:
    """O1: every path under /v1/orders/ speaks growin-orders/1, every other path growin-relay/1."""
    return ORDERS_CONTRACT if path.startswith(ORDERS_PATH_PREFIX) else CONTRACT


def _error_response(failure: RelayFailure, contract: str = CONTRACT) -> JSONResponse:
    body = {
        "contract": contract,
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


IST = timezone(timedelta(hours=5, minutes=30))


def _parse_intraday_query(request: Request, today_ist: date) -> tuple[str, date]:
    """C3 for /v1/bars/intraday: stock_code, date (not after today IST), interval (enum), exchange."""
    seen, bad = _read_query(request, ROUTE_BARS_INTRADAY)
    for required in ("stock_code", "date", "interval"):
        if required not in seen:
            bad.add(required)
    stock_code = seen.get("stock_code", "")
    if "stock_code" in seen and not STOCK_CODE_RE.fullmatch(stock_code):
        bad.add("stock_code")
    if "exchange" in seen and seen["exchange"] != BARS_EXCHANGE:
        bad.add("exchange")
    if "interval" in seen and seen["interval"] not in INTRADAY_INTERVALS:
        bad.add("interval")
    day: date | None = None
    if "date" in seen:
        try:
            if not DATE_RE.fullmatch(seen["date"]):
                raise ValueError
            day = date.fromisoformat(seen["date"])
        except ValueError:
            bad.add("date")
        else:
            if day > today_ist:
                bad.add("date")
    if bad:
        raise RelayFailure("VALIDATION", fields=sorted(bad))
    assert day is not None
    return stock_code, day


def _is_empty_success(body: bytes) -> bool:
    success = json.loads(body).get("Success")
    return isinstance(success, (list, dict)) and len(success) == 0


def _symbol_query(request: Request, route: str, required: tuple[str, ...]) -> tuple[dict[str, str], set[str]]:
    """The C3 rules quotes and preview-order share: stock_code and exchange."""
    seen, bad = _read_query(request, route)
    for name in required:
        if name not in seen:
            bad.add(name)
    if "stock_code" in seen and not STOCK_CODE_RE.fullmatch(seen["stock_code"]):
        bad.add("stock_code")
    if "exchange" in seen and seen["exchange"] != BARS_EXCHANGE:
        bad.add("exchange")
    return seen, bad


def _parse_quotes_query(request: Request) -> str:
    seen, bad = _symbol_query(request, ROUTE_QUOTES, ("stock_code",))
    if bad:
        raise RelayFailure("VALIDATION", fields=sorted(bad))
    return seen["stock_code"]


def _valid_price(raw: str) -> bool:
    """A plain decimal string, at most two places, no exponent, 0 < price <= 1,000,000."""
    if not PRICE_RE.fullmatch(raw):
        return False
    return PRICE_MIN_EXCLUSIVE < Decimal(raw) <= PRICE_MAX


def _parse_preview_query(request: Request) -> tuple[str, str, int, str]:
    """stock_code, action, quantity, price. The price goes upstream as the exact string validated."""
    seen, bad = _symbol_query(request, ROUTE_PREVIEW_ORDER, ("stock_code", "action", "quantity", "price"))
    if "action" in seen and seen["action"] not in PREVIEW_ACTIONS:
        bad.add("action")
    quantity = 0
    if "quantity" in seen:
        if QUANTITY_RE.fullmatch(seen["quantity"]) and int(seen["quantity"]) <= MAX_QUANTITY:
            quantity = int(seen["quantity"])
        else:
            bad.add("quantity")
    if "price" in seen and not _valid_price(seen["price"]):
        bad.add("price")
    if bad:
        raise RelayFailure("VALIDATION", fields=sorted(bad))
    return seen["stock_code"], seen["action"], quantity, seen["price"]


def _compact(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _breeze_bytes_response(
    request: Request,
    session: SessionManager,
    raw: RawUpstream,
    *,
    api: str,
    upstream_request: dict,
    interval: str | None = None,
) -> Response:
    """C4 for a Breeze route: only a good body is relayed, as the exact upstream bytes."""
    verdict = classify_payload(raw.http_status, raw.body)
    request.scope["state"]["breeze_status"] = verdict.breeze_status
    if not verdict.ok:
        raise session.note_breeze_error(
            verdict.error_class,
            breeze_status=verdict.breeze_status,
            http_status=raw.http_status,
        )
    headers = {
        HDR_UP_SHA256: hashlib.sha256(raw.body).hexdigest(),
        HDR_UP_FETCHED_AT: _iso_utc(raw.fetched_at_utc),
        HDR_UP_API: api,
        HDR_UP_REQUEST: _compact(upstream_request),
        HDR_BREEZE_STATUS: "200",
        HDR_EMPTY: "true" if _is_empty_success(raw.body) else "false",
    }
    if interval is not None:
        headers[HDR_UP_INTERVAL] = interval
    return Response(content=raw.body, media_type="application/json", headers=headers)


# --------------------------------------------------------- middleware


class _Guard:
    """Outermost app middleware (pure ASGI).

    Refuses a non-loopback Host or a wrong contract header before routing, adds
    the C2 headers to every response (including Starlette's own 404 and 405),
    turns any escaped exception into 500 INTERNAL, and emits one request event.
    The contract string is per path: growin-orders/1 under /v1/orders/, growin-relay/1
    everywhere else, and a request carrying the other one is 400 CONTRACT_MISMATCH.
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
        expected_contract = _contract_for(scope.get("path", ""))
        scope["state"] = {**(scope.get("state") or {}), "request_id": request_id}
        started_at = self._clock.monotonic()
        sent = {"status": 0}

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                sent["status"] = message["status"]
                headers = MutableHeaders(scope=message)
                headers[HDR_CONTRACT] = expected_contract
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
        elif len(contracts) != 1 or contracts[0] != expected_contract:
            refusal = RelayFailure("CONTRACT_MISMATCH")

        if refusal is not None:
            _record(scope, refusal)
            await _error_response(refusal, expected_contract)(scope, receive, send_with_headers)
        else:
            try:
                await self.app(scope, receive, send_with_headers)
            except Exception:  # nothing may reach the server's error logger
                failure = RelayFailure("INTERNAL")
                _record(scope, failure)
                if sent["status"] == 0:
                    await _error_response(failure, expected_contract)(scope, receive, send_with_headers)

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
    security_master_url: str | None = None,
    orders: OrderService | None = None,
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
        return _error_response(exc, _contract_for(request.url.path))

    @app.exception_handler(OrderRefusal)
    async def _order_refusal(request: Request, exc: OrderRefusal):
        _record(request.scope, OrderFailure(exc))
        return order_error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        names: set[str] = set()
        for err in exc.errors():
            loc = err.get("loc") or ()
            last = loc[-1] if loc else None
            names.add(last if last in ALLOWED_BODY_FIELDS else "body")
        failure = RelayFailure("VALIDATION", fields=sorted(names) or ["body"])
        _record(request.scope, failure)
        return _error_response(failure, _contract_for(request.url.path))

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
        return _error_response(failure, _contract_for(request.url.path))

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

        # The same name the request used, so the header describes the real request.
        exchange_param = BARS_REQUEST_KNOBS.get("exchange_param_name", V2_DEFAULT_EXCHANGE_PARAM)
        return _breeze_bytes_response(
            request,
            session,
            raw,
            api=API_BARS_V2,
            upstream_request={
                exchange_param: BARS_EXCHANGE,
                "from_date": from_iso,
                "interval": V2_DAILY_INTERVAL,
                "product_type": "cash",
                "stock_code": stock_code,
                "to_date": to_iso,
            },
            interval=DAILY_INTERVAL_SEMANTIC,
        )

    @app.get(ROUTE_BARS_INTRADAY)
    def bars_intraday(request: Request, token: Optional[str] = Depends(_bearer)):
        """One session of minute bars as the exact upstream bytes. Nothing is stored (OD-17)."""
        authorization = session.authorize(token)  # same order as bars_daily: 401, 422, 503, budget, Breeze
        today_ist = clock.now_utc().astimezone(IST).date()
        stock_code, day = _parse_intraday_query(request, today_ist)
        session_token = session.require_active(authorization)
        charge_budget(budget, events)

        # A whole-day window: the label timezone is unproven (U-3), so the Mac filters by label.
        from_iso = day.isoformat() + "T00:00:00.000Z"
        to_iso = day.isoformat() + "T23:59:59.000Z"
        try:
            raw = breeze.intraday_bars_v2_raw(
                app_key=credentials.app_key,
                session_token=session_token,
                stock_code=stock_code,
                exch_code=BARS_EXCHANGE,
                from_iso=from_iso,
                to_iso=to_iso,
                **BARS_REQUEST_KNOBS,
            )
        except BreezeError as exc:
            raise breeze_exception_failure(exc) from None

        exchange_param = BARS_REQUEST_KNOBS.get("exchange_param_name", V2_DEFAULT_EXCHANGE_PARAM)
        return _breeze_bytes_response(
            request,
            session,
            raw,
            api=API_BARS_V2,
            upstream_request={
                exchange_param: BARS_EXCHANGE,
                "from_date": from_iso,
                "interval": V2_INTRADAY_INTERVAL,
                "product_type": "cash",
                "stock_code": stock_code,
                "to_date": to_iso,
            },
            interval=INTRADAY_INTERVAL_SEMANTIC,
        )

    # ------------------------------------------------------------ quotes

    @app.get(ROUTE_QUOTES)
    def quotes(request: Request, token: Optional[str] = Depends(_bearer)):
        authorization = session.authorize(token)
        stock_code = _parse_quotes_query(request)
        session_token = session.require_active(authorization)
        charge_budget(budget, events)
        try:
            raw = breeze.quote_raw(
                app_key=credentials.app_key,
                secret_key=credentials.secret_key,
                session_token=session_token,
                stock_code=stock_code,
                exchange_code=BARS_EXCHANGE,
            )
        except BreezeError as exc:
            raise breeze_exception_failure(exc) from None
        return _breeze_bytes_response(
            request,
            session,
            raw,
            api=API_QUOTES_V1,
            upstream_request=quote_payload(stock_code, BARS_EXCHANGE),
        )

    # ----------------------------------------------------- preview-order

    @app.get(ROUTE_PREVIEW_ORDER)
    def preview_order(request: Request, token: Optional[str] = Depends(_bearer)):
        """The brokerage-charge calculator. It prices a hypothetical order and places nothing."""
        authorization = session.authorize(token)
        stock_code, action, quantity, price = _parse_preview_query(request)
        session_token = session.require_active(authorization)
        charge_budget(budget, events)
        try:
            raw = breeze.preview_order_raw(
                app_key=credentials.app_key,
                secret_key=credentials.secret_key,
                session_token=session_token,
                stock_code=stock_code,
                exchange_code=BARS_EXCHANGE,
                action=action,
                quantity=quantity,
                price=price,
            )
        except BreezeError as exc:
            raise breeze_exception_failure(exc) from None
        return _breeze_bytes_response(
            request,
            session,
            raw,
            api=API_PREVIEW_V1,
            upstream_request=preview_payload(stock_code, BARS_EXCHANGE, action, quantity, price),
        )

    # --------------------------------------------------- security master

    @app.get(ROUTE_SECURITY_MASTER, dependencies=[Depends(_query_allowlist(ROUTE_SECURITY_MASTER))])
    def security_master(token: Optional[str] = Depends(_bearer)):
        """The ICICI SecurityMaster.zip. Needs a fresh egress check, not a Breeze session."""
        session.authorize(token)  # a bound session still needs its bearer
        if security_master_url is None:  # create_app was built without the URL: fail closed
            events.emit("config_alarm", result="error", reason="config_invalid")
            raise RelayFailure("INTERNAL", reason="config_invalid")
        charge_budget(budget, events, "security_master")  # the cap first: a refusal makes no egress request
        session.require_egress(token)  # ok and fresh evidence, same bearer, under one lock hold
        try:
            raw = breeze.security_master_raw(
                security_master_url,
                timeouts=Timeouts(connect_s=5.0, read_s=30.0, total_s=SECURITY_MASTER_TOTAL_S),
                max_bytes=SECURITY_MASTER_MAX_BYTES,
            )
        except BreezeError as exc:
            raise breeze_exception_failure(exc) from None
        if raw.http_status != 200:
            raise RelayFailure("UPSTREAM_ERROR", reason="upstream_http")
        if not raw.body.startswith(ZIP_MAGIC):
            raise RelayFailure("UPSTREAM_ERROR", reason="upstream_shape")
        headers = {
            HDR_UP_SHA256: hashlib.sha256(raw.body).hexdigest(),
            HDR_UP_FETCHED_AT: _iso_utc(raw.fetched_at_utc),
            HDR_UP_API: API_SECURITY_MASTER,
            HDR_UP_REQUEST: _compact({"url": security_master_url}),
        }
        last_modified = raw.headers.get("last-modified")
        if last_modified is not None and HTTP_DATE_RE.fullmatch(last_modified):
            headers[HDR_UP_LAST_MODIFIED] = last_modified
        return Response(content=raw.body, media_type="application/zip", headers=headers)

    # ------------------------------------------------------------ budget

    @app.get(ROUTE_BUDGET, dependencies=[Depends(_query_allowlist(ROUTE_BUDGET))])
    def budget_snapshot(token: Optional[str] = Depends(_bearer)):
        session.authorize(token)  # the bearer is checked while a session is bound; the state is not
        return JSONResponse({"contract": CONTRACT, **budget.snapshot()})

    # ------------------------------------------------------------ orders

    if orders is not None:
        mount_order_routes(app, orders, session=session, bearer=_bearer, require_json=_require_json)

    return app

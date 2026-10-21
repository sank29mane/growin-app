"""The five growin-orders/1 routes (O2) and the wiring behind them (63-05).

    POST /v1/orders/intents     mint a challenge for one intent
    POST /v1/orders/authorize   verify the Mac's signature, re-run every check, audit
    POST /v1/orders/halt        set mac_halt (restricts; nothing clears it here)
    GET  /v1/orders/state       kill state, latches, drawdown, peak_date, last_evaluated_session
    GET  /v1/orders/audit       at most 200 audit entries after after_seq

The handlers add nothing to the 63-01 pipeline. They check the relay bearer, bind the
Breeze session token for the ports, call mint or authorize, and shape the answer. A
verified intent is audited and answered VERIFIED_NOT_FORWARDED: the forward step is the
constant refusal object and no route here places, changes or withdraws an order.

Order of a Breeze-backed route: bearer check, order configuration ready, body
validation, the Breeze session (require_active), then the pipeline, whose ports charge
the relay budget before each Breeze GET. halt, state and audit need only the bearer:
they never touch Breeze and halt only restricts.

If the limits file, the order key or the state directory cannot be trusted at start,
every order route answers 503 with the reason (config_invalid, state_unreadable) while
health and the data routes keep working.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from ..breeze_client import BreezeClient
from ..events import ORDER_REASONS, EventLog
from ..session import BudgetGate, Credentials, RelayFailure, SessionManager
from ..transport import Clock
from . import (
    CONTRACT,
    ROUTE_AUDIT,
    ROUTE_AUTHORIZE,
    ROUTE_HALT,
    ROUTE_INTENTS,
    ROUTE_STATE,
    OrderRefusal,
    intent_invalid,
    refusal,
)
from .alerts import AlertLogError, FileAlertPort
from .audit import AuditBroken, AuditLog
from .intent import parse_intent
from .kill import DEFAULT_BASE_URL, MetadataKillReader
from .limits import DEFAULT_LIMITS_PATH, IST, LimitsError, TickTable, load_limits
from .master import MasterHolder
from .pipeline import OrderPipeline, RefusalForward, RuleGuard
from .reads import (
    BarsMarks,
    BarsTickReference,
    BreezeAccount,
    BreezeBars,
    BreezeMarket,
    BreezeReads,
    ReadContext,
    ReadFailed,
)
from .store import AUDIT_FILE, StateStore
from .verify import KeyConfigError, load_order_key

DEFAULT_KEY_PATH = "/etc/growin-gateway/order-key.json"
MAX_BODY_BYTES = 8192
AUDIT_PAGE = 200
_AFTER_SEQ = re.compile(r"[0-9]{1,12}")


@dataclass(frozen=True)
class OrderSettings:
    """Where the order configuration lives. Tests override these; production does not.

    ``owner_uid`` is the uid that must own the limits and key files (root, 0). It is
    None only in tests. ``allow_test_key`` lets a test pin a committed TEST ONLY key;
    nothing in the production start-up path sets it. ``kill_base_url`` defaults to the
    instance metadata server and a test points it at a loopback fake.
    """

    state_dir: Path | None = None
    limits_path: str = DEFAULT_LIMITS_PATH
    key_path: str = DEFAULT_KEY_PATH
    owner_uid: int | None = 0
    allow_test_key: bool = False
    kill_base_url: str = DEFAULT_BASE_URL
    tick_table: TickTable | None = None


class OrderFailure:
    """What the request log needs from an OrderRefusal: a result and a closed-vocabulary reason."""

    event_result = "rejected"
    breeze_status = None

    def __init__(self, exc: OrderRefusal) -> None:
        if exc.code in ORDER_REASONS:
            self.event_reason = exc.code
        elif exc.error == "INTENT_INVALID":
            self.event_reason = "intent_invalid"
        else:
            self.event_reason = "other"


def order_error_response(exc: OrderRefusal) -> JSONResponse:
    """O6: the C6 body shape with the orders contract. ``code`` is the category, ``reason`` the O6 code."""
    body = {
        "contract": CONTRACT,
        "error": {
            "code": exc.error,
            "reason": exc.code,
            "breeze_status": None,
            "retryable": False,
            "retry_after_s": None,
            "fields": None,
        },
    }
    return JSONResponse(body, status_code=exc.status)


class OrderService:
    """The built order stack, or the one refusal every order route answers with."""

    def __init__(
        self,
        *,
        context: ReadContext,
        unavailable: OrderRefusal | None = None,
        pipeline: OrderPipeline | None = None,
        store: StateStore | None = None,
        audit: AuditLog | None = None,
        kill: MetadataKillReader | None = None,
        alerts: FileAlertPort | None = None,
    ) -> None:
        self.context = context
        self.unavailable = unavailable
        self.pipeline = pipeline
        self.store = store
        self.audit = audit
        self.kill = kill
        self.alerts = alerts

    def ready(self) -> None:
        if self.unavailable is not None or self.pipeline is None:
            raise self.unavailable or refusal("config_invalid")


def build_order_service(
    settings: OrderSettings,
    *,
    session: SessionManager,
    breeze: BreezeClient,
    credentials: Credentials,
    budget: BudgetGate,
    events: EventLog,
    clock: Clock,
    security_master_url: str | None,
) -> OrderService:
    """Load the limits, the pinned key and the state once. Any refusal becomes the service's answer."""
    context = ReadContext()
    try:
        limits = load_limits(settings.limits_path, expected_owner_uid=settings.owner_uid)
        key = load_order_key(
            settings.key_path,
            expected_owner_uid=settings.owner_uid,
            allow_test_key=settings.allow_test_key,
        )
        state_dir = settings.state_dir
        if state_dir is None or not Path(state_dir).is_dir():
            raise LimitsError("no state directory")
        store = StateStore(state_dir)
        audit = AuditLog(Path(state_dir) / AUDIT_FILE, clock=clock.now_utc, anchor=store)
        store.load_or_init(limits, audit_has_entries=audit.has_entries())
    except (LimitsError, KeyConfigError):
        events.emit("config_alarm", result="error", reason="config_invalid")
        return OrderService(context=context, unavailable=refusal("config_invalid"))
    except OrderRefusal as exc:
        events.emit("config_alarm", result="error", reason=exc.code)
        return OrderService(context=context, unavailable=exc)

    reads = BreezeReads(
        breeze=breeze,
        credentials=credentials,
        budget=budget,
        events=events,
        session=session,
        context=context,
    )

    def fetch_master() -> bytes:
        if security_master_url is None:
            raise ReadFailed("no_master_url")
        return reads.security_master_bytes(security_master_url)

    master = MasterHolder(fetch_master, lambda: clock.now_utc().astimezone(IST).date())
    bars = BreezeBars(reads=reads, master=master)
    market = BreezeMarket(reads=reads, master=master)
    tick_reference = BarsTickReference(bars=bars)
    guard = RuleGuard(
        limits=limits,
        kill=MetadataKillReader(settings.kill_base_url),
        store=store,
        account=BreezeAccount(reads=reads, master=master, clock=clock),
        market=market,
        tick_reference=tick_reference,
        audit=audit,
        tick_table=settings.tick_table,
        marks=BarsMarks(
            bars=bars,
            market=market,
            tick_reference=tick_reference,
            master=master,
            tick_table=settings.tick_table,
        ),
    )
    pipeline = OrderPipeline(
        key=key,
        limits_sha256=limits.sha256,
        intents=store,
        guard=guard,
        audit=audit,
        clock=clock.now_utc,
        monotonic=clock.monotonic,
        forward=RefusalForward(),
        lock=store.lock(),
    )
    return OrderService(
        context=context,
        pipeline=pipeline,
        store=store,
        audit=audit,
        kill=MetadataKillReader(settings.kill_base_url),
        alerts=FileAlertPort(state_dir),
    )


# ------------------------------------------------------------------- bodies


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _no_number(_text: str) -> Any:
    raise ValueError("number")


def _object(raw: bytes) -> dict[str, Any]:
    """A strict JSON object: ASCII, no duplicate keys, no floats or constants."""
    try:
        text = raw.decode("ascii")
        parsed = json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_float=_no_number,
            parse_constant=_no_number,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise intent_invalid("bad_body") from None
    if not isinstance(parsed, dict):
        raise intent_invalid("bad_body")
    return parsed


def _authorize_body(raw: bytes) -> tuple[str, bytes]:
    body = _object(raw)
    if set(body) != {"challenge_id", "signature_der_b64"}:
        raise intent_invalid("bad_body")
    challenge_id, encoded = body["challenge_id"], body["signature_der_b64"]
    if not isinstance(challenge_id, str) or not isinstance(encoded, str) or len(encoded) > 256:
        raise intent_invalid("bad_body")
    try:
        signature = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise intent_invalid("bad_signature_b64") from None
    return challenge_id, signature


async def _raw_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise RelayFailure("VALIDATION", fields=["body"])
        chunks.append(chunk)
    return b"".join(chunks)


def _no_query(request: Request) -> None:
    if request.query_params:
        raise RelayFailure("VALIDATION", fields=["unknown"])


def _audit_query(request: Request) -> int:
    names = [name for name, _ in request.query_params.multi_items()]
    if any(name != "after_seq" for name in names):
        raise RelayFailure("VALIDATION", fields=["unknown"])
    if len(names) > 1:
        raise RelayFailure("VALIDATION", fields=["after_seq"])
    if not names:
        return 0
    value = request.query_params["after_seq"]
    if _AFTER_SEQ.fullmatch(value) is None:
        raise RelayFailure("VALIDATION", fields=["after_seq"])
    return int(value)


# ------------------------------------------------------------------- routes


def mount_order_routes(
    app: FastAPI,
    service: OrderService,
    *,
    session: SessionManager,
    bearer: Callable[..., Any],
    require_json: Callable[..., Any],
) -> None:
    def pipeline() -> OrderPipeline:
        service.ready()
        assert service.pipeline is not None
        return service.pipeline

    @app.post(ROUTE_INTENTS, dependencies=[Depends(require_json), Depends(_no_query)])
    def mint(raw: bytes = Depends(_raw_body), token: str | None = Depends(bearer)):
        authorization = session.authorize(token)
        order = pipeline()
        parse_intent(raw)  # a malformed or LIVE intent is answered before any session or read
        session_token = session.require_active(authorization)
        with service.context.bound(token, session_token):
            return JSONResponse(order.mint(raw).body())

    @app.post(ROUTE_AUTHORIZE, dependencies=[Depends(require_json), Depends(_no_query)])
    def authorize(raw: bytes = Depends(_raw_body), token: str | None = Depends(bearer)):
        authorization = session.authorize(token)
        order = pipeline()
        challenge_id, signature = _authorize_body(raw)
        session_token = session.require_active(authorization)
        with service.context.bound(token, session_token):
            return JSONResponse(order.authorize(challenge_id, signature).body())

    @app.post(ROUTE_HALT, dependencies=[Depends(require_json), Depends(_no_query)])
    def halt(raw: bytes = Depends(_raw_body), token: str | None = Depends(bearer)):
        session.authorize(token)
        order = pipeline()
        if _object(raw):
            raise intent_invalid("bad_body")
        return JSONResponse(order.halt())

    @app.get(ROUTE_STATE, dependencies=[Depends(_no_query)])
    def state(token: str | None = Depends(bearer)):
        session.authorize(token)
        pipeline()
        assert service.store is not None and service.kill is not None and service.alerts is not None
        kill = service.kill.read()
        with service.store.lock():
            current = service.store.load()
        try:
            alerts = service.alerts.count()
        except AlertLogError:
            raise refusal("state_unreadable") from None
        return JSONResponse(
            {
                "contract": CONTRACT,
                "kill": kill.label,
                "latches": list(current.latch_names()),
                "drawdown": format(current.drawdown, "f"),
                "peak_date": current.peak_date,
                "last_evaluated_session": current.last_evaluated_session,
                "alerts_logged": alerts,
            }
        )

    @app.get(ROUTE_AUDIT)
    def audit_page(request: Request, token: str | None = Depends(bearer)):
        session.authorize(token)
        pipeline()
        after_seq = _audit_query(request)
        assert service.store is not None and service.audit is not None
        try:
            with service.store.lock():
                entries = service.audit.entries_after(after_seq, AUDIT_PAGE)
        except AuditBroken:
            raise refusal("audit_broken") from None
        return JSONResponse({"contract": CONTRACT, "entries": entries})

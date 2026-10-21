"""The growin-relay/1 values Phase 59 depends on, copied here and pinned by a parity test.

Phase 61's RelayClient (brokers.breeze_gateway) is the only transport. This module adds no
transport code: it re-checks every payload at the storage boundary, gates a run on relay health
and budget, and maps every relay failure to one fail-closed action. It never retries, sleeps or
opens a connection; classify_gateway_error only says how long the caller should sleep.

Exception messages carry stable codes and relay request ids, never bodies, headers or query values.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Literal, Protocol

from brokers.breeze_gateway import (
    BudgetExhausted,
    BudgetUnavailable,
    ContractMismatch,
    EgressMismatch,
    ForbiddenClient,
    IdentityMismatch,
    InternalRelayError,
    LoginAttemptInvalid,
    MethodNotAllowed,
    NoSession,
    NotFound,
    RelayBudget,
    RelayClient,
    RelayContractMismatch,
    RelayHashMismatch,
    RelayHealth,
    RelayProtocolError,
    RelayUnavailable,
    RelayValidationError,
    SessionExpired,
    TunnelError,
    Unauthorized,
    UpstreamError,
    UpstreamPayload,
    UpstreamTimeout,
)

from .core import PilotDataError, SourceDescriptor, sha256_hex

EXPECTED_CONTRACT_VERSION = "growin-relay/1"

COPIED_HEADERS: tuple[str, ...] = (
    "X-Growin-Relay-Contract",
    "X-Growin-Relay-Request-Id",
    "X-Growin-Upstream-Sha256",
    "X-Growin-Upstream-Fetched-At",
    "X-Growin-Upstream-Api",
    "X-Growin-Upstream-Request",
    "X-Growin-Upstream-Interval",
    "X-Growin-Breeze-Status",
    "X-Growin-Empty",
    "X-Growin-Upstream-Last-Modified",
)

ROUTE_DAILY_BARS = "/v1/bars/daily"
ROUTE_SECURITY_MASTER = "/v1/security-master"
COPIED_ROUTES: tuple[str, ...] = ("/v1/health", "/v1/budget", ROUTE_DAILY_BARS, ROUTE_SECURITY_MASTER)

API_DAILY_BARS = "breeze-historicalcharts-v2"
API_SECURITY_MASTER = "icici-security-master-zip"
INTERVAL_DAILY = "1day"

KIND_DAILY_BARS = "breeze_v2_daily"
KIND_SECURITY_MASTER = "security_master_zip"

REQUEST_ID_RE = r"^[0-9a-f]{16}$"
SHA256_RE = r"^[0-9a-f]{64}$"
STOCK_CODE_RE = r"^[A-Z0-9]{1,10}$"
DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"

COPIED_PAYLOAD_FIELDS: tuple[str, ...] = (
    "body",
    "sha256",
    "fetched_at_utc",
    "upstream_api",
    "upstream_request",
    "upstream_interval",
    "breeze_status",
    "empty",
    "upstream_last_modified",
    "request_id",
    "route",
    "query",
    "contract",
)
COPIED_HEALTH_FIELDS: tuple[str, ...] = (
    "state",
    "login_pending",
    "expires_at_ist",
    "egress_ok",
    "egress_checked_at_utc",
    "server_time_utc",
    "version",
)
COPIED_BUDGET_FIELDS: tuple[str, ...] = (
    "calls_last_minute",
    "calls_today_ist",
    "per_minute_limit",
    "per_day_limit",
    "remaining_today",
    "ist_date",
)

# C6 code -> the Phase 61 class name.
COPIED_ERROR_CODES: dict[str, str] = {
    "UNAUTHORIZED": "Unauthorized",
    "NO_SESSION": "NoSession",
    "SESSION_EXPIRED": "SessionExpired",
    "EGRESS_MISMATCH": "EgressMismatch",
    "BUDGET_UNAVAILABLE": "BudgetUnavailable",
    "BUDGET_EXHAUSTED": "BudgetExhausted",
    "UPSTREAM_ERROR": "UpstreamError",
    "UPSTREAM_TIMEOUT": "UpstreamTimeout",
    "VALIDATION": "RelayValidationError",
    "LOGIN_ATTEMPT_INVALID": "LoginAttemptInvalid",
    "IDENTITY_MISMATCH": "IdentityMismatch",
    "FORBIDDEN_CLIENT": "ForbiddenClient",
    "CONTRACT_MISMATCH": "ContractMismatch",
    "NOT_FOUND": "NotFound",
    "METHOD_NOT_ALLOWED": "MethodNotAllowed",
    "INTERNAL": "InternalRelayError",
}

# Reason values the failure policy branches on (C6).
REASON_BUDGET_MINUTE = "budget_minute"
REASON_BUDGET_DAY = "budget_day"
REASON_SECURITY_MASTER_HOURLY = "security_master_hourly"
REASON_NO_DATA = "no_data"
COPIED_POLICY_REASONS: frozenset[str] = frozenset(
    {
        REASON_BUDGET_MINUTE,
        REASON_BUDGET_DAY,
        REASON_SECURITY_MASTER_HOURLY,
        REASON_NO_DATA,
        "upstream_http",
        "upstream_shape",
        "other",
        "invalid_session",
        "public_key_missing",
    }
)

# 61-SMOKE-BREEZE (2026-10-21): a request that returns this many rows was capped, and the cap
# keeps the newest rows, so the answer is missing its oldest days.
BREEZE_ROW_CAP = 1000

_REQUEST_ID = re.compile(REQUEST_ID_RE)
_SHA256 = re.compile(SHA256_RE)
_STOCK_CODE = re.compile(STOCK_CODE_RE)
_DATE = re.compile(DATE_RE)

RELAY_RESPONSE_INVALID = "relay_response_invalid"


class RelaySource(Protocol):
    """The RelayClient subset Phase 59 calls. RelayClient satisfies it structurally."""

    def health(self) -> RelayHealth: ...

    def budget(self) -> RelayBudget: ...

    def daily_bars(
        self, stock_code: str, from_date: date, to_date: date, *, exchange: str = "NSE"
    ) -> UpstreamPayload: ...

    def security_master(self) -> UpstreamPayload: ...


def open_relay_source() -> RelayClient:
    """RelayClient.open(): it loads its own config and owns its tunnel. Use as a context manager."""
    return RelayClient.open()


# ----------------------------------------------------------------- payload validation


def _invalid(payload: object) -> PilotDataError:
    request_id = getattr(payload, "request_id", None)
    suffix = f" (request {request_id})" if isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id) else ""
    return PilotDataError(RELAY_RESPONSE_INVALID, f"relay payload failed re-validation{suffix}")


def _is_utc_aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _query_ok(route: str, query: Mapping[str, str]) -> bool:
    if not isinstance(query, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in query.items()
    ):
        return False
    if route == ROUTE_SECURITY_MASTER:
        return not query
    if set(query) - {"stock_code", "from", "to", "exchange"} or not {"stock_code", "from", "to"} <= set(query):
        return False
    if not _STOCK_CODE.fullmatch(query["stock_code"]) or query.get("exchange", "NSE") != "NSE":
        return False
    try:
        start, end = (date.fromisoformat(query[name]) for name in ("from", "to"))
    except ValueError:
        return False
    return all(_DATE.fullmatch(query[name]) for name in ("from", "to")) and start <= end


def _day_part(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _upstream_echo_ok(upstream_request: Mapping[str, str], query: Mapping[str, str]) -> bool:
    """The exact upstream parameters must echo the request: same code, same days (at most one day wider)."""
    if upstream_request.get("stock_code") != query["stock_code"]:
        return False
    if upstream_request.get("interval") != INTERVAL_DAILY or upstream_request.get("product_type") != "cash":
        return False
    sent_from, sent_to = _day_part(upstream_request.get("from_date")), _day_part(upstream_request.get("to_date"))
    want_from, want_to = date.fromisoformat(query["from"]), date.fromisoformat(query["to"])
    if sent_from is None or sent_to is None:
        return False
    return (
        want_from - timedelta(days=1) <= sent_from <= want_from
        and want_to <= sent_to <= want_to + timedelta(days=1)
    )


def locator_for(route: str, query: Mapping[str, str]) -> str:
    """`relay:<route>?<sorted query>`, built only from a validated route and query."""
    if not query:
        return f"relay:{route}"
    return f"relay:{route}?" + "&".join(f"{key}={query[key]}" for key in sorted(query))


def validate_payload(
    payload: UpstreamPayload,
    *,
    expected_api: str,
    kind: str,
    expected_query: Mapping[str, str] | None = None,
) -> SourceDescriptor:
    """Re-check a relay payload at the storage boundary and describe it as a source.

    Phase 61's client already checked the sha256 header on receipt. This is a second, deliberate
    check just before anything is stored. `expected_query`, when given, is the request the caller
    sent: the payload must echo it exactly.
    """
    if not isinstance(payload, UpstreamPayload):
        raise _invalid(payload)
    if kind == KIND_DAILY_BARS:
        route = ROUTE_DAILY_BARS
    elif kind == KIND_SECURITY_MASTER:
        route = ROUTE_SECURITY_MASTER
    else:
        raise PilotDataError("relay_kind_invalid", "unknown payload kind")
    if payload.contract != EXPECTED_CONTRACT_VERSION:
        raise PilotDataError("relay_contract_mismatch", "relay payload carries another contract version")
    if not isinstance(payload.body, bytes) or not isinstance(payload.sha256, str):
        raise _invalid(payload)
    claimed = payload.sha256.encode("utf-8")
    actual = sha256_hex(payload.body).encode("ascii")
    if not hmac.compare_digest(claimed, actual):
        suffix = f" (request {payload.request_id})" if _REQUEST_ID.fullmatch(str(payload.request_id)) else ""
        raise PilotDataError("relay_hash_mismatch", f"relay body does not match its sha256{suffix}")
    if (
        not _is_utc_aware(payload.fetched_at_utc)
        or not isinstance(payload.request_id, str)
        or not _REQUEST_ID.fullmatch(payload.request_id)
        or payload.upstream_api != expected_api
        or payload.route != route
        or not isinstance(payload.upstream_request, Mapping)
        or not _query_ok(route, payload.query)
    ):
        raise _invalid(payload)
    if kind == KIND_DAILY_BARS:
        if (
            payload.upstream_interval != INTERVAL_DAILY
            or payload.breeze_status != 200
            or not isinstance(payload.empty, bool)
            or not _upstream_echo_ok(payload.upstream_request, payload.query)
        ):
            raise _invalid(payload)
        if expected_query is not None and dict(payload.query) != dict(expected_query):
            raise _invalid(payload)
    elif expected_query is not None and dict(payload.query) != dict(expected_query):
        raise _invalid(payload)
    return SourceDescriptor(
        source="breeze_relay",
        kind=kind,
        locator=locator_for(route, payload.query),
        fetched_at=payload.fetched_at_utc,
    )


# --------------------------------------------------------------------------- gating


def gate_health(health: RelayHealth) -> None:
    """Pass only an active session whose egress check says ok."""
    state = health.state
    if state == "none":
        raise PilotDataError("breeze_session_missing", "relay has no Breeze session")
    if state == "expired":
        raise PilotDataError("breeze_session_stale", "relay Breeze session has expired")
    if state == "suspended_egress":
        raise PilotDataError("egress_ip_unverified", "relay egress is suspended")
    if state != "active":
        raise PilotDataError(RELAY_RESPONSE_INVALID, "relay health state is not recognised")
    if health.egress_ok is not True:
        raise PilotDataError("egress_ip_unverified", "relay egress is not verified")


def gate_budget(budget: RelayBudget, *, today_ist: date) -> None:
    """The relay's day must be today's IST date (else its counts are not today's), with a call left."""
    if budget.ist_date != today_ist:
        raise PilotDataError("relay_clock_mismatch", "relay budget day differs from today (IST)")
    if budget.remaining_today < 1:
        raise PilotDataError("relay_budget_day", "relay has no Breeze calls left today")


# ------------------------------------------------------------------ error classification

GatewayAction = Literal["stop", "retry_once", "empty", "fail_window"]

_STOP_BY_CLASS: tuple[tuple[type[BaseException], str], ...] = (
    (NoSession, "breeze_session_missing"),
    (SessionExpired, "breeze_session_stale"),
    (EgressMismatch, "egress_ip_unverified"),
    (Unauthorized, "relay_unauthorized"),
    (BudgetUnavailable, "relay_budget_unavailable"),
    (RelayValidationError, "relay_validation"),
    (ForbiddenClient, "relay_forbidden_client"),
    (ContractMismatch, "relay_contract_mismatch"),
    (IdentityMismatch, "relay_identity_mismatch"),
    (LoginAttemptInvalid, "relay_login_attempt_invalid"),
    (NotFound, "relay_not_found"),
    (MethodNotAllowed, "relay_method_not_allowed"),
    (InternalRelayError, "relay_internal"),
    (RelayContractMismatch, "relay_contract_mismatch"),
    (RelayHashMismatch, "relay_hash_mismatch"),
    (RelayProtocolError, "relay_protocol_error"),
    (TunnelError, "relay_tunnel_error"),
)


def classify_gateway_error(exc: BaseException) -> tuple[GatewayAction, str, float]:
    """(action, stable code, seconds to sleep before a retry) for anything a relay call raised."""
    if isinstance(exc, BudgetExhausted):
        if exc.reason == REASON_BUDGET_MINUTE:
            wait = float(min(exc.retry_after_s or 60, 60))
            return ("retry_once", "relay_budget_minute", max(wait, 0.0))
        if exc.reason == REASON_BUDGET_DAY:
            return ("stop", "relay_budget_day", 0.0)
        if exc.reason == REASON_SECURITY_MASTER_HOURLY:
            return ("stop", "relay_security_master_hourly", 0.0)
        return ("stop", "relay_budget_exhausted", 0.0)
    if isinstance(exc, UpstreamError):
        reason = exc.reason
        if reason == REASON_NO_DATA:
            return ("empty", "breeze_no_data", 0.0)
        if reason in ("upstream_http", "upstream_shape", "other"):
            return ("fail_window", f"upstream_{reason}", 0.0)
        if reason in ("invalid_session", "public_key_missing"):
            return ("stop", f"upstream_{reason}", 0.0)
        return ("stop", "upstream_unknown", 0.0)
    if isinstance(exc, UpstreamTimeout):
        return ("retry_once", "upstream_timeout", 0.0)
    if isinstance(exc, RelayUnavailable):
        return ("retry_once", "relay_unavailable", 0.0)
    for cls, code in _STOP_BY_CLASS:
        if isinstance(exc, cls):
            return ("stop", code, 0.0)
    return ("stop", "gateway_error_unknown", 0.0)

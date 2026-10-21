"""Constants of relay contract growin-relay/1 (server side).

Plan 61-09 holds the canonical text of the contract; this module is the VM's
copy of every name and limit it needs. Nothing here is secret and nothing here
performs I/O.

Two groups of values come from the Breeze smoke result (61-SMOKE-BREEZE.json)
and are marked below with the JSON key they were derived from and the run date.
No account value from that file is copied into the repo.
"""

from __future__ import annotations

import re
from typing import Mapping

CONTRACT = "growin-relay/1"

# ---------------------------------------------------------------- headers

HDR_CONTRACT = "X-Growin-Relay-Contract"
HDR_REQUEST_ID = "X-Growin-Relay-Request-Id"
HDR_CACHE_CONTROL = "Cache-Control"
CACHE_CONTROL_VALUE = "no-store"

HDR_UP_SHA256 = "X-Growin-Upstream-Sha256"
HDR_UP_FETCHED_AT = "X-Growin-Upstream-Fetched-At"
HDR_UP_API = "X-Growin-Upstream-Api"
HDR_UP_REQUEST = "X-Growin-Upstream-Request"
HDR_UP_INTERVAL = "X-Growin-Upstream-Interval"
HDR_BREEZE_STATUS = "X-Growin-Breeze-Status"
HDR_EMPTY = "X-Growin-Empty"

HDR_AUTHORIZATION = "Authorization"
BEARER_RE = re.compile(r"^Bearer ([A-Za-z0-9_-]{43})$")

# ----------------------------------------------------------------- routes

ROUTE_HEALTH = "/v1/health"
ROUTE_LOGIN_ARM = "/v1/login/arm"
ROUTE_LOGIN_COMPLETE = "/v1/login/complete"
ROUTE_SESSION_REVOKE = "/v1/session/revoke"
ROUTE_BARS_DAILY = "/v1/bars/daily"
# The routes this plan builds. 61-08 adds budget, quotes, preview-order and
# security-master and extends this set.
ROUTES = frozenset(
    {
        ROUTE_HEALTH,
        ROUTE_LOGIN_ARM,
        ROUTE_LOGIN_COMPLETE,
        ROUTE_SESSION_REVOKE,
        ROUTE_BARS_DAILY,
    }
)

# Host header values the tunnel can legitimately deliver (C1).
HOST_RE = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\])(:[0-9]{1,5})?$", re.IGNORECASE)

# ---------------------------------------------------------------- errors
# code -> (HTTP status, default retryable)

ERRORS: Mapping[str, tuple[int, bool]] = {
    "UNAUTHORIZED": (401, False),
    "NO_SESSION": (503, False),
    "SESSION_EXPIRED": (503, False),
    "EGRESS_MISMATCH": (503, False),
    "BUDGET_UNAVAILABLE": (503, False),
    "BUDGET_EXHAUSTED": (429, True),
    "UPSTREAM_ERROR": (502, False),
    "UPSTREAM_TIMEOUT": (504, True),
    "VALIDATION": (422, False),
    "LOGIN_ATTEMPT_INVALID": (409, False),
    "IDENTITY_MISMATCH": (403, False),
    "FORBIDDEN_CLIENT": (403, False),
    "CONTRACT_MISMATCH": (400, False),
    "NOT_FOUND": (404, False),
    "METHOD_NOT_ALLOWED": (405, False),
    "INTERNAL": (500, False),
}

# Reason values defined by the C6 table.
RELAY_REASONS = frozenset(
    {
        "deadline",
        "invalid_session",
        "resource_not_available",
        "egress_mismatch",
        "egress_unavailable",
        "ip_mismatch",
        "counter_unreadable",
        "budget_minute",
        "budget_day",
        "security_master_hourly",
        "upstream_http",
        "upstream_shape",
        "no_data",
        "public_key_missing",
        "other",
        "upstream_timeout",
        "upstream_unreachable",
        "identity_mismatch",
        "segment_disabled",
    }
)
# Extra values only the event log uses (event vocabulary).
EVENT_ONLY_REASONS = frozenset(
    {
        "attempt_invalid",
        "validation",
        "forbidden_client",
        "contract_mismatch",
        "not_found",
        "method_not_allowed",
        "internal",
        "secrets_unavailable",
        "config_invalid",
        "unauthorized",
        "counter_unreadable",
        "bind_invalid",
    }
)

# Reason shown in the event log when an error has no C6 reason of its own.
EVENT_REASON_FOR_CODE: Mapping[str, str] = {
    "UNAUTHORIZED": "unauthorized",
    "LOGIN_ATTEMPT_INVALID": "attempt_invalid",
    "VALIDATION": "validation",
    "FORBIDDEN_CLIENT": "forbidden_client",
    "CONTRACT_MISMATCH": "contract_mismatch",
    "NOT_FOUND": "not_found",
    "METHOD_NOT_ALLOWED": "method_not_allowed",
    "INTERNAL": "internal",
}

# ------------------------------------------------------------- upstream

API_BARS_V2 = "breeze-historicalcharts-v2"
API_QUOTES_V1 = "breeze-quotes-v1"
API_PREVIEW_V1 = "breeze-preview-order-v1"
API_SECURITY_MASTER = "icici-security-master-zip"

LOGIN_URL_BASE = "https://api.icicidirect.com/apiuser/login?api_key="
DAILY_INTERVAL_SEMANTIC = "1day"  # what X-Growin-Upstream-Interval reports, whatever the wire value
BARS_EXCHANGE = "NSE"

# -------------------------------------------------------------- patterns

STOCK_CODE_RE = re.compile(r"^[A-Z0-9]{1,10}$")
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{16}$")

# From 61-SMOKE-BREEZE.json, run 2026-10-21, key s1_customer_details.code_length
# (n = 8), by the OD-14 rule: lower bound min(6, n), upper bound max(64, 2n),
# charset [A-Za-z0-9] (s1_customer_details.code_charset is digits, so the
# alphanumeric charset is the looser, safe superset).
API_SESSION_RE = re.compile(r"^[A-Za-z0-9]{6,64}$")

# From 61-SMOKE-BREEZE.json, run 2026-10-21, key s2_bars[id=S2a].boundary_first_included
# and .boundary_last_included. Both were true, so no widening: pad days are 0.
# s2_variant_passed was null, so S2a is the item to read.
BARS_FROM_PAD_DAYS = 0
BARS_TO_PAD_DAYS = 0

# From 61-SMOKE-BREEZE.json, run 2026-10-21, key s2_variant_passed (null) and
# s2_variant_knobs (null): no request knob applies, so BreezeClient.daily_bars_v2_raw
# keeps the request shape 61-01 shipped. A smoke that passed S2e or S2f would put
# its user_agent, encode_colons and exchange_param_name here.
BARS_REQUEST_KNOBS: Mapping[str, object] = {}

# ---------------------------------------------------------------- limits

ARM_WINDOW_S = 120
EGRESS_MAX_AGE_S = 300
MAX_RANGE_DAYS = 3000
ALLOWED_BARS_QUERY = ("stock_code", "from", "to", "exchange")
# C3: only the listed query names, on every route. A route with no parameters
# lists none, so any query string on it is a 422.
ALLOWED_QUERY_BY_ROUTE: Mapping[str, tuple[str, ...]] = {
    ROUTE_HEALTH: (),
    ROUTE_LOGIN_ARM: (),
    ROUTE_LOGIN_COMPLETE: (),
    ROUTE_SESSION_REVOKE: (),
    ROUTE_BARS_DAILY: ALLOWED_BARS_QUERY,
}
ALLOWED_BODY_FIELDS = ("attempt_id", "apisession")

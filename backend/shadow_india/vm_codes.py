"""P-06 classification of the VM's typed answers (64-03 Task 3, T-64-36).

The VM answers every refusal with a typed body: a ``contract`` and a ``code``, and for the order routes the O6
name in ``reason``. This module reads that body first, looks the code up in an explicit table, and only then
requires the HTTP status to equal the status the table gives that code. The HTTP status alone never decides
anything: a corrupt audit chain is a 503, and so is a quote the VM could not read, and only the code tells them
apart. A code whose status is not the table's is ``code_status_mismatch`` (abort).

Three outcomes:

- ``expected``: ``kill_switch`` at 423. The only answer the shadow run wants. It still has to correlate with
  exactly one verified audit entry, which the runner checks;
- ``pause``: relay and session trouble, an unreadable kill label and answers that carry no typed body. A pause
  stops the invocation and only ``resume --reason`` clears it;
- ``abort``: every other order code, every corruption code on any status, an unknown code, a challenge or any
  2xx. Terminal.

The row set is total. ``test_shadow_india_runner`` iterates ``gateway_vm.orders.CODE_TABLE`` and the RELAY-API
error table and fails when a code has no row. A relay code the plan did not name (``UPSTREAM_ERROR``,
``UPSTREAM_TIMEOUT``, ``INTERNAL``, ``VALIDATION``, ``NOT_FOUND``, ``METHOD_NOT_ALLOWED``, ``LOGIN_ATTEMPT_INVALID``,
``IDENTITY_MISMATCH``) is a pause on an existing policy event, never a silent pass.

Pure: no import beyond the standard library, no clock, no network. Errors are read through their ``code``,
``reason`` and ``http_status`` attributes, so this file never imports the relay client.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

ORDERS_CONTRACT = "growin-orders/1"
RELAY_CONTRACT = "growin-relay/1"

EXPECTED = "expected"
PAUSE = "pause"
ABORT = "abort"

KILL_SWITCH = "kill_switch"
KILL_LABEL_UNREADABLE = "unreadable"
PORTFOLIO_CHECKS_NOT_RUN = "not_run_kill_first"

# Client-side codes (``brokers.breeze_gateway.errors``): the answer was not a typed body.
CLIENT_PROTOCOL = "RELAY_PROTOCOL_ERROR"
CLIENT_CONTRACT = "RELAY_CONTRACT_MISMATCH"
CLIENT_HASH = "RELAY_HASH_MISMATCH"
CLIENT_UNAVAILABLE = "RELAY_UNAVAILABLE"
REASON_UNKNOWN_CODE = "unknown_code"


@dataclass(frozen=True)
class Row:
    """One code: the status it must arrive with, what it means, and the policy event it raises."""

    status: int
    kind: str
    event: str | None
    category: str | None = None  # the ``code`` field an order name arrives under


@dataclass(frozen=True)
class Classification:
    kind: str
    event: str | None  # the ``policy.POLICY`` key for a pause or abort; None when expected
    code: str  # the O6 name or relay code the answer carried, or a closed word when it carried none
    status: int | None
    detail: str  # a closed word naming why, never a value


@dataclass(frozen=True)
class VmAnswer:
    """What a response said. ``None`` means the field was absent or not parsable."""

    contract: str | None
    code: str | None
    reason: str | None
    status: int | None


def _order(status: int, category: str, kind: str, event: str | None) -> Row:
    return Row(status, kind, event, category)


_REPLAY = (409, "REPLAY")
_LIMIT = (409, "LIMIT_REJECTED")
_BLOCKED = (423, "ORDERS_BLOCKED")
_UNAVAILABLE = (503, "ORDERS_UNAVAILABLE")

# The abort events a corruption code raises. They hold on any HTTP status.
CORRUPTION_EVENTS: Mapping[str, str] = {
    "audit_broken": "vm_audit_broken",
    "state_unreadable": "vm_state_unreadable",
    "state_unwritable": "vm_state_unwritable",
    "config_invalid": "vm_config_invalid",
    "clock_inconsistent": "vm_clock_inconsistent",
}

# O6 names (``gateway_vm.orders.CODE_TABLE``): status and category are the VM's own, copied here so a change on
# either side is a failing test, not a silent drift.
ORDER_ROWS: Mapping[str, Row] = {
    KILL_SWITCH: _order(423, "ORDERS_BLOCKED", EXPECTED, None),
    **{
        name: _order(status, category, ABORT, "vm_order_code")
        for name, (status, category) in {
            "challenge_unknown": _REPLAY,
            "challenge_expired": _REPLAY,
            "intent_consumed": _REPLAY,
            "challenge_outstanding": _REPLAY,
            "signature_invalid": (403, "SIGNATURE_INVALID"),
            "challenge_capacity": (429, "CHALLENGE_CAPACITY"),
            "capital_cap": _LIMIT,
            "per_position_cap": _LIMIT,
            "collar": _LIMIT,
            "circuit_band": _LIMIT,
            "off_tick": _LIMIT,
            "limits_hash_mismatch": _LIMIT,
            "key_mismatch": _LIMIT,
            "isin_mismatch": _LIMIT,
            "instrument_unsupported": _LIMIT,
            "sell_exceeds_holding": _LIMIT,
            "halt_latch": _BLOCKED,
            "pilot_ended": _BLOCKED,
            "stop_open": _BLOCKED,
            "mac_halt": _BLOCKED,
            "account_mismatch": _BLOCKED,
            "session_closed": _BLOCKED,
            "live_disabled": _BLOCKED,
            "quote_unavailable": _UNAVAILABLE,
            "tick_reference_unavailable": _UNAVAILABLE,
            "account_read_failed": _UNAVAILABLE,
        }.items()
    },
    **{
        name: _order(503, "ORDERS_UNAVAILABLE", ABORT, event) for name, event in CORRUPTION_EVENTS.items()
    },
}

# A 422 INTENT_INVALID carries a parse reason that is not an O6 name. It is a growin-orders/1 code like the rest.
INTENT_INVALID = Row(422, ABORT, "vm_order_code", "INTENT_INVALID")

ORDER_CATEGORIES: frozenset[str] = frozenset(
    {row.category for row in ORDER_ROWS.values() if row.category} | {INTENT_INVALID.category}
)

_PAUSE_ALL = PAUSE


def _relay(status: int, event: str) -> Row:
    return Row(status, PAUSE, event)


# growin-relay/1 error table (RELAY-API C6). Pauses only: none of these can be a corrupt audit or state.
RELAY_ROWS: Mapping[str, Row] = {
    "UNAUTHORIZED": _relay(401, "relay_unauthorized"),
    "NO_SESSION": _relay(503, "relay_no_session"),
    "SESSION_EXPIRED": _relay(503, "relay_session_expired"),
    "EGRESS_MISMATCH": _relay(503, "relay_egress_mismatch"),
    "BUDGET_UNAVAILABLE": _relay(503, "relay_budget_unavailable"),
    "BUDGET_EXHAUSTED": _relay(429, "relay_budget_exhausted"),
    "UPSTREAM_ERROR": _relay(502, "untyped_response"),
    "UPSTREAM_TIMEOUT": _relay(504, "untyped_response"),
    "VALIDATION": _relay(422, "relay_contract_mismatch"),
    "LOGIN_ATTEMPT_INVALID": _relay(409, "relay_unauthorized"),
    "IDENTITY_MISMATCH": _relay(403, "relay_unauthorized"),
    "FORBIDDEN_CLIENT": _relay(403, "relay_forbidden_client"),
    "CONTRACT_MISMATCH": _relay(400, "relay_contract_mismatch"),
    "NOT_FOUND": _relay(404, "relay_contract_mismatch"),
    "METHOD_NOT_ALLOWED": _relay(405, "relay_contract_mismatch"),
    "INTERNAL": _relay(500, "untyped_response"),
}


def _is_success(status: int | None) -> bool:
    return status is not None and 200 <= status < 300


def _abort(event: str, code: str, status: int | None, detail: str) -> Classification:
    return Classification(ABORT, event, code, status, detail)


def _pause(event: str, code: str, status: int | None, detail: str) -> Classification:
    return Classification(PAUSE, event, code, status, detail)


def classify(answer: VmAnswer) -> Classification:
    """Classify one typed answer: the code first, then the status the code requires.

    ``answer.reason`` is the O6 name when ``answer.code`` is an order category. Corruption codes abort on any
    status; every other code aborts with ``code_status_mismatch`` when the status is not the table's.
    """

    status = answer.status
    if _is_success(status):
        # A mint that returned a challenge, or any 2xx: the kill-first order did not hold. Never read further.
        return _abort("vm_challenge_or_success", answer.code or "success", status, "success_or_challenge")
    code = answer.code
    contract = answer.contract
    if contract not in (ORDERS_CONTRACT, RELAY_CONTRACT) or not isinstance(code, str):
        return _pause("untyped_response", "untyped", status, "no_typed_body")
    row: Row | None
    name = code
    if code in ORDER_CATEGORIES:
        reason = answer.reason
        row = ORDER_ROWS.get(reason) if isinstance(reason, str) else None
        if row is None:
            if code == INTENT_INVALID.category:
                row, name = INTENT_INVALID, code
            else:
                return _abort("vm_unknown_code", code, status, "order_reason_not_in_table")
        else:
            name = reason  # type: ignore[assignment]
            if row.category != code:
                return _abort("code_status_mismatch", name, status, "category_mismatch")
    else:
        row = RELAY_ROWS.get(code)
        if row is None:
            return _abort("vm_unknown_code", code, status, "code_not_in_table")
    if row.kind == ABORT and row.event in CORRUPTION_EVENTS.values():
        # Corruption stays abort-class whatever the status says, including a 503 and including while a pause is open.
        return _abort(row.event, name, status, "corruption_code")  # type: ignore[arg-type]
    if status != row.status:
        return _abort("code_status_mismatch", name, status, "status_not_the_codes_status")
    if row.kind == EXPECTED:
        return Classification(EXPECTED, None, name, status, "expected")
    if row.kind == ABORT:
        return _abort(row.event, name, status, "order_code")  # type: ignore[arg-type]
    return _pause(row.event, name, status, "relay_code")  # type: ignore[arg-type]


def classify_body(status: int | None, body: bytes | str | None) -> Classification:
    """Classify a raw response body: parse ``contract`` and ``error.code``, then ``classify``.

    A body that is not JSON, not an object, or has no ``error`` object carries no typed answer.
    """

    answer = VmAnswer(None, None, None, status)
    if body is not None:
        try:
            data: Any = json.loads(body)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            error = data.get("error")
            contract = data.get("contract")
            if isinstance(error, dict) and isinstance(contract, str):
                code = error.get("code")
                reason = error.get("reason")
                answer = VmAnswer(
                    contract,
                    code if isinstance(code, str) else None,
                    reason if isinstance(reason, str) else None,
                    status,
                )
    return classify(answer)


def classify_error(error: BaseException) -> Classification:
    """Classify a relay client error (``RelayError``): its typed code, reason and status, or its client-side kind."""

    code = getattr(error, "code", None)
    reason = getattr(error, "reason", None)
    status = getattr(error, "http_status", None)
    if code == CLIENT_PROTOCOL:
        if reason == REASON_UNKNOWN_CODE:
            return _abort("vm_unknown_code", "unknown", status, "code_not_in_client_table")
        if status is None or _is_success(status):
            # The client reads the status first, so a protocol error with no error status is a 2xx that did not parse.
            return _abort("vm_challenge_or_success", "success", status, "success_or_challenge")
        return _pause("untyped_response", "untyped", status, "no_typed_body")
    if code == CLIENT_CONTRACT:
        return _pause("relay_contract_mismatch", "contract_header", status, "contract_header")
    if code in (CLIENT_HASH, CLIENT_UNAVAILABLE):
        return _pause("untyped_response", "transport", status, "transport")
    if isinstance(code, str):
        return classify(VmAnswer(ORDERS_CONTRACT, code, reason if isinstance(reason, str) else None, status))
    return _pause("untyped_response", "untyped", status, "no_typed_body")


__all__ = [
    "ABORT",
    "CORRUPTION_EVENTS",
    "EXPECTED",
    "INTENT_INVALID",
    "KILL_LABEL_UNREADABLE",
    "KILL_SWITCH",
    "ORDERS_CONTRACT",
    "ORDER_CATEGORIES",
    "ORDER_ROWS",
    "PAUSE",
    "PORTFOLIO_CHECKS_NOT_RUN",
    "RELAY_CONTRACT",
    "RELAY_ROWS",
    "Classification",
    "Row",
    "VmAnswer",
    "classify",
    "classify_body",
    "classify_error",
]

"""growin-orders/1: the VM half of the signed-order guard (Phase 63).

Pure modules over injected ports. Nothing here imports the backend package,
touches Breeze, or builds a broker request. The forward step is a constant
refusal (see pipeline.RefusalForward): in 63 a verified intent is recorded
and never sent anywhere.
"""

from __future__ import annotations

CONTRACT = "growin-orders/1"
ORDER_PURPOSE = "growin.relay.order"
SIGNED_VERSION = 1
CHALLENGE_TTL_SECONDS = 60
MAX_OUTSTANDING_CHALLENGES = 4

# O6: code -> (http status, error category). One table, so every layer maps a
# reason code to the same wire answer.
_REPLAY = (409, "REPLAY")
_LIMIT = (409, "LIMIT_REJECTED")
_BLOCKED = (423, "ORDERS_BLOCKED")
_UNAVAILABLE = (503, "ORDERS_UNAVAILABLE")

CODE_TABLE: dict[str, tuple[int, str]] = {
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
    "kill_switch": _BLOCKED,
    "halt_latch": _BLOCKED,
    "pilot_ended": _BLOCKED,
    "stop_open": _BLOCKED,
    "mac_halt": _BLOCKED,
    "account_mismatch": _BLOCKED,
    "session_closed": _BLOCKED,
    "live_disabled": _BLOCKED,
    "config_invalid": _UNAVAILABLE,
    "state_unreadable": _UNAVAILABLE,
    "state_unwritable": _UNAVAILABLE,
    "audit_broken": _UNAVAILABLE,
    "quote_unavailable": _UNAVAILABLE,
    "tick_reference_unavailable": _UNAVAILABLE,
    "account_read_failed": _UNAVAILABLE,
}


class OrderRefusal(Exception):
    """A refusal with its wire answer. Carries a code, never a value."""

    def __init__(self, status: int, error: str, code: str) -> None:
        self.status = status
        self.error = error
        self.code = code
        super().__init__(f"{status} {error} {code}")


def refusal(code: str) -> OrderRefusal:
    """Build the OrderRefusal for an O6 code. An unknown code is a bug: fail closed."""
    status, error = CODE_TABLE.get(code, (503, "ORDERS_UNAVAILABLE"))
    if code not in CODE_TABLE:
        code = "config_invalid"
    return OrderRefusal(status, error, code)


def intent_invalid(code: str) -> OrderRefusal:
    return OrderRefusal(422, "INTENT_INVALID", code)

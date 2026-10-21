"""The relay's event log: one JSON object per line, a closed vocabulary.

This is the login audit trail (GATE-05). Every key, event and enum value is
checked on emit, so a request body, token, code, IP or Breeze Error text cannot
reach the sink by accident: an unknown key or value raises ValueError, which is
a programming error that tests catch.
"""

from __future__ import annotations

import json
import threading
from typing import TextIO

from .contract import EVENT_ONLY_REASONS, REQUEST_ID_RE, RELAY_REASONS, ROUTES
from .transport import Clock

ALLOWED_KEYS = frozenset(
    {
        "ts",
        "event",
        "state",
        "route",
        "result",
        "reason",
        "http",
        "breeze_status",
        "ms",
        "count",
        "request_id",
    }
)
EVENTS = frozenset(
    {
        "service_start",
        "egress_check",
        "login_armed",
        "login_completed",
        "session_expired",
        "session_revoked",
        "budget_exhausted",
        "request",
        "config_alarm",
    }
)
RESULTS = frozenset({"ok", "rejected", "error"})
REASONS = RELAY_REASONS | EVENT_ONLY_REASONS
STATES = frozenset({"none", "active", "expired", "suspended_egress"})
ROUTE_VALUES = ROUTES | frozenset(
    {"/v1/budget", "/v1/quotes", "/v1/preview-order", "/v1/security-master", "other"}
)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _non_negative_int(value: object) -> bool:
    return _is_int(value) and value >= 0  # type: ignore[operator]


def _state_value(value: object) -> str | None:
    """Accept a SessionState (a str Enum) or its plain value."""
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else None


_VALIDATORS = {
    "event": lambda v: isinstance(v, str) and v in EVENTS,
    "state": lambda v: _state_value(v) in STATES,
    "route": lambda v: isinstance(v, str) and v in ROUTE_VALUES,
    "result": lambda v: isinstance(v, str) and v in RESULTS,
    "reason": lambda v: v is None or (isinstance(v, str) and v in REASONS),
    "http": _non_negative_int,
    "breeze_status": lambda v: v is None or _is_int(v),
    "ms": _non_negative_int,
    "count": _non_negative_int,
    "request_id": lambda v: isinstance(v, str) and REQUEST_ID_RE.fullmatch(v) is not None,
}


class EventLog:
    def __init__(self, sink: TextIO, clock: Clock) -> None:
        self._sink = sink
        self._clock = clock
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: object) -> None:
        # The error text names the key, never the value.
        if not _VALIDATORS["event"](event):
            raise ValueError("event log: unknown event")
        record: dict[str, object] = {"event": event}
        for key, value in fields.items():
            if key not in _VALIDATORS:
                raise ValueError(f"event log: key not allowed: {key if key in ALLOWED_KEYS else '?'}")
            if not _VALIDATORS[key](value):
                raise ValueError(f"event log: bad value for {key}")
            record[key] = _state_value(value) if key == "state" else value
        ts = self._clock.now_utc().isoformat(timespec="milliseconds").replace("+00:00", "Z")
        record["ts"] = ts
        line = json.dumps(record, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
        with self._lock:
            self._sink.write(line + "\n")
            flush = getattr(self._sink, "flush", None)
            if flush is not None:
                flush()

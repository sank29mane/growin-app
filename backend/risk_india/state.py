"""Durable Mac latch state for the India pilot (63-04, P-18, D-05, D-06).

``drawdown.RiskState`` is a frozen value; this module keeps one of them in
``india-risk-state.json`` beside the India ledger and is the only place that reads or
writes that file. Unlike ``rules``, ``drawdown`` and ``exits`` it does file I/O, so it is
scanned by its own, narrower source test (no gateway import, no network, no float, no
clock or environment read) instead of the pure-module test.

File shape: ``{"schema_version": 1, "workspace": "india", "state": {...}, "sha256": H}``
where ``H`` is the sha256 of the canonical JSON of the first three keys. The file is
written with an fsync and an atomic rename, mode 0600.

Fail-closed reading, each a ``StateUnreadable`` (the O6 name ``state_unreadable``):

- the file is absent while the India ledger holds fills (a deleted file must never reset
  a halt, an end or a stop);
- it is not a regular 0600 file owned by this user, is too large, is not strict JSON, has
  unknown or missing keys, a float, a duplicate key, a bad type, or an unsupported version;
- it names another workspace;
- its self-hash does not match its content.

Absent with no fills is the pilot start: the state is created with the peak at the
capital cap (P-07) and written before it is returned.

The Mac keys positions, stops and open exits by the ledger's execution ticker (for
example ``NSE:CASH:RELIANCE``). ``drawdown`` calls that field ``isin``; the Mac state is
never sent to the VM, so the two key spaces never meet.

Every mutating call takes an in-process lock and an ``flock`` on a sidecar lock file, so
the server and the reset CLI cannot interleave a read-modify-write.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Sequence

from .drawdown import (
    OpenExit,
    ResetRecord,
    RiskState,
    SessionResult,
    StopLatch,
    apply_exit_fill as _apply_exit_fill,
    evaluate_session as _evaluate_session,
    initial_state,
    reset as _reset,
)
from .exits import Position
from .rules import Limits, RiskConfigError

STATE_FILE_NAME = "india-risk-state.json"
STATE_SCHEMA_VERSION = 1
STATE_WORKSPACE = "india"
MAX_STATE_BYTES = 1_048_576

_DECIMAL_RE = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class StateUnreadable(RuntimeError):
    """The latch file cannot be trusted. India BUY admission is denied ``state_unreadable``."""

    code = "state_unreadable"

    def __init__(self, reason: str) -> None:
        super().__init__(f"state_unreadable: {reason}")
        self.reason = reason


class StateUnwritable(RuntimeError):
    """The latch file could not be written. Nothing is applied that was not persisted."""

    code = "state_unwritable"


def state_path_for(ledger_path: str | os.PathLike[str]) -> Path:
    """The latch file beside the India ledger."""
    return Path(ledger_path).with_name(STATE_FILE_NAME)


def limits_from_config(config: Any) -> Limits:
    """The Mac's ``Limits`` from a ``WorkspaceConfig`` loaded with ``require_india_execution``.

    The nine P-06 keys come from ``limits.json`` (seven) and ``execution.json`` (the
    collar); every value is the operator's own decimal text, so ``limits_sha256`` is the
    hash of the file's values. Raises ``rules.RiskConfigError`` when either file is absent
    or a value is out of range.
    """
    limits_file = getattr(config, "limits", None)
    execution = getattr(config, "india_execution", None)
    if limits_file is None or execution is None:
        raise RiskConfigError("India limits and execution config are required")
    return Limits.from_fields(
        {
            "schema_version": 1,
            "workspace": "india",
            "currency": "INR",
            "capital_cap": _text(limits_file.capital_cap),
            "per_position_cap": _text(limits_file.per_position_cap),
            "drawdown_halt": _text(limits_file.drawdown_halt),
            "drawdown_flatten": _text(limits_file.drawdown_flatten),
            "position_stop": _text(limits_file.position_stop),
            "fat_finger_collar": _text(execution.fat_finger_collar),
        }
    )


# ------------------------------------------------------------------ encoding


def _text(value: Decimal) -> str:
    return format(value, "f")


def _frozen(mapping: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(mapping))


def encode_state(state: RiskState) -> dict[str, Any]:
    """The JSON-safe form of a state. Decimals and dates are strings; no floats."""
    return {
        "peak": _text(state.peak),
        "last_session": None if state.last_session is None else state.last_session.isoformat(),
        "drawdown": _text(state.drawdown),
        "halt": state.halt,
        "ended": state.ended,
        "stops": {
            key: {"session": latch.session.isoformat(), "quantity": latch.quantity}
            for key, latch in sorted(state.stops.items())
        },
        "open_exits": {
            key: {
                "reason": exit_.reason,
                "quantity": exit_.quantity,
                "stock_code": exit_.stock_code,
                "since": exit_.since.isoformat(),
            }
            for key, exit_ in sorted(state.open_exits.items())
        },
        "resets": [
            {
                "latch": record.latch,
                "actor": record.actor,
                "isin": record.isin,
                "rebase_halt_anchor": record.rebase_halt_anchor,
            }
            for record in state.resets
        ],
        "last_equity": None if state.last_equity is None else _text(state.last_equity),
        "halt_anchor": None if state.halt_anchor is None else _text(state.halt_anchor),
    }


def _digest(body: Mapping[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _body(state_json: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "workspace": STATE_WORKSPACE,
        "state": state_json,
    }


def serialize_state(state: RiskState) -> bytes:
    body = _body(encode_state(state))
    document = {**body, "sha256": _digest(body)}
    return (json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode(
        "ascii"
    )


# ------------------------------------------------------------------ decoding


def _bad(reason: str) -> StateUnreadable:
    return StateUnreadable(reason)


def _keys(obj: Any, expected: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(obj, dict) or set(obj) != expected:
        raise _bad(f"{name}_shape")
    return obj


def _decimal(value: Any, name: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _DECIMAL_RE.fullmatch(value) is None:
        raise _bad(f"{name}_type")
    return Decimal(value)


def _date(value: Any, name: str, *, optional: bool = False) -> date | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        raise _bad(f"{name}_type")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _bad(f"{name}_type") from None


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _bad(f"{name}_type")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise _bad(f"{name}_type")
    return value


def _string(value: Any, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > 128:
        raise _bad(f"{name}_type")
    return value


def decode_state(raw: Any) -> RiskState:
    """Rebuild a ``RiskState`` from its encoded form, strictly. Anything else is unreadable."""
    raw = _keys(
        raw,
        {
            "peak", "last_session", "drawdown", "halt", "ended", "stops", "open_exits",
            "resets", "last_equity", "halt_anchor",
        },
        "state",
    )
    stops: dict[str, StopLatch] = {}
    stops_raw = raw["stops"]
    if not isinstance(stops_raw, dict):
        raise _bad("stops_shape")
    for key, value in stops_raw.items():
        entry = _keys(value, {"session", "quantity"}, "stop")
        stops[_string(key, "stop_key")] = StopLatch(
            _date(entry["session"], "stop_session"), _int(entry["quantity"], "stop_quantity")
        )
    exits: dict[str, OpenExit] = {}
    exits_raw = raw["open_exits"]
    if not isinstance(exits_raw, dict):
        raise _bad("open_exits_shape")
    for key, value in exits_raw.items():
        entry = _keys(value, {"reason", "quantity", "stock_code", "since"}, "open_exit")
        reason = _string(entry["reason"], "exit_reason")
        if reason not in ("halve", "stop", "flatten"):
            raise _bad("exit_reason_type")
        exits[_string(key, "exit_key")] = OpenExit(
            reason,
            _int(entry["quantity"], "exit_quantity"),
            _string(entry["stock_code"], "exit_stock_code"),
            _date(entry["since"], "exit_since"),
        )
    resets_raw = raw["resets"]
    if not isinstance(resets_raw, list):
        raise _bad("resets_shape")
    resets: list[ResetRecord] = []
    for value in resets_raw:
        entry = _keys(value, {"latch", "actor", "isin", "rebase_halt_anchor"}, "reset")
        resets.append(
            ResetRecord(
                _string(entry["latch"], "reset_latch"),
                _string(entry["actor"], "reset_actor"),
                _string(entry["isin"], "reset_isin", optional=True),
                _bool(entry["rebase_halt_anchor"], "reset_rebase"),
            )
        )
    peak = _decimal(raw["peak"], "peak")
    if not peak > 0:
        raise _bad("peak_range")
    return RiskState(
        peak=peak,
        last_session=_date(raw["last_session"], "last_session", optional=True),
        drawdown=_decimal(raw["drawdown"], "drawdown"),
        halt=_bool(raw["halt"], "halt"),
        ended=_bool(raw["ended"], "ended"),
        stops=_frozen(stops),
        open_exits=_frozen(exits),
        resets=tuple(resets),
        last_equity=_decimal(raw["last_equity"], "last_equity", optional=True),
        halt_anchor=_decimal(raw["halt_anchor"], "halt_anchor", optional=True),
    )


def _refuse_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _refuse_number(_text: str) -> Any:
    raise ValueError("a number that is not an integer")


def parse_state_bytes(data: bytes) -> RiskState:
    """Parse and verify one file's bytes. Raises ``StateUnreadable``; never returns a guess."""
    try:
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_refuse_duplicates,
            parse_float=_refuse_number,
            parse_constant=_refuse_number,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _bad("corrupt") from None
    document = _keys(document, {"schema_version", "workspace", "state", "sha256"}, "file")
    version = document["schema_version"]
    if isinstance(version, bool) or version != STATE_SCHEMA_VERSION:
        raise _bad("unsupported_version")
    if document["workspace"] != STATE_WORKSPACE:
        raise _bad("workspace_mismatch")
    claimed = document["sha256"]
    if not isinstance(claimed, str):
        raise _bad("hash_type")
    expected = _digest(_body(document["state"]))
    if not hmac.compare_digest(claimed.encode("ascii", "replace"), expected.encode("ascii")):
        raise _bad("hash_mismatch")
    return decode_state(document["state"])


# ------------------------------------------------------------------ file I/O


def read_state(path: str | os.PathLike[str]) -> RiskState | None:
    """The stored state, ``None`` when the file does not exist, else ``StateUnreadable``."""
    target = Path(path)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError:
        raise _bad("stat_failed") from None
    if stat.S_ISLNK(info.st_mode):
        raise _bad("symlink")
    if not stat.S_ISREG(info.st_mode):
        raise _bad("not_a_regular_file")
    if info.st_uid != os.getuid():
        raise _bad("owner_mismatch")
    if info.st_mode & 0o077:
        raise _bad("permissions_too_open")
    if info.st_size > MAX_STATE_BYTES:
        raise _bad("too_large")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError:
        raise _bad("open_failed") from None
    try:
        data = b""
        while len(data) <= MAX_STATE_BYTES:
            chunk = os.read(fd, MAX_STATE_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        raise _bad("read_failed") from None
    finally:
        os.close(fd)
    if len(data) > MAX_STATE_BYTES:
        raise _bad("too_large")
    return parse_state_bytes(data)


def write_state(path: str | os.PathLike[str], state: RiskState) -> None:
    """Write the state with an fsync and an atomic rename, mode 0600."""
    target = Path(path)
    payload = serialize_state(state)
    tmp = target.with_name(target.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise StateUnwritable("the latch file could not be written") from exc


# --------------------------------------------------------------------- store


class LatchStore:
    """One India latch file and the operations on it.

    ``has_fills`` answers "does the India ledger hold any fill?". It is asked only when the
    file is absent, because that is the one case where the answer decides between a pilot
    start and a deleted file.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        limits: Limits,
        *,
        has_fills: Callable[[], bool],
    ) -> None:
        self.path = Path(path)
        self.limits = limits
        self._has_fills = has_fills
        self._lock = threading.RLock()

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        with self._lock:
            lock_path = self.path.with_name(self.path.name + ".lock")
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                raise StateUnwritable("the latch lock file could not be opened") from exc
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)  # closing the descriptor releases the flock

    def _read_or_start(self) -> RiskState:
        state = read_state(self.path)
        if state is not None:
            return state
        try:
            fills = bool(self._has_fills())
        except Exception:  # noqa: BLE001 - an unreadable ledger cannot vouch for a pilot start
            raise _bad("fills_unknown") from None
        if fills:
            raise _bad("absent_with_fills")
        started = initial_state(self.limits)
        write_state(self.path, started)
        return started

    def load(self) -> RiskState:
        """The current state. Creates it at the pilot start; otherwise fails closed."""
        state = read_state(self.path)
        if state is not None:
            return state
        with self._exclusive():
            return self._read_or_start()

    def evaluate_session(
        self,
        marks: Mapping[str, Decimal],
        session_date: date,
        *,
        cash: Decimal,
        positions: Sequence[Position],
        vol_stops: Mapping[str, Decimal] | None = None,
    ) -> SessionResult:
        """Apply one session close (``drawdown.evaluate_session``) and persist the result.

        ``marks`` are the closes by position key. A held position with no mark raises
        ``drawdown.MarkMissing`` and nothing is written.
        """
        with self._exclusive():
            current = self._read_or_start()
            result = _evaluate_session(
                current,
                self.limits,
                session_date,
                cash=cash,
                positions=positions,
                closes=marks,
                vol_stops=vol_stops,
            )
            if result.state != current:
                write_state(self.path, result.state)
            return result

    def apply_exit_fill(self, key: str, *, sold_quantity: int, remaining_quantity: int) -> RiskState:
        """Record exit fill evidence. A stop latch clears only when nothing is left held."""
        with self._exclusive():
            current = self._read_or_start()
            updated = _apply_exit_fill(
                current, key, sold_quantity=sold_quantity, remaining_quantity=remaining_quantity
            )
            if updated != current:
                write_state(self.path, updated)
            return updated

    def reset(
        self,
        latch: str,
        actor: str,
        *,
        key: str | None = None,
        rebase_halt_anchor: bool = False,
        audit: Callable[[RiskState, RiskState], None] | None = None,
    ) -> RiskState:
        """Admin release of ``halt`` or a ``stop`` (D-05). ``ended`` is refused by ``drawdown``.

        ``audit`` is called with (before, after) once the reset is allowed and BEFORE the file
        is written. If it raises, nothing is written: a reset that cannot be audited does not
        happen.
        """
        with self._exclusive():
            current = self._read_or_start()
            updated = _reset(
                current,
                latch,
                actor,
                isin=key,
                limits=self.limits,
                rebase_halt_anchor=rebase_halt_anchor,
            )
            if audit is not None:
                audit(current, updated)
            write_state(self.path, updated)
            return updated

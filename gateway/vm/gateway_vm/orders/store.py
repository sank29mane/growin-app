"""Durable order state under the systemd StateDirectory (D-02, T-63-05).

One file, state.json: the derived risk state, the fill ledger, consumed intent
ids and sent-alert keys, plus the audit anchor (expected audit entry count and
head hash, see audit.py), wrapped with its own sha256. Numbers and identifiers
only; no market data. Writes are write-temp, fsync, rename, fsync-directory,
mode 0600. Any read problem (missing while an audit exists, corrupt JSON, a
self-hash mismatch, loose permissions, a symlink) raises state_unreadable, and
every order is refused. A write problem raises state_unwritable.

An absent state file is initialised only when the audit log is also empty (a
first start). Absent state beside a non-empty audit means someone removed the
state to clear latches, and is refused.

A flock on <dir>/.order.lock serialises the service and the admin CLI. The lock
is re-entrant within a process, so the pipeline can hold it across a whole
mint or authorize while the store takes it again for each read-modify-write.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path
from typing import Any

from . import refusal
from .audit import GENESIS
from .limits import Limits
from .risk import OrderState, StateInvalid, initial_state

STATE_FILE = "state.json"
AUDIT_FILE = "audit.jsonl"
LOCK_FILE = ".order.lock"
_MAX_STATE_BYTES = 8 * 1024 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}")
Anchor = tuple[int, str]
_NO_ANCHOR: Anchor = (0, GENESIS)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


class _DirLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._rlock = threading.RLock()
        self._fd: int | None = None
        self._depth = 0

    def __enter__(self) -> "_DirLock":
        self._rlock.acquire()
        try:
            if self._depth == 0:
                fd = -1
                try:
                    fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except OSError:
                    if fd >= 0:
                        os.close(fd)
                    raise refusal("state_unwritable") from None
                self._fd = fd
            self._depth += 1
        except BaseException:
            self._rlock.release()
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        self._rlock.release()


class StateStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / STATE_FILE
        self.audit_path = self.directory / AUDIT_FILE
        self._lock = _DirLock(self.directory / LOCK_FILE)

    def lock(self) -> _DirLock:
        return self._lock

    # -- read ------------------------------------------------------------

    def exists(self) -> bool:
        return os.path.lexists(self.path)

    def load(self) -> OrderState:
        return self._read()[0]

    def audit_anchor(self) -> Anchor | None:
        """The durable audit anchor; None if no state exists yet (a first start)."""
        with self._lock:
            if not self.exists():
                return None
            return self._read()[1]

    def _read(self) -> tuple[OrderState, Anchor]:
        try:
            st = os.lstat(self.path)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("not a regular file")
            if st.st_mode & 0o077:
                raise ValueError("state file permissions are too open")
            if st.st_size > _MAX_STATE_BYTES:
                raise ValueError("state file is too large")
            wrapper = _strict_loads(self.path.read_text(encoding="ascii"))
            if not isinstance(wrapper, dict) or set(wrapper) != {
                "state",
                "audit_anchor",
                "state_sha256",
            }:
                raise ValueError("state wrapper keys")
            if _wrapper_hash(wrapper["state"], wrapper["audit_anchor"]) != wrapper["state_sha256"]:
                raise ValueError("state self-hash mismatch")
            return OrderState.from_json(wrapper["state"]), _parse_anchor(wrapper["audit_anchor"])
        except (OSError, ValueError, StateInvalid):
            raise refusal("state_unreadable") from None

    def load_or_init(self, limits: Limits, *, audit_has_entries: bool) -> OrderState:
        with self._lock:
            if self.exists():
                return self.load()
            if audit_has_entries:
                raise refusal("state_unreadable")
            state = initial_state(limits)
            self.save(state)
            return state

    # -- write -----------------------------------------------------------

    def save(self, state: OrderState) -> None:
        """Persist the risk state. The audit anchor on disk is carried over
        unchanged: a caller holding an older copy of the state can never move
        the anchor back."""
        with self._lock:
            anchor = self._read()[1] if self.exists() else _NO_ANCHOR
            self._write(state, anchor)

    def set_audit_anchor(self, seq: int, head_sha256: str) -> None:
        """Atomically replace the anchor, keeping the state. Never moves backwards."""
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValueError("anchor seq")
        if _HEX64.fullmatch(head_sha256) is None or (seq == 0) != (head_sha256 == GENESIS):
            raise ValueError("anchor head")
        with self._lock:
            state, (current, _) = self._read()
            if seq < current:
                raise ValueError("the audit anchor never moves backwards")
            self._write(state, (seq, head_sha256))

    def _write(self, state: OrderState, anchor: Anchor) -> None:
        body = state.to_json()
        anchor_json = {"seq": anchor[0], "head_sha256": anchor[1]}
        wrapper = {
            "state": body,
            "audit_anchor": anchor_json,
            "state_sha256": _wrapper_hash(body, anchor_json),
        }
        data = _canonical(wrapper) + b"\n"
        tmp = self.directory / f"{STATE_FILE}.tmp-{os.getpid()}"
        try:
            with self._lock:
                fd = os.open(
                    tmp,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC,
                    0o600,
                )
                try:
                    if os.write(fd, data) != len(data):
                        raise OSError("short state write")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, self.path)
                dir_fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise refusal("state_unwritable") from None

    # -- intent ledger port ------------------------------------------------

    def is_consumed(self, intent_id: str) -> bool:
        return intent_id in self.load().consumed_intents

    def consume(self, intent_id: str) -> None:
        with self._lock:
            state = self.load()
            if intent_id not in state.consumed_intents:
                state.consumed_intents.append(intent_id)
                self.save(state)


def _wrapper_hash(body: Any, anchor: Any) -> str:
    return hashlib.sha256(_canonical({"state": body, "audit_anchor": anchor})).hexdigest()


def _parse_anchor(raw: Any) -> Anchor:
    if not isinstance(raw, dict) or set(raw) != {"seq", "head_sha256"}:
        raise ValueError("audit anchor keys")
    seq, head = raw["seq"], raw["head_sha256"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ValueError("audit anchor seq")
    if not isinstance(head, str) or _HEX64.fullmatch(head) is None or (seq == 0) != (head == GENESIS):
        raise ValueError("audit anchor head")
    return seq, head


def _strict_loads(text: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out

    def no_float(_: str) -> Any:
        raise ValueError("float")

    return json.loads(text, object_pairs_hook=pairs, parse_float=no_float, parse_constant=no_float)

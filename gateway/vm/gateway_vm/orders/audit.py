"""Append-only, hash-chained order audit (P-12, T-63-06, T-63-07).

One JSON object per line. Every entry carries the sha256 of the previous entry,
so a deleted, edited or reordered line breaks the chain. The chain is verified
in full before every append; a write failure raises AuditBroken and the order is
refused. The key set is the O7 allowlist: no quote values, tokens, account ids
or IPs can be written because no key exists to carry them.

A hash chain alone cannot see a deleted log (an empty chain verifies) or a
dropped tail (a valid prefix verifies). So production logs are anchored: the
expected entry count and head hash live in the durable order state (an
``AuditAnchor``), written after each entry is fsynced. The anchor is therefore
never ahead of the log. Verification demands:

- the log holds at least ``anchor.seq`` entries and entry ``anchor.seq`` hashes
  to ``anchor.head_sha256`` (a missing, truncated or rewritten log fails);
- the log holds at most ONE entry beyond the anchor: the crash window between
  the fsync of a line and the write of its anchor (that entry was never
  acknowledged to a caller);
- with an anchor, exactly one unterminated tail line is a torn write from a
  crash and is dropped (the next append truncates it); without an anchor any
  unterminated tail is a failure, since nothing proves it is a crash artifact.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from . import OrderRefusal

DECISIONS = frozenset(
    {"CHALLENGED", "REFUSED", "VERIFIED_NOT_FORWARDED", "HALTED", "RESET", "EVALUATED"}
)

# O7. Order matters only for readability: entries are hashed over sorted keys.
ENTRY_KEYS: tuple[str, ...] = (
    "seq",
    "prev_sha256",
    "entry_sha256",
    "at_utc",
    "route",
    "intent_id",
    "proposal_id",
    "intent_sha256",
    "key_id",
    "side",
    "stock_code",
    "isin",
    "quantity",
    "limit_price",
    "reason",
    "batch_id",
    "decision",
    "codes",
    "limits_sha256",
    "kill",
    "latches",
)
_CALLER_KEYS = frozenset(ENTRY_KEYS) - {"seq", "prev_sha256", "entry_sha256", "at_utc"}
GENESIS = "0" * 64

Clock = Callable[[], datetime]


class AuditBroken(Exception):
    """The chain does not verify, or an entry could not be written durably."""


class AuditAnchor(Protocol):
    """Durable (entry count, head hash) kept outside the audit file."""

    def audit_anchor(self) -> tuple[int, str] | None:
        """The anchor, or None when no order state exists yet. Raises if unreadable."""
        ...

    def set_audit_anchor(self, seq: int, head_sha256: str) -> None:
        """Atomically persist a new anchor. Never moves backwards."""
        ...


@dataclass(frozen=True)
class _Scan:
    entries: list[dict[str, Any]]
    good_bytes: int  # length of the verified, newline-terminated prefix
    torn: bool  # an unterminated tail follows the verified prefix


def _hash_entry(entry: Mapping[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "entry_sha256"}
    data = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Clock = _utc_now,
        anchor: AuditAnchor | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self.anchor = anchor

    # -- reading -----------------------------------------------------------

    def _scan(self) -> _Scan:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return _Scan([], 0, False)
        except OSError as exc:
            raise AuditBroken("audit log is unreadable") from exc
        torn = False
        if raw and not raw.endswith(b"\n"):
            if self.anchor is None:
                raise AuditBroken("audit log ends with a truncated line")
            raw = raw[: raw.rfind(b"\n") + 1]  # drop the one torn tail line
            torn = True
        entries: list[dict[str, Any]] = []
        prev = GENESIS
        for number, line in enumerate(raw.split(b"\n")[:-1], start=1):
            try:
                entry = json.loads(line.decode("ascii"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise AuditBroken(f"audit line {number} is not valid JSON") from exc
            if not isinstance(entry, dict) or set(entry) != set(ENTRY_KEYS):
                raise AuditBroken(f"audit line {number} has the wrong keys")
            if entry["seq"] != number or entry["prev_sha256"] != prev:
                raise AuditBroken(f"audit chain breaks at line {number}")
            if _hash_entry(entry) != entry["entry_sha256"]:
                raise AuditBroken(f"audit entry {number} hash mismatch")
            prev = entry["entry_sha256"]
            entries.append(entry)
        return _Scan(entries, len(raw), torn)

    def _check_anchor(self, scan: _Scan) -> tuple[int, str] | None:
        """Compare the log with its anchor. Returns the anchor (None: no anchor port)."""
        if self.anchor is None:
            return None
        try:
            anchor = self.anchor.audit_anchor()
        except OrderRefusal:
            raise  # unreadable order state keeps its own typed answer (state_unreadable)
        except Exception as exc:
            raise AuditBroken("audit anchor is unreadable") from exc
        entries = scan.entries
        if anchor is None:
            if entries or scan.torn:
                raise AuditBroken("audit entries exist but the order state has no anchor")
            return None
        seq, head = anchor
        if len(entries) < seq:
            raise AuditBroken("audit log is shorter than its anchor: deleted or truncated")
        if (GENESIS if seq == 0 else entries[seq - 1]["entry_sha256"]) != head:
            raise AuditBroken("audit log does not match its anchor")
        if len(entries) > seq + 1:
            raise AuditBroken("audit log is ahead of its anchor by more than one entry")
        return anchor

    def _read_entries(self) -> list[dict[str, Any]]:
        scan = self._scan()
        self._check_anchor(scan)
        return scan.entries

    def verify(self) -> tuple[int, str]:
        """Return (entry count, last entry sha256). Raises AuditBroken."""
        entries = self._read_entries()
        return (len(entries), entries[-1]["entry_sha256"] if entries else GENESIS)

    def has_torn_tail(self) -> bool:
        """True if a crash left one unterminated line after the verified prefix."""
        scan = self._scan()
        self._check_anchor(scan)
        return scan.torn

    def has_entries(self) -> bool:
        try:
            return self.path.stat().st_size > 0
        except FileNotFoundError:
            return False

    def entries_after(self, after_seq: int, limit: int = 200) -> list[dict[str, Any]]:
        entries = self._read_entries()
        return [e for e in entries if e["seq"] > after_seq][: max(0, min(limit, 200))]

    # -- writing -----------------------------------------------------------

    def append(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Verify the chain, then append one entry durably. Raises AuditBroken."""
        unknown = set(fields) - _CALLER_KEYS
        if unknown:
            raise ValueError(f"audit keys outside the O7 allowlist: {sorted(unknown)}")
        decision = fields.get("decision")
        if decision not in DECISIONS:
            raise ValueError("audit decision outside the O7 set")
        try:
            lock_fd = os.open(
                str(self.path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600
            )
        except OSError as exc:
            raise AuditBroken("audit lock is unavailable") from exc
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            scan = self._scan()
            anchor = self._check_anchor(scan)
            if self.anchor is not None and anchor is None:
                raise AuditBroken("the order state must exist before the audit can be written")
            entries = scan.entries
            if scan.torn:
                self._drop_torn_tail(scan.good_bytes)
            prev = entries[-1]["entry_sha256"] if entries else GENESIS
            entry: dict[str, Any] = {key: None for key in ENTRY_KEYS}
            entry.update(fields)
            entry["codes"] = list(fields.get("codes") or [])
            entry["latches"] = sorted(fields.get("latches") or [])
            entry["seq"] = len(entries) + 1
            entry["prev_sha256"] = prev
            entry["at_utc"] = (
                self._clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            entry["entry_sha256"] = _hash_entry(entry)
            line = (
                json.dumps(
                    entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                )
                + "\n"
            ).encode("ascii")
            created = not self.path.exists()
            try:
                fd = os.open(
                    self.path,
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC,
                    0o600,
                )
                try:
                    written = os.write(fd, line)
                    if written != len(line):
                        raise OSError("short audit write")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if created:
                    _fsync_dir(self.path.parent)
            except OSError as exc:
                raise AuditBroken("audit entry could not be written") from exc
            if self.anchor is not None:
                # After the fsync, so the anchor is never ahead of the log. If
                # this fails the entry exists but was never acknowledged; the
                # next call sees the log one ahead and tolerates exactly that.
                try:
                    self.anchor.set_audit_anchor(entry["seq"], entry["entry_sha256"])
                except OrderRefusal:
                    raise
                except Exception as exc:
                    raise AuditBroken("audit anchor could not be written") from exc
            return entry
        finally:
            os.close(lock_fd)


    def _drop_torn_tail(self, good_bytes: int) -> None:
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CLOEXEC)
            try:
                os.ftruncate(fd, good_bytes)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise AuditBroken("torn audit tail could not be removed") from exc


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

"""Append-only, hash-chained order audit (P-12, T-63-06, T-63-07).

One JSON object per line. Every entry carries the sha256 of the previous entry,
so a deleted, edited or reordered line breaks the chain. The chain is verified
in full before every append; a write failure raises AuditBroken and the order is
refused. The key set is the O7 allowlist: no quote values, tokens, account ids
or IPs can be written because no key exists to carry them.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

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


def _hash_entry(entry: Mapping[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "entry_sha256"}
    data = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog:
    def __init__(self, path: str | Path, *, clock: Clock = _utc_now) -> None:
        self.path = Path(path)
        self._clock = clock

    # -- reading -----------------------------------------------------------

    def _read_entries(self) -> list[dict[str, Any]]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise AuditBroken("audit log is unreadable") from exc
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise AuditBroken("audit log ends with a truncated line")
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
        return entries

    def verify(self) -> tuple[int, str]:
        """Return (entry count, last entry sha256). Raises AuditBroken."""
        entries = self._read_entries()
        return (len(entries), entries[-1]["entry_sha256"] if entries else GENESIS)

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
            entries = self._read_entries()
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
            return entry
        finally:
            os.close(lock_fd)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

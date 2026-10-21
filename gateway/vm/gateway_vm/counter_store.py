"""The relay's one file: the persisted Breeze day counter (OD-4, T-61-82).

This is the only gateway_vm module that opens a file for writing; the no-write
scan in test_gateway_relay_app.py names it as the one exception. The file holds
the IST date and the count, nothing else, in a private systemd StateDirectory.

Any fault (no directory, unreadable, symlink, wrong type, extra key, bad value,
unwritable) is CounterUnreadable. The caller turns that into a refusal of every
Breeze call instead of guessing a count. Nothing here logs.
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import date
from pathlib import Path
from typing import Protocol

COUNTER_FILE = "counter.json"
_KEYS = frozenset({"ist_date", "count"})
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_MAX_BYTES = 4096


class CounterUnreadable(Exception):
    """Absent state dir, unreadable, malformed or unwritable. No detail is kept."""


class CounterStore(Protocol):
    def load(self) -> tuple[date, int] | None: ...

    def save(self, ist_date: date, count: int) -> None: ...


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen[key] = value
    return seen


def _reject_constant(_text: str) -> object:
    raise ValueError("bad constant")


def _parse(raw: bytes) -> tuple[date, int]:
    try:
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant
        )
    except (ValueError, RecursionError):
        raise CounterUnreadable from None
    if not isinstance(data, dict) or set(data) != _KEYS:
        raise CounterUnreadable
    raw_date, count = data["ist_date"], data["count"]
    if not isinstance(raw_date, str) or not _DATE_RE.fullmatch(raw_date):
        raise CounterUnreadable
    if type(count) is not int or count < 0:
        raise CounterUnreadable
    try:
        return date.fromisoformat(raw_date), count
    except ValueError:
        raise CounterUnreadable from None


class FileCounterStore:
    """counter.json, mode 0600, {"ist_date": "YYYY-MM-DD", "count": int}.

    state_dir None means "unavailable": every call raises CounterUnreadable.
    The directory itself may be a symlink (systemd's DynamicUser state
    directories are); counter.json may not.
    """

    def __init__(self, state_dir: Path | None) -> None:
        self._dir = Path(state_dir) if state_dir is not None else None

    def _directory(self) -> Path:
        if self._dir is None:
            raise CounterUnreadable
        try:
            if not stat.S_ISDIR(os.stat(self._dir).st_mode):
                raise CounterUnreadable
        except OSError:
            raise CounterUnreadable from None
        return self._dir

    def load(self) -> tuple[date, int] | None:
        path = self._directory() / COUNTER_FILE
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError:
            raise CounterUnreadable from None
        if not stat.S_ISREG(info.st_mode):  # a symlink, a directory or a device
            raise CounterUnreadable
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise CounterUnreadable from None
        try:
            raw = os.read(fd, _MAX_BYTES + 1)
        except OSError:
            raise CounterUnreadable from None
        finally:
            os.close(fd)
        if len(raw) > _MAX_BYTES:
            raise CounterUnreadable
        return _parse(raw)

    def save(self, ist_date: date, count: int) -> None:
        directory = self._directory()
        payload = json.dumps(
            {"ist_date": ist_date.isoformat(), "count": count}, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        tmp = directory / f".{COUNTER_FILE}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
        fd = -1
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp, directory / COUNTER_FILE)
        except OSError:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp)
            except OSError:
                pass  # a leftover temp file is harmless; the refusal below is what matters
            raise CounterUnreadable from None
        try:  # make the rename itself durable; best effort, the data is already replaced
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

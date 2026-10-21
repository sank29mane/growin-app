"""Relay-token storage on the Mac (contract growin-relay/1, C1 and C9, OD-16).

The relay token is the relay's own bearer for one session. It is not a Breeze
credential, and it is the only secret the Mac keeps. Nothing here logs, prints
or puts a token in an exception.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

RELAY_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
FILE_VERSION = 1
MAX_FILE_BYTES = 4096


@dataclass(frozen=True)
class StoredToken:
    """A saved relay token. The token is hidden from repr and str."""

    token: str = field(repr=False)
    expires_at_ist: datetime


class TokenStore(Protocol):
    def load(self) -> StoredToken | None: ...

    def save(self, token: str, expires_at_ist: datetime) -> None: ...

    def clear(self) -> None: ...


class MemoryTokenStore:
    """In-process store. The default for the http test seam."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stored: StoredToken | None = None

    def load(self) -> StoredToken | None:
        with self._lock:
            return self._stored

    def save(self, token: str, expires_at_ist: datetime) -> None:
        with self._lock:
            self._stored = StoredToken(token=token, expires_at_ist=expires_at_ist)

    def clear(self) -> None:
        with self._lock:
            self._stored = None


def same_token(left: str, right: str) -> bool:
    """Constant-time comparison of two tokens."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def default_token_path() -> Path:
    return Path.home() / ".config" / "growin" / "relay-session.json"


class FileTokenStore:
    """The saved relay token: a 0600 file in a 0700 directory, owned by the caller.

    load() accepts the file only when it is a regular file (never a symlink), mode
    0600, owned by the current user, in a 0700 directory owned by the current user,
    well formed and unexpired. Anything else is None, never an exception.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        uid: int | None = None,
    ) -> None:
        self._path = path if path is not None else default_token_path()
        self._now = now if now is not None else (lambda: datetime.now(timezone.utc))
        self._uid = uid if uid is not None else os.geteuid()

    def _safe_dir(self, info: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(info.st_mode)
            and stat.S_IMODE(info.st_mode) == 0o700
            and info.st_uid == self._uid
        )

    def load(self) -> StoredToken | None:
        try:
            if not self._safe_dir(os.lstat(self._path.parent)):
                return None
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != self._uid
                ):
                    return None
                raw = os.read(fd, MAX_FILE_BYTES + 1)
            finally:
                os.close(fd)
            if len(raw) > MAX_FILE_BYTES:
                return None
            data = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(data, dict)
                or set(data) != {"v", "token", "expires_at_ist"}
                or data["v"] != FILE_VERSION
                or isinstance(data["v"], bool)
                or not isinstance(data["token"], str)
                or not RELAY_TOKEN_RE.fullmatch(data["token"])
                or not isinstance(data["expires_at_ist"], str)
            ):
                return None
            expires = datetime.fromisoformat(data["expires_at_ist"])
            if expires.tzinfo is None or expires <= self._now():
                return None
            return StoredToken(token=data["token"], expires_at_ist=expires)
        except (OSError, ValueError):
            return None

    def save(self, token: str, expires_at_ist: datetime) -> None:
        if not isinstance(token, str) or not RELAY_TOKEN_RE.fullmatch(token):
            raise ValueError("invalid_token")
        if expires_at_ist.tzinfo is None:
            raise ValueError("invalid_expiry")
        parent = self._path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self._safe_dir(os.lstat(parent)):
            raise PermissionError("insecure_directory")
        payload = json.dumps(
            {"v": FILE_VERSION, "token": token, "expires_at_ist": expires_at_ist.isoformat()},
            separators=(",", ":"),
        ).encode("utf-8")
        temp = parent / f".{self._path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp, self._path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def clear(self) -> None:
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass

"""Relay-token storage on the Mac (contract growin-relay/1, C1 and C9, OD-16).

The relay token is the relay's own bearer for one session. It is not a Breeze
credential, and it is the only secret the Mac keeps. Nothing here logs, prints
or puts a token in an exception.
"""

from __future__ import annotations

import hmac
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


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

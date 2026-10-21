"""The operator alert channel (63-05): a durable, append-only log on the VM.

The external channel (a message to the operator's phone) is DEFERRED by the operator,
so the alert is written to ``alerts.jsonl`` in the StateDirectory and shown to the
operator on the Mac: ``GET /v1/orders/state`` reports how many alerts were logged, and
``session_end_check`` already audits every alert as an EVALUATED entry that the Mac
reads from ``GET /v1/orders/audit``. The alert never unblocks an order and never
changes any state; it can only be read.

One JSON object per line: ISIN, session date and quantity (StopExitAlert has no
other fields). Opened O_APPEND with mode 0600, fsynced before send() returns, so a
send() that returns means the line is on disk. A symlink or a non-regular file at
the path is refused. Any failure raises, which session_end_check records as
``alert_failed`` and retries on the next call.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .risk import StopExitAlert

ALERT_FILE = "alerts.jsonl"
_MAX_LOG_BYTES = 4 * 1024 * 1024


class AlertLogError(Exception):
    """The alert log could not be written or read safely."""


class FileAlertPort:
    def __init__(self, directory: str | Path) -> None:
        self.path = Path(directory) / ALERT_FILE

    def send(self, alert: StopExitAlert) -> None:
        line = json.dumps(
            {
                "kind": "stop_exit_open",
                "isin": alert.isin,
                "session_date": alert.session_date.isoformat(),
                "quantity": alert.quantity,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            fd = os.open(
                self.path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError:
            raise AlertLogError("alert log unwritable") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise AlertLogError("alert log is not a private regular file")
            data = (line + "\n").encode("ascii")
            if os.write(fd, data) != len(data):
                raise AlertLogError("short alert write")
            os.fsync(fd)
        except OSError:
            raise AlertLogError("alert log unwritable") from None
        finally:
            os.close(fd)

    def count(self) -> int:
        """Lines logged so far. A missing log is 0; an unreadable one raises."""
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return 0
        except OSError:
            raise AlertLogError("alert log unreadable") from None
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_LOG_BYTES:
            raise AlertLogError("alert log unreadable")
        try:
            return sum(1 for line in self.path.read_bytes().splitlines() if line)
        except OSError:
            raise AlertLogError("alert log unreadable") from None

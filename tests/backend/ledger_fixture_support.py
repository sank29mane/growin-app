"""Replay helper for the captured v5 ledger fixture (not collected by pytest)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

FIXTURE_SQL_PATH = Path(__file__).resolve().parent / "fixtures" / "ledger_v5.sql"


def replay_v5_fixture(path: Path) -> Path:
    """Create a WAL SQLite file at ``path`` from ``ledger_v5.sql`` and return it.

    The SQL ends with ``PRAGMA user_version = 5;`` so the replayed file reports
    schema version 5 without any help from the ledger class.
    """

    path = Path(path)
    if path.exists():
        raise FileExistsError(f"refusing to replay the fixture over {path}")
    script = FIXTURE_SQL_PATH.read_text(encoding="utf-8")
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.executescript(script)
        connection.execute("PRAGMA journal_mode=WAL").fetchone()
    finally:
        connection.close()
    return path

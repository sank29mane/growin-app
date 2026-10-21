"""The stock_code to (ISIN, series) map from the ICICI security master (P-21, T-63-12).

The VM binds every quote, trade and order row to an ISIN through this map, so the
ISIN on an intent can never be taken on trust from the Mac. The map lives in
memory for one IST day and is never written anywhere. Only live EQ and BE rows are
kept (Token not 0). A stock code with more than one such row is ambiguous and is
left out, which answers isin_mismatch downstream (fail closed). An ISIN shared by
two stock codes is left out of the reverse map for the same reason.

The parser reads one named zip member under a hard size cap. Nothing in the file
is echoed into an error.
"""

from __future__ import annotations

import csv
import io
import re
import threading
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import Callable

MASTER_MEMBER = "NSEScripMaster.txt"
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 8
REQUIRED_COLUMNS = ("Token", "ShortName", "Series", "ISINCode")
CASH_SERIES = ("EQ", "BE")
_ISIN = re.compile(r"IN[A-Z0-9]{9}[0-9]")
_STOCK_CODE = re.compile(r"[A-Z0-9]{1,10}")


class MasterError(Exception):
    """The master is unreadable, malformed or too large. Carries no file content."""


@dataclass(frozen=True)
class MasterRow:
    isin: str
    series: str


class MasterMap:
    """Immutable lookup built from one master file."""

    def __init__(self, by_code: dict[str, MasterRow], by_isin: dict[str, str]) -> None:
        self._by_code = dict(by_code)
        self._by_isin = dict(by_isin)

    def __len__(self) -> int:
        return len(self._by_code)

    def row(self, stock_code: str) -> MasterRow | None:
        return self._by_code.get(stock_code)

    def code_for_isin(self, isin: str) -> str | None:
        return self._by_isin.get(isin)


def parse_master(content: bytes) -> MasterMap:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS:
                raise MasterError("master zip has too many members")
            matches = [info for info in infos if info.filename == MASTER_MEMBER]
            if len(matches) != 1:
                raise MasterError("master zip does not hold the master file")
            info = matches[0]
            if info.file_size > MAX_MEMBER_BYTES:
                raise MasterError("master file is too large")
            with archive.open(info) as handle:
                raw = handle.read(MAX_MEMBER_BYTES + 1)
    except MasterError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError, ValueError, EOFError):
        raise MasterError("master zip is unreadable") from None
    if len(raw) > MAX_MEMBER_BYTES:
        raise MasterError("master file is too large")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise MasterError("master file is not UTF-8") from None
    reader = csv.reader(io.StringIO(text), skipinitialspace=True)
    try:
        header = next(reader)
    except StopIteration:
        raise MasterError("master file is empty") from None
    if any(name not in header for name in REQUIRED_COLUMNS):
        raise MasterError("master file lacks a required column")
    index = {name: header.index(name) for name in REQUIRED_COLUMNS}
    seen: dict[str, MasterRow] = {}
    ambiguous: set[str] = set()
    try:
        for row in reader:
            if not row:
                continue
            if len(row) != len(header):
                raise MasterError("master row has the wrong field count")
            token = row[index["Token"]].strip()
            series = row[index["Series"]].strip()
            code = row[index["ShortName"]].strip()
            isin = row[index["ISINCode"]].strip()
            if not token.isdigit() or int(token) == 0:
                continue  # an index line, a placeholder or a dead instrument
            if series not in CASH_SERIES or _STOCK_CODE.fullmatch(code) is None:
                continue
            if _ISIN.fullmatch(isin) is None:
                ambiguous.add(code)  # a live cash row with no usable ISIN: never guess
                continue
            if code in seen:
                ambiguous.add(code)
            else:
                seen[code] = MasterRow(isin=isin, series=series)
    except csv.Error:
        raise MasterError("master file is not valid CSV") from None
    by_code = {code: row for code, row in seen.items() if code not in ambiguous}
    owners: dict[str, list[str]] = {}
    for code, row in by_code.items():
        owners.setdefault(row.isin, []).append(code)
    by_isin = {isin: codes[0] for isin, codes in owners.items() if len(codes) == 1}
    by_code = {code: row for code, row in by_code.items() if row.isin in by_isin}
    if not by_code:
        raise MasterError("master file holds no usable cash row")
    return MasterMap(by_code, by_isin)


class MasterHolder:
    """One master map per IST day, loaded on first use and dropped at the next day.

    ``fetch`` returns the zip bytes (the relay's own security-master path, with its
    budget charge and egress check). Memory only: nothing is cached on disk.
    """

    def __init__(self, fetch: Callable[[], bytes], today: Callable[[], date]) -> None:
        self._fetch = fetch
        self._today = today
        self._lock = threading.Lock()
        self._day: date | None = None
        self._map: MasterMap | None = None

    def current(self) -> MasterMap:
        day = self._today()
        with self._lock:
            if self._map is None or self._day != day:
                self._map = parse_master(self._fetch())
                self._day = day
            return self._map

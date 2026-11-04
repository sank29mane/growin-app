"""Synthetic fixtures for the D-34 CAS snapshot procedure (64-02 Task 2b).

Everything here is built in the test: small gz files in the shape of ``NSE_CM_security_DDMMYYYY.csv.gz``, a tiny
ICICI master, a pinned calendar and an approval log written to a temp path. No real NSE file, no network, no secret.
ISINs are real-shaped ones that pass the validator (or are generated with a check digit computed here).
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from costs import cas_class
from costs.cas_class import entry_sha256
from pilot_data import cas_snapshots
from pilot_data.core import SourceDescriptor
from pilot_data.nse_calendar import PinnedCalendar, load_pinned_calendar
from pilot_data.security_master import ingest_security_master
from pilot_data.store import PilotDataStore

import pilot_data_testkit as kit

D = date(2026, 10, 29)  # Thursday, a session
D_PREV = date(2026, 10, 28)
D_NEXT = date(2026, 10, 30)
SATURDAY = date(2026, 10, 31)

HEADER = (
    "Sgmt", "TckrSymb", "SctySrs", "ISIN", "FinInstrmNm", "ElgbltyNrmlMkt", "SctyStsNrmlMkt",
    "ElgbltyClsgAuctnSsn", "DelFlg", "Spare",
)


def isin_with_check(body11: str) -> str:
    """Append the ISO 6166 check digit, computed here and independent of the validator under test."""
    digits = "".join(str(int(char, 36)) for char in body11)
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return body11 + str((10 - total % 10) % 10)


ISIN_REL = "INE002A01018"
ISIN_NIFTY = "INF204KB14I2"
ISIN_TMPV = "INE155A01022"
ISIN_HDFC = "INE040A01034"
ISIN_CAN = "INE476A01022"
ISIN_DVR = isin_with_check("IN9000A0100")  # an IN9 (DVR-style) id is valid
ISIN_UNK1 = isin_with_check("INE0UNK0100")
ISIN_UNK2 = isin_with_check("INE0UNK0200")
ISIN_CONF = isin_with_check("INE0CNF0100")
ISIN_NOACT = isin_with_check("INE0NAC0100")
ISIN_AMB1 = isin_with_check("INE0AMB0100")
ISIN_AMB2 = isin_with_check("INE0AMB0200")
ISIN_BADCHK = ISIN_REL[:-1] + str((int(ISIN_REL[-1]) + 1) % 10)
ISIN_DUMMY = "DUMMY"


def nse_row(
    symbol: str, series: str, isin: str, flag: str, *, del_flag: str = "N", eligibility: str = "1", status: str = "6"
) -> dict[str, str]:
    return {
        "Sgmt": "CM", "TckrSymb": symbol, "SctySrs": series, "ISIN": isin, "FinInstrmNm": symbol + " FIXTURE",
        "ElgbltyNrmlMkt": eligibility, "SctyStsNrmlMkt": status, "ElgbltyClsgAuctnSsn": flag, "DelFlg": del_flag,
        "Spare": "",
    }


def standard_rows() -> list[dict[str, str]]:
    return [
        nse_row("RELIANCE", "EQ", ISIN_REL, "1"),
        nse_row("NIFTYBEES", "EQ", ISIN_NIFTY, "0"),
        nse_row("NIFTYINAV", "EQ", ISIN_NIFTY, "0", eligibility="0"),  # iNAV twin: shares the ISIN, inactive
        nse_row("TMPV", "EQ", ISIN_TMPV, "1"),
        nse_row("TATAMOTORS", "EQ", ISIN_TMPV, "1", del_flag="Y", eligibility="0", status="3"),  # deleted old symbol
        nse_row("HDFCBANK", "EQ", ISIN_HDFC, "1"),
        nse_row("HDFCBANK", "BE", ISIN_HDFC, "1", status="1"),  # a BE row of a CAS ISIN
        nse_row("CANBK", "EQ", ISIN_CAN, "0"),
        nse_row("DVRCO", "EQ", ISIN_DVR, "1"),
        nse_row("DUMMYCO", "EQ", ISIN_DUMMY, "1"),  # not an ISIN
        nse_row("BADCHKCO", "EQ", ISIN_BADCHK, "1"),  # bad check digit
        nse_row("UNKA", "EQ", ISIN_UNK1, "1"),  # unknown flag beside a usable row, usable first
        nse_row("UNKB", "EQ", ISIN_UNK1, "X"),
        nse_row("UNKC", "EQ", ISIN_UNK2, "X"),  # unknown first, usable second
        nse_row("UNKD", "EQ", ISIN_UNK2, "0"),
        nse_row("CONFA", "EQ", ISIN_CONF, "1"),
        nse_row("CONFB", "EQ", ISIN_CONF, "0"),
        nse_row("GONECO", "EQ", ISIN_NOACT, "1", del_flag="Y", eligibility="0", status="3"),  # no active row
        nse_row("AMBIG", "EQ", ISIN_AMB1, "1"),  # one symbol, two ISINs among active rows
        nse_row("AMBIG", "EQ", ISIN_AMB2, "0"),
    ]


def snapshot_bytes(rows: list[dict[str, str]], header: tuple[str, ...] = HEADER) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(name, "") for name in header])
    return gzip.compress(buffer.getvalue().encode("utf-8"), mtime=0)


def snapshot_name(day: date) -> str:
    return f"NSE_CM_security_{day:%d%m%Y}.csv.gz"


def write_snapshot(folder: Path, day: date, rows: list[dict[str, str]] | None = None, *, name: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name or snapshot_name(day))
    path.write_bytes(snapshot_bytes(standard_rows() if rows is None else rows))
    return path


def calendar_for(tmp_path: Path, *, holidays: tuple[date, ...] = ()) -> PinnedCalendar:
    doc = {
        "schema": "growin-nse-calendar/1", "exchange": "NSE", "segment": "CM", "basis": "test_fixture",
        "coverage": {"from": "2026-01-01", "to": "2026-12-31"},
        "source": {"publisher": "test fixture", "reference": "none", "published_on": "2025-01-01",
                   "document_sha256": "d" * 64, "saved_by": "test", "saved_at_utc": "2025-01-02T00:00:00+00:00"},
        "holidays": [{"date": day.isoformat(), "name": "fixture holiday"} for day in holidays],
        "special_sessions": [],
    }
    raw = json.dumps(doc, sort_keys=True).encode()
    path = tmp_path / "calendar.json"
    path.write_bytes(raw)
    loaded = load_pinned_calendar(path, expected_sha256=hashlib.sha256(raw).hexdigest())
    assert loaded is not None
    return loaded


class ProcedureLogFile:
    """A writable stand-in for ``nse_cas_procedure.json`` that keeps the hash chain right."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[dict[str, Any]] = []
        self.flush()

    def flush(self) -> None:
        self.path.write_text(
            json.dumps({"schema": cas_class.CAS_PROCEDURE_SCHEMA, "entries": self.entries}), encoding="utf-8"
        )

    def _append(self, raw: dict[str, Any]) -> None:
        raw["prev_sha256"] = entry_sha256(self.entries[-1]) if self.entries else None
        self.entries.append(raw)
        self.flush()

    def approve(
        self, version: int, *, spec: str | None = None, impl: str | None = None, u24_status: str = "unverified",
        u24_evidence_ref: str | None = None,
    ) -> None:
        running_spec, running_impl = cas_snapshots.running_identity()
        self._append(
            {
                "procedure_version": version,
                "spec_sha256": spec or running_spec,
                "impl_sha256": impl or running_impl,
                "u24_status": u24_status,
                "u24_evidence_ref": u24_evidence_ref,
                "operator_approval": {"line": "approved in a test", "date": "2026-11-05"},
            }
        )

    def revoke(self, version: int) -> None:
        self._append({"revokes": version, "operator_approval": {"line": "revoked in a test", "date": "2026-11-06"}})


ICICI_STALE = kit.master_row(3457, "TATOLD", "EQ", "TATA MOTORS OLD NAME", "0.01", "2", ISIN_TMPV, "TATAMOTORS")
ICICI_HDFC_BE = kit.master_row(7777, "HDFBE", "BE", "HDFC BANK BE", "0.01", "1", ISIN_HDFC, "HDFCBANK")
ICICI_DVR = kit.master_row(8001, "DVRCO", "EQ", "DVR CO", "0.01", "1", ISIN_DVR, "DVRCO")
ICICI_DUMMY = kit.master_row(8002, "DUMMYC", "EQ", "DUMMY CO", "0.01", "1", ISIN_DUMMY, "DUMMYCO")
ICICI_UNK1 = kit.master_row(8003, "UNKA", "EQ", "UNK A", "0.01", "1", ISIN_UNK1, "UNKA")
ICICI_UNK2 = kit.master_row(8004, "UNKD", "EQ", "UNK D", "0.01", "1", ISIN_UNK2, "UNKD")
ICICI_CONF = kit.master_row(8005, "CONFA", "EQ", "CONF A", "0.01", "1", ISIN_CONF, "CONFA")
ICICI_NOACT = kit.master_row(8006, "GONECO", "EQ", "GONE CO", "0.01", "1", ISIN_NOACT, "GONECO")
ICICI_AMB = kit.master_row(8007, "AMBIG", "EQ", "AMBIG", "0.01", "1", ISIN_AMB1, "AMBIG")
ISIN_ABSENT = isin_with_check("INE0ABS0100")
ICICI_ABSENT = kit.master_row(8008, "ABSENT", "EQ", "ABSENT CO", "0.01", "1", ISIN_ABSENT, "ABSENT")
ICICI_TWIN_EQ = kit.master_row(8009, "TWINC", "EQ", "TWIN EQ", "0.01", "1", ISIN_REL, "RELIANCE")
ICICI_TWIN_BE = kit.master_row(8010, "TWINC", "BE", "TWIN BE", "0.01", "1", ISIN_REL, "RELIANCE")
ICICI_ROWS = [
    kit.MASTER_RELIND, kit.MASTER_NIFBEE, kit.MASTER_TATMOT, ICICI_STALE, kit.MASTER_HDFBAN, ICICI_HDFC_BE,
    kit.MASTER_CANBAN, ICICI_DVR, ICICI_DUMMY, ICICI_UNK1, ICICI_UNK2, ICICI_CONF, ICICI_NOACT, ICICI_AMB, ICICI_ABSENT, ICICI_TWIN_EQ, ICICI_TWIN_BE,
]


def ingest_icici(
    root: Path, day: date, rows: list[dict[str, str]] | None = None, *, fetched_at: datetime | None = None
) -> str:
    descriptor = SourceDescriptor(
        source="local_file", kind="security_master", locator="NSEScripMaster.txt",
        fetched_at=fetched_at or datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc),
    )
    with PilotDataStore(root, workspace="india") as store:
        outcome = ingest_security_master(
            store, descriptor, kit.security_master_bytes(ICICI_ROWS if rows is None else rows),
            snapshot_date=day, snapshot_date_basis="test_fixture",
        )
    return outcome.snapshot_sha256


@dataclass
class World:
    root: Path
    tmp: Path
    calendar: PinnedCalendar
    log: ProcedureLogFile
    icici_sha: dict[date, str] = field(default_factory=dict)

    def import_(self, day: date = D, version: int = 1, rows: list[dict[str, str]] | None = None, **kwargs: Any):
        path = write_snapshot(self.tmp / f"inbox-{day.isoformat()}", day, rows)
        return cas_snapshots.import_snapshot(self.root, path, day, version, calendar=self.calendar, **kwargs)

    def resolve(self, stock_code: str, day: date = D, version: int = 1, pin: Any = None):
        return cas_snapshots.resolve_broker(
            self.root, day, version, stock_code, cas_snapshots.IciciPin.select(day) if pin is None else pin
        )


def make_world(tmp_path: Path, monkeypatch: Any, *, approve: bool = True, icici_days: tuple[date, ...] = (D,)) -> World:
    log = ProcedureLogFile(tmp_path / "nse_cas_procedure.json")
    monkeypatch.setattr(cas_class, "PROCEDURE_PATH", log.path)
    if approve:
        log.approve(1)
    root = tmp_path / "pilot"
    world = World(root, tmp_path, calendar_for(tmp_path), log)
    for day in icici_days:
        world.icici_sha[day] = ingest_icici(root, day)
    return world

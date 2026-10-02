"""ASM and GSM surveillance lists from the NSE API, stored as dated snapshots.

NSE serves both lists for today only, so history starts at the first stored snapshot
(D-05). A missed day cannot be recovered later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from market_data.models import is_valid_isin
from pydantic import BaseModel, ConfigDict

from .core import PilotDataError, SourceDescriptor, canonical_sha256, utc_naive
from .nse_http import NoFile, NseHttp
from .store import PilotDataStore

ListKind = Literal["asm", "gsm"]
ASM_URL = "https://www.nseindia.com/api/reportASM"
GSM_URL = "https://www.nseindia.com/api/reportGSM"
ASM_ROW_KEYS = frozenset(
    {"asmSurvIndicator", "asmTime", "companyName", "isin", "series", "survCode", "survDesc", "symbol", "srno"}
)
GSM_ROW_KEYS = frozenset(
    {"companyName", "gsmStage", "gsmTime", "isin", "survCode", "survDesc", "symbol", "srno"}
)
_MONTHS = {m: i + 1 for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}

SNAPSHOTS_DDL = (
    "CREATE TABLE IF NOT EXISTS surveillance_snapshots("
    "source_sha256 VARCHAR PRIMARY KEY, list_name VARCHAR NOT NULL, effective_date DATE NOT NULL, "
    "min_date DATE NOT NULL, fetched_at_utc TIMESTAMP NOT NULL, entry_count BIGINT NOT NULL, "
    "row_sha256 VARCHAR NOT NULL)"
)
ENTRIES_DDL = (
    "CREATE TABLE IF NOT EXISTS surveillance_entries("
    "source_sha256 VARCHAR NOT NULL, list_name VARCHAR NOT NULL, isin VARCHAR, nse_symbol VARCHAR NOT NULL, "
    "stage VARCHAR NOT NULL, term VARCHAR, surv_code VARCHAR NOT NULL, surv_desc VARCHAR NOT NULL, "
    "row_sha256 VARCHAR NOT NULL, PRIMARY KEY(source_sha256, row_sha256))"
)


def ensure_surveillance_tables(store: PilotDataStore) -> None:
    store.ensure_table("surveillance_snapshots", SNAPSHOTS_DDL, key_columns=("source_sha256",))
    store.ensure_table("surveillance_entries", ENTRIES_DDL, key_columns=("source_sha256", "row_sha256"))


class SurveillanceEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    list_name: ListKind
    isin: str | None
    nse_symbol: str
    stage: str
    term: Literal["long", "short"] | None
    surv_code: str
    surv_desc: str

    def row_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


@dataclass(frozen=True)
class SurveillanceParse:
    entries: tuple[SurveillanceEntry, ...]
    effective_date: date
    min_date: date
    extra_keys: tuple[str, ...]


class SurveillanceSnapshotRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_sha256: str
    list_name: ListKind
    effective_date: date
    min_date: date
    entry_count: int


def _mismatch(message: str) -> PilotDataError:
    return PilotDataError("surveillance_schema_mismatch", message)


def _parse_nse_date(text: Any) -> date:
    """dd-Mon-yyyy, optionally followed by a time of day (the time is ignored)."""
    if not isinstance(text, str):
        raise _mismatch("surveillance date is not a string")
    parts = text.strip().split(" ")[0].split("-")
    try:
        day, month, year = int(parts[0]), _MONTHS[parts[1].upper()], int(parts[2])
        return date(year, month, day)
    except (IndexError, KeyError, ValueError) as exc:
        raise _mismatch("surveillance date is not dd-Mon-yyyy") from exc


def _isin_or_none(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and is_valid_isin(value.strip()) else None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _load(content: bytes) -> Any:
    try:
        return json.loads(content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _mismatch("surveillance body is not valid JSON") from exc


def _finish(entries: list[SurveillanceEntry], dates: list[date], extra: set[str]) -> SurveillanceParse:
    if not entries:
        raise PilotDataError("surveillance_empty", "surveillance list holds no rows")
    return SurveillanceParse(tuple(entries), max(dates), min(dates), tuple(sorted(extra)))


def parse_asm(content: bytes) -> SurveillanceParse:
    body = _load(content)
    if not isinstance(body, dict):
        raise _mismatch("ASM body is not an object")
    entries: list[SurveillanceEntry] = []
    dates: list[date] = []
    extra: set[str] = set()
    for key, term in (("longterm", "long"), ("shortterm", "short")):
        section = body.get(key)
        if not isinstance(section, dict) or not isinstance(section.get("data"), list):
            raise _mismatch(f"ASM {key}.data is missing")
        for row in section["data"]:
            if not isinstance(row, dict) or not ASM_ROW_KEYS <= set(row):
                raise _mismatch("ASM row is missing required keys")
            extra |= set(row) - ASM_ROW_KEYS
            dates.append(_parse_nse_date(row["asmTime"]))
            entries.append(
                SurveillanceEntry(
                    list_name="asm", isin=_isin_or_none(row["isin"]), nse_symbol=_text(row["symbol"]),
                    stage=_text(row["asmSurvIndicator"]), term=term, surv_code=_text(row["survCode"]),
                    surv_desc=_text(row["survDesc"]),
                )
            )
    return _finish(entries, dates, extra)


def parse_gsm(content: bytes) -> SurveillanceParse:
    body = _load(content)
    if not isinstance(body, list):
        raise _mismatch("GSM body is not an array")
    entries: list[SurveillanceEntry] = []
    dates: list[date] = []
    extra: set[str] = set()
    for row in body:
        if not isinstance(row, dict) or not GSM_ROW_KEYS <= set(row):
            raise _mismatch("GSM row is missing required keys")
        extra |= set(row) - GSM_ROW_KEYS
        dates.append(_parse_nse_date(row["gsmTime"]))
        entries.append(
            SurveillanceEntry(
                list_name="gsm", isin=_isin_or_none(row["isin"]), nse_symbol=_text(row["symbol"]),
                stage=_text(row["gsmStage"]), term=None, surv_code=_text(row["survCode"]),
                surv_desc=_text(row["survDesc"]),
            )
        )
    return _finish(entries, dates, extra)


def ingest_surveillance(
    store: PilotDataStore, descriptor: SourceDescriptor, content: bytes, *, list_name: ListKind
) -> SurveillanceSnapshotRef:
    ensure_surveillance_tables(store)
    ref = store.register_source(descriptor, content)
    parsed = parse_asm(content) if list_name == "asm" else parse_gsm(content)
    store.append_rows(
        "surveillance_entries",
        [
            {
                "source_sha256": ref.source_sha256, "list_name": list_name, "isin": entry.isin,
                "nse_symbol": entry.nse_symbol, "stage": entry.stage, "term": entry.term,
                "surv_code": entry.surv_code, "surv_desc": entry.surv_desc, "row_sha256": entry.row_sha256(),
            }
            for entry in parsed.entries
        ],
        check="surveillance_entries",
    )
    store.append_rows(
        "surveillance_snapshots",
        [
            {
                "source_sha256": ref.source_sha256, "list_name": list_name,
                "effective_date": parsed.effective_date, "min_date": parsed.min_date,
                "fetched_at_utc": utc_naive(descriptor.fetched_at), "entry_count": len(parsed.entries),
                "row_sha256": canonical_sha256(
                    {"list": list_name, "effective": parsed.effective_date.isoformat(),
                     "min": parsed.min_date.isoformat(), "count": len(parsed.entries)}
                ),
            }
        ],
        check="surveillance_snapshot",
    )
    return SurveillanceSnapshotRef(
        source_sha256=ref.source_sha256, list_name=list_name, effective_date=parsed.effective_date,
        min_date=parsed.min_date, entry_count=len(parsed.entries),
    )


def snapshot_for(store: PilotDataStore, list_name: ListKind, effective_date: date) -> SurveillanceSnapshotRef | None:
    """The latest fetched snapshot of a list whose NSE effective date is exactly effective_date."""
    ensure_surveillance_tables(store)
    found = store.query(
        "SELECT source_sha256, effective_date, min_date, entry_count FROM surveillance_snapshots "
        "WHERE list_name = ? AND effective_date = ? ORDER BY fetched_at_utc DESC, rowid DESC LIMIT 1",
        [list_name, effective_date],
    )
    if not found:
        return None
    source, effective, minimum, count = found[0]
    return SurveillanceSnapshotRef(
        source_sha256=source, list_name=list_name, effective_date=effective, min_date=minimum,
        entry_count=int(count),
    )


def first_snapshot_date(store: PilotDataStore) -> date | None:
    """Earliest effective date that has both an ASM and a GSM snapshot."""
    ensure_surveillance_tables(store)
    found = store.query(
        "SELECT min(a.effective_date) FROM surveillance_snapshots a JOIN surveillance_snapshots g "
        "ON g.effective_date = a.effective_date WHERE a.list_name = 'asm' AND g.list_name = 'gsm'"
    )
    return found[0][0] if found and found[0][0] is not None else None


def surveillance_entries(store: PilotDataStore, ref: SurveillanceSnapshotRef) -> tuple[SurveillanceEntry, ...]:
    ensure_surveillance_tables(store)
    rows = store.query(
        "SELECT isin, nse_symbol, stage, term, surv_code, surv_desc FROM surveillance_entries "
        "WHERE source_sha256 = ? ORDER BY nse_symbol, stage, row_sha256",
        [ref.source_sha256],
    )
    return tuple(
        SurveillanceEntry(list_name=ref.list_name, isin=r[0], nse_symbol=r[1], stage=r[2], term=r[3],
                          surv_code=r[4], surv_desc=r[5])
        for r in rows
    )


def fetch_surveillance(
    store: PilotDataStore, http: NseHttp
) -> tuple[SurveillanceSnapshotRef, SurveillanceSnapshotRef]:
    refs: list[SurveillanceSnapshotRef] = []
    for url, list_name, kind in ((ASM_URL, "asm", "surveillance_asm"), (GSM_URL, "gsm", "surveillance_gsm")):
        fetched = http.fetch(url, expect="json")
        if isinstance(fetched, NoFile):
            raise PilotDataError("surveillance_unavailable", f"NSE has no {list_name.upper()} list")
        refs.append(
            ingest_surveillance(store, fetched.descriptor(kind, None), fetched.content, list_name=list_name)
        )
    return refs[0], refs[1]


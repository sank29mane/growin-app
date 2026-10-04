"""ICICI security master parsing, dated snapshot history, change diff and ISIN identity.

The master is seeded from the operator's local file (Part 1) and refreshed through the VM
relay (Part 2). This module never downloads it. ticksize is stored as raw text only.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal

from market_data.models import is_valid_isin
from pydantic import BaseModel, ConfigDict, ValidationError

from .bhavcopy import quarantines_from_inputs
from .core import (
    IST,
    ZIP_MAGIC,
    PilotDataError,
    SourceDescriptor,
    canonical_sha256,
    read_zip_members,
    utc_naive,
)
from .models import IsinSegment, Lineage, ParseQuarantineInput, SecurityMasterRow
from .store import PilotDataStore

MASTER_MEMBER = "NSEScripMaster.txt"
MASTER_REQUIRED_COLUMNS = (
    "Token", "ShortName", "Series", "CompanyName", "ticksize", "ISINCode", "ExchangeCode",
    "DateOfListing", "DateOfDeListing", "DeleteFlag",
)

SnapshotBasis = Literal["operator_declared", "relay_fetch", "test_fixture"]
ChangeKind = Literal[
    "stock_code_isin_changed", "stock_code_symbol_changed", "token_changed", "row_added", "row_removed"
]

SNAPSHOTS_DDL = (
    "CREATE TABLE IF NOT EXISTS security_master_snapshots("
    "snapshot_sha256 VARCHAR NOT NULL, snapshot_date DATE NOT NULL, snapshot_date_basis VARCHAR NOT NULL, "
    "row_count BIGINT NOT NULL, ingested_at_utc TIMESTAMP NOT NULL, source_sha256 VARCHAR NOT NULL, "
    "row_sha256 VARCHAR NOT NULL, PRIMARY KEY(snapshot_sha256, snapshot_date))"
)
ROWS_DDL = (
    "CREATE TABLE IF NOT EXISTS security_master_rows("
    "snapshot_sha256 VARCHAR NOT NULL, token BIGINT NOT NULL, stock_code VARCHAR NOT NULL, "
    "series VARCHAR NOT NULL, company_name VARCHAR NOT NULL, tick_size_raw VARCHAR NOT NULL, "
    "isin VARCHAR NOT NULL, nse_symbol VARCHAR NOT NULL, listing_raw VARCHAR NOT NULL, "
    "delisting_raw VARCHAR NOT NULL, delete_flag_raw VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL, "
    "row_sha256 VARCHAR NOT NULL, PRIMARY KEY(snapshot_sha256, stock_code, series))"
)
CHANGES_DDL = (
    "CREATE TABLE IF NOT EXISTS security_master_changes("
    "new_snapshot_sha256 VARCHAR NOT NULL, prev_snapshot_sha256 VARCHAR NOT NULL, change_kind VARCHAR NOT NULL, "
    "stock_code VARCHAR NOT NULL, series VARCHAR NOT NULL, old_value VARCHAR, new_value VARCHAR, "
    "source_sha256 VARCHAR NOT NULL, row_sha256 VARCHAR PRIMARY KEY)"
)


def ensure_master_tables(store: PilotDataStore) -> None:
    store.ensure_table(
        "security_master_snapshots", SNAPSHOTS_DDL, key_columns=("snapshot_sha256", "snapshot_date")
    )
    store.ensure_table(
        "security_master_rows", ROWS_DDL, key_columns=("snapshot_sha256", "stock_code", "series")
    )
    store.ensure_table("security_master_changes", CHANGES_DDL, key_columns=("row_sha256",))


def _mismatch(message: str) -> PilotDataError:
    return PilotDataError("security_master_schema_mismatch", message)


@dataclass(frozen=True)
class MasterParse:
    rows: tuple[SecurityMasterRow, ...]
    skipped: tuple[ParseQuarantineInput, ...]


def parse_security_master_full(content: bytes) -> MasterParse:
    """Parse text or zip bytes. Rows whose Token is not an integer (indices, OFS lines, NA
    placeholders in the real file) cannot be mapped to a Breeze instrument and are reported
    in `skipped` instead of aborting the whole file."""
    if content.startswith(ZIP_MAGIC):
        members = read_zip_members(content)
        if MASTER_MEMBER not in members:
            raise _mismatch("zip does not contain NSEScripMaster.txt")
        content = members[MASTER_MEMBER]
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _mismatch("security master is not valid UTF-8") from exc
    reader = csv.reader(io.StringIO(text), skipinitialspace=True)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise _mismatch("security master is empty") from exc
    missing = [name for name in MASTER_REQUIRED_COLUMNS if name not in header]
    if missing:
        raise _mismatch(f"required columns missing: {missing}")
    index = {name: header.index(name) for name in MASTER_REQUIRED_COLUMNS}
    rows: list[SecurityMasterRow] = []
    skipped: list[ParseQuarantineInput] = []
    for raw in reader:
        if not raw:
            continue
        if len(raw) != len(header):
            raise _mismatch("security master row has the wrong field count")
        token_text = raw[index["Token"]].strip()
        if not token_text.isdigit():
            skipped.append(
                ParseQuarantineInput(
                    reason_code="non_numeric_token",
                    stock_code=raw[index["ShortName"]].strip() or None,
                    series=raw[index["Series"]].strip() or None,
                    detail={"token_raw": token_text[:40]},
                )
            )
            continue
        try:
            rows.append(
                SecurityMasterRow(
                    token=int(token_text),
                    stock_code=raw[index["ShortName"]].strip(),
                    series=raw[index["Series"]].strip(),
                    company_name=raw[index["CompanyName"]].strip(),
                    tick_size_raw=raw[index["ticksize"]].strip(),
                    isin=raw[index["ISINCode"]].strip(),
                    nse_symbol=raw[index["ExchangeCode"]].strip(),
                    listing_raw=raw[index["DateOfListing"]].strip(),
                    delisting_raw=raw[index["DateOfDeListing"]].strip(),
                    delete_flag_raw=raw[index["DeleteFlag"]].strip(),
                )
            )
        except ValidationError as exc:
            raise _mismatch("security master row failed validation") from exc
    return MasterParse(tuple(rows), tuple(skipped))


def parse_security_master(content: bytes) -> list[SecurityMasterRow]:
    return list(parse_security_master_full(content).rows)


class MasterSnapshotRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_sha256: str
    snapshot_date: date
    snapshot_date_basis: str
    row_count: int


class MasterChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    change_kind: ChangeKind
    stock_code: str
    series: str
    old_value: str | None
    new_value: str | None


@dataclass(frozen=True)
class MasterIngestOutcome:
    snapshot_sha256: str
    snapshot_date: date
    row_count: int
    rows_inserted: int
    skipped_rows: int = 0
    changes: tuple[MasterChange, ...] = ()
    prev_snapshot_sha256: str | None = None


def _row_from_db(values: tuple) -> SecurityMasterRow:
    token, stock_code, series, company, tick, isin, symbol, listing, delisting, delete_flag = values
    return SecurityMasterRow(
        token=int(token), stock_code=stock_code, series=series, company_name=company, tick_size_raw=tick,
        isin=isin, nse_symbol=symbol, listing_raw=listing, delisting_raw=delisting, delete_flag_raw=delete_flag,
    )


_ROW_COLUMNS = (
    "token, stock_code, series, company_name, tick_size_raw, isin, nse_symbol, listing_raw, delisting_raw, "
    "delete_flag_raw"
)


def _snapshot_from_db(row: tuple) -> MasterSnapshotRef:
    return MasterSnapshotRef(
        snapshot_sha256=row[0], snapshot_date=row[1], snapshot_date_basis=row[2], row_count=int(row[3])
    )


def latest_snapshot(store: PilotDataStore, *, on_or_before: date) -> MasterSnapshotRef | None:
    ensure_master_tables(store)
    found = store.query(
        "SELECT snapshot_sha256, snapshot_date, snapshot_date_basis, row_count FROM security_master_snapshots "
        "WHERE snapshot_date <= ? ORDER BY snapshot_date DESC, ingested_at_utc DESC, snapshot_sha256 LIMIT 1",
        [on_or_before],
    )
    return _snapshot_from_db(found[0]) if found else None


def require_fresh_snapshot(store: PilotDataStore, *, today_ist: date) -> MasterSnapshotRef:
    ensure_master_tables(store)
    found = store.query(
        "SELECT snapshot_sha256, snapshot_date, snapshot_date_basis, row_count FROM security_master_snapshots "
        "ORDER BY snapshot_date DESC, ingested_at_utc DESC, snapshot_sha256 LIMIT 1"
    )
    if not found:
        raise PilotDataError("security_master_missing", "no security master snapshot has been stored")
    ref = _snapshot_from_db(found[0])
    if ref.snapshot_date < today_ist:
        raise PilotDataError(
            "security_master_stale",
            f"latest security master snapshot is dated {ref.snapshot_date.isoformat()}, "
            f"today (IST) is {today_ist.isoformat()}",
        )
    return ref


def master_rows(store: PilotDataStore, snapshot: MasterSnapshotRef) -> list[SecurityMasterRow]:
    ensure_master_tables(store)
    return [
        _row_from_db(values)
        for values in store.query(
            f"SELECT {_ROW_COLUMNS} FROM security_master_rows WHERE snapshot_sha256 = ? ORDER BY stock_code, series",
            [snapshot.snapshot_sha256],
        )
    ]


def lookup_isin(
    store: PilotDataStore, snapshot: MasterSnapshotRef, isin: str, *, series: str
) -> SecurityMasterRow | None:
    """The live (token not 0) row for an ISIN and series, or None. Never matches by symbol."""
    if not is_valid_isin(isin):
        return None
    ensure_master_tables(store)
    found = store.query(
        f"SELECT {_ROW_COLUMNS} FROM security_master_rows WHERE snapshot_sha256 = ? AND isin = ? AND series = ? "
        "AND token <> 0 ORDER BY stock_code",
        [snapshot.snapshot_sha256, isin, series],
    )
    if len(found) > 1:
        raise PilotDataError("isin_ambiguous", "more than one live master row shares this ISIN and series")
    return _row_from_db(found[0]) if found else None


def lookup_stock_code(
    store: PilotDataStore, snapshot: MasterSnapshotRef, stock_code: str
) -> SecurityMasterRow | None:
    """The live row for a Breeze stock_code. EQ or BE wins over other series (warrants and so on)."""
    ensure_master_tables(store)
    found = [
        _row_from_db(values)
        for values in store.query(
            f"SELECT {_ROW_COLUMNS} FROM security_master_rows WHERE snapshot_sha256 = ? AND stock_code = ? "
            "AND token <> 0 ORDER BY series",
            [snapshot.snapshot_sha256, stock_code],
        )
    ]
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    cash = [row for row in found if row.series in ("EQ", "BE")]
    if len(cash) == 1:
        return cash[0]
    raise PilotDataError("stock_code_ambiguous", "stock_code has more than one live row")


def _diff(previous: list[SecurityMasterRow], current: list[SecurityMasterRow]) -> list[MasterChange]:
    old = {(row.stock_code, row.series): row for row in previous}
    new = {(row.stock_code, row.series): row for row in current}
    changes: list[MasterChange] = []
    for key in sorted(set(old) | set(new)):
        code, series = key
        if key not in old:
            changes.append(MasterChange(change_kind="row_added", stock_code=code, series=series,
                                        old_value=None, new_value=new[key].isin))
        elif key not in new:
            changes.append(MasterChange(change_kind="row_removed", stock_code=code, series=series,
                                        old_value=old[key].isin, new_value=None))
        else:
            before, after = old[key], new[key]
            if before.isin != after.isin:
                changes.append(MasterChange(change_kind="stock_code_isin_changed", stock_code=code, series=series,
                                            old_value=before.isin, new_value=after.isin))
            if before.nse_symbol != after.nse_symbol:
                changes.append(MasterChange(change_kind="stock_code_symbol_changed", stock_code=code,
                                            series=series, old_value=before.nse_symbol, new_value=after.nse_symbol))
            if before.token != after.token:
                changes.append(MasterChange(change_kind="token_changed", stock_code=code, series=series,
                                            old_value=str(before.token), new_value=str(after.token)))
    return changes


def ingest_security_master(
    store: PilotDataStore,
    descriptor: SourceDescriptor,
    content: bytes,
    *,
    snapshot_date: date,
    snapshot_date_basis: SnapshotBasis,
) -> MasterIngestOutcome:
    ensure_master_tables(store)
    fetched_ist = descriptor.fetched_at.astimezone(IST).date()
    if snapshot_date > fetched_ist:
        raise PilotDataError("snapshot_date_invalid", "snapshot_date is later than the fetch date in IST")
    ref = store.register_source(descriptor, content)
    parsed = parse_security_master_full(content)
    rows = list(parsed.rows)
    row_dicts = [
        {
            "snapshot_sha256": ref.source_sha256, "token": row.token, "stock_code": row.stock_code,
            "series": row.series, "company_name": row.company_name, "tick_size_raw": row.tick_size_raw,
            "isin": row.isin, "nse_symbol": row.nse_symbol, "listing_raw": row.listing_raw,
            "delisting_raw": row.delisting_raw, "delete_flag_raw": row.delete_flag_raw,
            "source_sha256": ref.source_sha256, "row_sha256": row.row_sha256(),
        }
        for row in rows
    ]
    outcome = store.append_rows("security_master_rows", row_dicts, check="security_master_rows")
    store.record_quarantine(
        quarantines_from_inputs(
            parsed.skipped, check="security_master_parse", source_sha256=ref.source_sha256,
            default_date=snapshot_date,
        )
    )
    previous = store.query(
        "SELECT snapshot_sha256, snapshot_date, snapshot_date_basis, row_count FROM security_master_snapshots "
        "WHERE snapshot_date < ? ORDER BY snapshot_date DESC, ingested_at_utc DESC, snapshot_sha256 LIMIT 1",
        [snapshot_date],
    )
    store.append_rows(
        "security_master_snapshots",
        [
            {
                "snapshot_sha256": ref.source_sha256, "snapshot_date": snapshot_date,
                "snapshot_date_basis": snapshot_date_basis, "row_count": len(rows),
                "ingested_at_utc": utc_naive(datetime.now(timezone.utc)), "source_sha256": ref.source_sha256,
                "row_sha256": canonical_sha256(
                    {"basis": snapshot_date_basis, "row_count": len(rows), "date": snapshot_date.isoformat()}
                ),
            }
        ],
        check="security_master_snapshot",
    )
    changes: list[MasterChange] = []
    prev_sha: str | None = None
    if previous and previous[0][0] != ref.source_sha256:
        prev_sha = previous[0][0]
        prev_rows = master_rows(store, _snapshot_from_db(previous[0]))
        changes = _diff(prev_rows, rows)
        store.append_rows(
            "security_master_changes",
            [
                {
                    "new_snapshot_sha256": ref.source_sha256, "prev_snapshot_sha256": prev_sha,
                    "change_kind": change.change_kind, "stock_code": change.stock_code, "series": change.series,
                    "old_value": change.old_value, "new_value": change.new_value,
                    "source_sha256": ref.source_sha256,
                    "row_sha256": canonical_sha256(
                        {"new": ref.source_sha256, "prev": prev_sha, **change.model_dump(mode="json")}
                    ),
                }
                for change in changes
            ],
            check="security_master_changes",
        )
    return MasterIngestOutcome(
        ref.source_sha256, snapshot_date, len(rows), outcome.inserted, skipped_rows=len(parsed.skipped),
        changes=tuple(changes), prev_snapshot_sha256=prev_sha,
    )


def _latest_snapshot_sha(store: PilotDataStore, as_of: date) -> str:
    ref = latest_snapshot(store, on_or_before=as_of)
    if ref is None:
        raise PilotDataError("security_master_missing", "no security master snapshot on or before as_of")
    return ref.snapshot_sha256


def lineage_from_master(
    store: PilotDataStore,
    *,
    stock_code: str,
    as_of: date,
    window_start: date,
    window_end: date,
    workspace: Literal["india"],
) -> Lineage:
    """Single-segment lineage from the latest master snapshot (the tracer form)."""
    snapshot = _latest_snapshot_sha(store, as_of)
    found = store.query(
        "SELECT token, series, isin, nse_symbol FROM security_master_rows "
        "WHERE snapshot_sha256 = ? AND stock_code = ? AND series IN ('EQ', 'BE') AND token <> 0",
        [snapshot, stock_code],
    )
    if not found:
        raise PilotDataError("stock_code_unmapped", "stock_code has no live EQ or BE row in the master")
    if len(found) > 1:
        raise PilotDataError("stock_code_ambiguous", "stock_code has more than one live EQ or BE row")
    token, series, isin, nse_symbol = found[0]
    if not is_valid_isin(isin):
        raise PilotDataError("isin_invalid", "master ISIN fails format or check-digit validation")
    return Lineage(
        workspace=workspace,
        anchor_isin=isin,
        anchor_series=series,
        stock_code=stock_code,
        segments=(
            IsinSegment(
                isin=isin, nse_symbol=nse_symbol, valid_from=window_start, valid_to=window_end,
                link="master_snapshot", token=int(token),
            ),
        ),
        resolved_from=window_start,
        built_as_of=as_of,
        basis="master_snapshot_single_segment",
    )

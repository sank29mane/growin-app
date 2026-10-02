"""ICICI security master parsing, snapshot storage and stock_code to ISIN identity."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import ValidationError

from .core import (
    PilotDataError,
    SourceDescriptor,
    ZIP_MAGIC,
    canonical_sha256,
    read_zip_members,
    utc_naive,
)
from .models import IsinSegment, Lineage, SecurityMasterRow
from .store import PilotDataStore

MASTER_MEMBER = "NSEScripMaster.txt"
MASTER_REQUIRED_COLUMNS = (
    "Token", "ShortName", "Series", "CompanyName", "ticksize", "ISINCode", "ExchangeCode",
    "DateOfListing", "DateOfDeListing", "DeleteFlag",
)

SnapshotBasis = Literal["operator_declared", "relay_fetch", "test_fixture"]

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


def ensure_master_tables(store: PilotDataStore) -> None:
    store.ensure_table(
        "security_master_snapshots", SNAPSHOTS_DDL, key_columns=("snapshot_sha256", "snapshot_date")
    )
    store.ensure_table(
        "security_master_rows", ROWS_DDL, key_columns=("snapshot_sha256", "stock_code", "series")
    )


def _mismatch(message: str) -> PilotDataError:
    return PilotDataError("security_master_schema_mismatch", message)


def parse_security_master(content: bytes) -> list[SecurityMasterRow]:
    """Parse the master from text bytes or from a zip holding NSEScripMaster.txt."""
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
    for raw in reader:
        if not raw:
            continue
        if len(raw) != len(header):
            raise _mismatch("security master row has the wrong field count")
        token_text = raw[index["Token"]].strip()
        if not token_text.isdigit():
            raise _mismatch("security master token is not a non-negative integer")
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
    return rows


@dataclass(frozen=True)
class MasterIngestOutcome:
    snapshot_sha256: str
    snapshot_date: date
    row_count: int
    rows_inserted: int


def ingest_security_master(
    store: PilotDataStore,
    descriptor: SourceDescriptor,
    content: bytes,
    *,
    snapshot_date: date,
    snapshot_date_basis: SnapshotBasis,
) -> MasterIngestOutcome:
    ensure_master_tables(store)
    ref = store.register_source(descriptor, content)
    rows = parse_security_master(content)
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
    return MasterIngestOutcome(ref.source_sha256, snapshot_date, len(rows), outcome.inserted)


_ISIN_SHAPE = re.compile(r"^IN[EF][A-Z0-9]{9}$")


def _latest_snapshot_sha(store: PilotDataStore, as_of: date) -> str:
    ensure_master_tables(store)
    found = store.query(
        "SELECT snapshot_sha256 FROM security_master_snapshots WHERE snapshot_date <= ? "
        "ORDER BY snapshot_date DESC, ingested_at_utc DESC LIMIT 1",
        [as_of],
    )
    if not found:
        raise PilotDataError("security_master_missing", "no security master snapshot on or before as_of")
    return found[0][0]


def lineage_from_master(
    store: PilotDataStore,
    *,
    stock_code: str,
    as_of: date,
    window_start: date,
    window_end: date,
    workspace: Literal["india"],
) -> Lineage:
    """Single-segment lineage from the latest master snapshot (tracer form)."""
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
    if not _ISIN_SHAPE.match(isin):
        raise PilotDataError("isin_invalid", "master ISIN does not have the expected shape")
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

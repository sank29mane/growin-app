"""Today's Nifty 500 and Nifty Smallcap 250 lists, stored as dated, hashed snapshots.

These lists are current only (no history), so everything built from them carries the
survivorship and hindsight caveats (D-08).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from market_data.models import is_valid_isin
from pydantic import BaseModel, ConfigDict

from .core import PilotDataError, SourceDescriptor, canonical_sha256, utc_naive
from .nse_http import NoFile, NseHttp
from .store import PilotDataStore

ListName = Literal["nifty500", "smallcap250"]
NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
SMALLCAP250_URL = "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap250list.csv"
EXPECTED_MEMBERS: dict[str, int] = {"nifty500": 500, "smallcap250": 250}
INDEX_LIST_HEADER = ("Company Name", "Industry", "Symbol", "Series", "ISIN Code")
_URLS = {"nifty500": NIFTY500_URL, "smallcap250": SMALLCAP250_URL}
_KINDS = {"nifty500": "index_list_nifty500", "smallcap250": "index_list_smallcap250"}

SNAPSHOTS_DDL = (
    "CREATE TABLE IF NOT EXISTS index_list_snapshots("
    "source_sha256 VARCHAR NOT NULL, list_name VARCHAR NOT NULL, fetched_at_utc TIMESTAMP NOT NULL, "
    "member_count BIGINT NOT NULL, placeholders_json VARCHAR NOT NULL, row_sha256 VARCHAR NOT NULL, "
    "PRIMARY KEY(source_sha256, list_name))"
)
MEMBERS_DDL = (
    "CREATE TABLE IF NOT EXISTS index_list_members("
    "source_sha256 VARCHAR NOT NULL, list_name VARCHAR NOT NULL, isin VARCHAR NOT NULL, nse_symbol VARCHAR NOT NULL, "
    "series VARCHAR NOT NULL, company_name VARCHAR NOT NULL, industry VARCHAR NOT NULL, row_sha256 VARCHAR NOT NULL, "
    "PRIMARY KEY(source_sha256, list_name, isin))"
)


def ensure_constituent_tables(store: PilotDataStore) -> None:
    store.ensure_table("index_list_snapshots", SNAPSHOTS_DDL, key_columns=("source_sha256", "list_name"))
    store.ensure_table("index_list_members", MEMBERS_DDL, key_columns=("source_sha256", "list_name", "isin"))


class IndexMember(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    company_name: str
    industry: str
    nse_symbol: str
    series: str
    isin: str

    def row_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


@dataclass(frozen=True)
class IndexListParse:
    members: tuple[IndexMember, ...]
    placeholders_excluded: tuple[str, ...]


class IndexListSnapshotRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_sha256: str
    list_name: str
    fetched_at: datetime
    member_count: int
    placeholders_excluded: tuple[str, ...] = ()


def parse_index_list(content: bytes, *, list_name: ListName) -> IndexListParse:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PilotDataError("index_list_schema_mismatch", "index list is not valid UTF-8") from exc
    reader = csv.reader(io.StringIO(text))
    try:
        header = tuple(cell.strip() for cell in next(reader))
    except StopIteration as exc:
        raise PilotDataError("index_list_schema_mismatch", "index list is empty") from exc
    if header != INDEX_LIST_HEADER:
        raise PilotDataError("index_list_schema_mismatch", "index list header differs from the verified header")
    members: list[IndexMember] = []
    placeholders: list[str] = []
    for raw in reader:
        if not raw:
            continue
        if len(raw) != len(INDEX_LIST_HEADER):
            raise PilotDataError("index_list_schema_mismatch", "index list row has the wrong field count")
        company, industry, symbol, series, isin = (cell.strip() for cell in raw)
        if symbol.upper().startswith("DUMMY") or not is_valid_isin(isin):
            placeholders.append(symbol)
            continue
        members.append(IndexMember(company_name=company, industry=industry, nse_symbol=symbol, series=series,
                                   isin=isin))
    isins = [member.isin for member in members]
    if len(isins) != len(set(isins)):
        raise PilotDataError("index_list_duplicate", "index list repeats an ISIN")
    expected = EXPECTED_MEMBERS[list_name]
    if len(members) != expected:
        raise PilotDataError(
            "index_list_count_mismatch", f"{list_name} has {len(members)} real members, expected {expected}"
        )
    return IndexListParse(tuple(members), tuple(placeholders))


def ingest_index_list(
    store: PilotDataStore, descriptor: SourceDescriptor, content: bytes, *, list_name: ListName
) -> IndexListSnapshotRef:
    ensure_constituent_tables(store)
    ref = store.register_source(descriptor, content)
    parsed = parse_index_list(content, list_name=list_name)
    store.append_rows(
        "index_list_members",
        [
            {
                "source_sha256": ref.source_sha256, "list_name": list_name, "isin": member.isin,
                "nse_symbol": member.nse_symbol, "series": member.series, "company_name": member.company_name,
                "industry": member.industry, "row_sha256": member.row_sha256(),
            }
            for member in parsed.members
        ],
        check="index_list_members",
    )
    placeholders_json = ",".join(parsed.placeholders_excluded)
    store.append_rows(
        "index_list_snapshots",
        [
            {
                "source_sha256": ref.source_sha256, "list_name": list_name,
                "fetched_at_utc": utc_naive(descriptor.fetched_at), "member_count": len(parsed.members),
                "placeholders_json": placeholders_json,
                "row_sha256": canonical_sha256(
                    {"list": list_name, "count": len(parsed.members), "placeholders": placeholders_json}
                ),
            }
        ],
        check="index_list_snapshot",
    )
    return IndexListSnapshotRef(
        source_sha256=ref.source_sha256, list_name=list_name, fetched_at=descriptor.fetched_at,
        member_count=len(parsed.members), placeholders_excluded=parsed.placeholders_excluded,
    )


def latest_index_list(store: PilotDataStore, list_name: ListName) -> IndexListSnapshotRef | None:
    ensure_constituent_tables(store)
    found = store.query(
        "SELECT source_sha256, fetched_at_utc, member_count, placeholders_json FROM index_list_snapshots "
        "WHERE list_name = ? ORDER BY fetched_at_utc DESC, rowid DESC LIMIT 1",
        [list_name],
    )
    if not found:
        return None
    source, fetched, count, placeholders = found[0]
    return IndexListSnapshotRef(
        source_sha256=source, list_name=list_name, fetched_at=fetched.replace(tzinfo=timezone.utc),
        member_count=int(count), placeholders_excluded=tuple(p for p in placeholders.split(",") if p),
    )


def index_members(store: PilotDataStore, ref: IndexListSnapshotRef) -> tuple[IndexMember, ...]:
    ensure_constituent_tables(store)
    rows = store.query(
        "SELECT company_name, industry, nse_symbol, series, isin FROM index_list_members "
        "WHERE source_sha256 = ? AND list_name = ? ORDER BY isin",
        [ref.source_sha256, ref.list_name],
    )
    return tuple(
        IndexMember(company_name=r[0], industry=r[1], nse_symbol=r[2], series=r[3], isin=r[4]) for r in rows
    )


def fetch_index_list(store: PilotDataStore, http: NseHttp, list_name: ListName) -> IndexListSnapshotRef:
    fetched = http.fetch(_URLS[list_name], expect="csv")
    if isinstance(fetched, NoFile):
        raise PilotDataError("index_list_unavailable", f"NSE has no file for {list_name}")
    return ingest_index_list(
        store, fetched.descriptor(_KINDS[list_name], None), fetched.content, list_name=list_name
    )


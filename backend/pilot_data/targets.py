"""Target universe: Nifty 500 plus liquid ETFs, mapped to Breeze stock codes by ISIN.

Mapping is by ISIN, series EQ and a live token, never by symbol. Liquid-ETF selection uses
today's liquidity, so every result carries the survivorship and hindsight caveats.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

import httpx
from market_data.models import is_valid_isin
from pydantic import BaseModel, ConfigDict

from .bhavcopy import ensure_bhavcopy_tables
from .constituents import fetch_index_list, index_members, latest_index_list
from .core import (
    CaveatedResult,
    PilotDataError,
    SourceDescriptor,
    canonical_sha256,
    dec_str,
    standard_caveats,
    utc_naive,
    utc_now,
)
from .nse_http import NseHttp, build_default_client
from .security_master import (
    ingest_security_master,
    latest_snapshot,
    lookup_isin,
    master_rows,
)
from .store import PilotDataStore
from .surveillance import fetch_surveillance

ETF_LIQUIDITY_LOOKBACK = 60
ETF_LIQUIDITY_MIN_OBSERVATIONS = 40
ETF_MIN_MEDIAN_TRADED_VALUE = Decimal("50000000")  # 5 crore rupees, matching PDAT-03

TARGETS_DDL = (
    "CREATE TABLE IF NOT EXISTS target_universe_snapshots("
    "target_sha256 VARCHAR PRIMARY KEY, workspace VARCHAR NOT NULL, as_of DATE NOT NULL, "
    "built_at_utc TIMESTAMP NOT NULL, payload_json VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL, "
    "row_sha256 VARCHAR NOT NULL)"
)


def ensure_target_tables(store: PilotDataStore) -> None:
    store.ensure_table("target_universe_snapshots", TARGETS_DDL, key_columns=("target_sha256",))


class TargetMember(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["nifty500", "liquid_etf"]
    anchor_isin: str
    anchor_series: Literal["EQ"] = "EQ"
    nse_symbol: str
    stock_code: str
    token: int
    company_name: str
    notes: tuple[str, ...] = ()


class TargetExclusion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str
    nse_symbol: str
    reason: str


class EtfRejected(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str
    nse_symbol: str
    stock_code: str
    reason: Literal["adv_below_min", "insufficient_known_observations"]
    eligibility_median: Decimal
    known_dates: int
    known_median: Decimal | None


class TargetUniverseResult(CaveatedResult):
    as_of: date
    members: tuple[TargetMember, ...]
    exclusions: tuple[TargetExclusion, ...]
    etf_rejected: tuple[EtfRejected, ...]
    master_snapshot: str
    nifty500_snapshot: str
    target_sha256: str


def decimal_median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _recent_primary_dates(store: PilotDataStore, as_of: date) -> dict[date, str]:
    """The 60 most recent distinct primary trade dates on or before as_of, with the kind to read."""
    ensure_bhavcopy_tables(store)
    rows = store.query(
        "SELECT trade_date, max(CASE WHEN file_kind = 'udiff' THEN 1 ELSE 0 END) FROM bhavcopy_files "
        "WHERE file_kind IN ('udiff', 'cm_legacy') AND trade_date <= ? GROUP BY trade_date "
        "ORDER BY trade_date DESC LIMIT ?",
        [as_of, ETF_LIQUIDITY_LOOKBACK],
    )
    if len(rows) < ETF_LIQUIDITY_LOOKBACK:
        raise PilotDataError(
            "insufficient_bhavcopy_history",
            f"only {len(rows)} primary bhavcopy dates on or before {as_of.isoformat()}, "
            f"need {ETF_LIQUIDITY_LOOKBACK}",
        )
    return {day: ("udiff" if has_udiff else "cm_legacy") for day, has_udiff in rows}


def _etf_daily_values(
    store: PilotDataStore, dates: dict[date, str], isins: list[str]
) -> dict[str, dict[date, Decimal]]:
    out: dict[str, dict[date, Decimal]] = {isin: {} for isin in isins}
    if not isins:
        return out
    placeholders = ", ".join("?" for _ in isins)
    for kind in ("udiff", "cm_legacy"):
        kind_dates = [day for day, k in dates.items() if k == kind]
        if not kind_dates:
            continue
        date_marks = ", ".join("?" for _ in kind_dates)
        rows = store.query(
            f"SELECT isin, trade_date, sum(traded_value) FROM bhavcopy_bars WHERE file_kind = ? "
            f"AND trade_date IN ({date_marks}) AND isin IN ({placeholders}) GROUP BY isin, trade_date",
            [kind, *kind_dates, *isins],
        )
        for isin, day, value in rows:
            out[isin][day] = Decimal(value)
    return out


def build_target_universe(
    store: PilotDataStore, *, as_of: date, workspace: Literal["india"]
) -> TargetUniverseResult:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    ensure_target_tables(store)
    snapshot = latest_snapshot(store, on_or_before=as_of)
    if snapshot is None:
        raise PilotDataError("security_master_missing", "no security master snapshot on or before as_of")
    nifty = latest_index_list(store, "nifty500")
    if nifty is None:
        raise PilotDataError("index_list_missing", "no Nifty 500 snapshot has been stored")
    members: list[TargetMember] = []
    exclusions: list[TargetExclusion] = []
    for item in sorted(index_members(store, nifty), key=lambda m: m.isin):
        try:
            row = lookup_isin(store, snapshot, item.isin, series="EQ")
        except PilotDataError as exc:
            exclusions.append(TargetExclusion(isin=item.isin, nse_symbol=item.nse_symbol, reason=exc.code))
            continue
        if row is None:
            dead = store.query(
                "SELECT count(*) FROM security_master_rows WHERE snapshot_sha256 = ? AND isin = ? AND series = 'EQ'",
                [snapshot.snapshot_sha256, item.isin],
            )[0][0]
            reason = "no_live_token" if dead else "no_breeze_mapping"
            exclusions.append(TargetExclusion(isin=item.isin, nse_symbol=item.nse_symbol, reason=reason))
            continue
        notes = ("symbol_mismatch",) if row.nse_symbol != item.nse_symbol else ()
        members.append(
            TargetMember(
                kind="nifty500", anchor_isin=item.isin, nse_symbol=item.nse_symbol, stock_code=row.stock_code,
                token=row.token, company_name=item.company_name, notes=notes,
            )
        )
    nifty_isins = {member.anchor_isin for member in members} | {e.isin for e in exclusions}
    candidates = [
        row
        for row in master_rows(store, snapshot)
        if row.series == "EQ" and row.token != 0 and row.isin.startswith("INF") and is_valid_isin(row.isin)
        and row.isin not in nifty_isins
    ]
    etf_rejected: list[EtfRejected] = []
    if candidates:
        dates = _recent_primary_dates(store, as_of)
        values = _etf_daily_values(store, dates, [row.isin for row in candidates])
        for row in candidates:
            observed = values[row.isin]
            eligibility = [observed.get(day, Decimal(0)) for day in dates]
            eligibility_median = decimal_median(eligibility)
            known = list(observed.values())
            known_median = decimal_median(known) if known else None
            reason: Literal["adv_below_min", "insufficient_known_observations"] | None = None
            if len(known) < ETF_LIQUIDITY_MIN_OBSERVATIONS:
                reason = "insufficient_known_observations"
            elif eligibility_median < ETF_MIN_MEDIAN_TRADED_VALUE:
                reason = "adv_below_min"
            if reason is not None:
                etf_rejected.append(
                    EtfRejected(
                        isin=row.isin, nse_symbol=row.nse_symbol, stock_code=row.stock_code, reason=reason,
                        eligibility_median=eligibility_median, known_dates=len(known), known_median=known_median,
                    )
                )
                continue
            members.append(
                TargetMember(
                    kind="liquid_etf", anchor_isin=row.isin, nse_symbol=row.nse_symbol, stock_code=row.stock_code,
                    token=row.token, company_name=row.company_name,
                    notes=(f"known_dates={len(known)}", f"known_median={dec_str(known_median)}",
                           f"eligibility_median={dec_str(eligibility_median)}"),
                )
            )
    members.sort(key=lambda m: (m.kind, m.anchor_isin))
    exclusions.sort(key=lambda e: e.isin)
    etf_rejected.sort(key=lambda e: e.isin)
    content = {
        "as_of": as_of.isoformat(),
        "members": [m.model_dump(mode="json") for m in members],
        "exclusions": [e.model_dump(mode="json") for e in exclusions],
        "etf_rejected": [e.model_dump(mode="json") for e in etf_rejected],
        "master_snapshot": snapshot.snapshot_sha256,
        "nifty500_snapshot": nifty.source_sha256,
    }
    result = TargetUniverseResult(
        workspace="india", caveats=standard_caveats(), as_of=as_of, members=tuple(members),
        exclusions=tuple(exclusions), etf_rejected=tuple(etf_rejected),
        master_snapshot=snapshot.snapshot_sha256, nifty500_snapshot=nifty.source_sha256,
        target_sha256=canonical_sha256(content),
    )
    store.append_rows(
        "target_universe_snapshots",
        [
            {
                "target_sha256": result.target_sha256, "workspace": store.workspace, "as_of": as_of,
                "built_at_utc": utc_naive(datetime.now(timezone.utc)),
                "payload_json": json.dumps(result.model_dump(mode="json"), sort_keys=True),
                "source_sha256": snapshot.snapshot_sha256, "row_sha256": result.target_sha256,
            }
        ],
        check="target_universe",
    )
    return result


def latest_target_universe(
    store: PilotDataStore, *, workspace: Literal["india"]
) -> TargetUniverseResult | None:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    ensure_target_tables(store)
    found = store.query(
        "SELECT payload_json FROM target_universe_snapshots ORDER BY built_at_utc DESC, rowid DESC LIMIT 1"
    )
    return TargetUniverseResult.model_validate(json.loads(found[0][0])) if found else None


def _caveat_payload() -> list[dict[str, str]]:
    return [caveat.model_dump() for caveat in standard_caveats()]


def main(argv: list[str] | None = None, *, client: httpx.Client | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pilot_data.targets", description="Reference data and target universe.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(name: str) -> argparse.ArgumentParser:
        child = sub.add_parser(name)
        child.add_argument("--root", required=True, type=Path)
        child.add_argument("--workspace", required=True, choices=["india"])
        child.add_argument("--min-interval-seconds", type=float, default=1.0)
        return child

    ingest = common("ingest-master")
    ingest.add_argument("--file", required=True, type=Path)
    ingest.add_argument("--snapshot-date", required=True, type=date.fromisoformat)
    common("fetch-lists")
    common("fetch-surveillance")
    build = common("build-targets")
    build.add_argument("--as-of", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    owns_client = client is None and args.command in {"fetch-lists", "fetch-surveillance"}
    http_client = client or (build_default_client() if owns_client else None)
    try:
        with PilotDataStore(args.root, workspace=args.workspace) as store:
            if args.command == "ingest-master":
                content = args.file.read_bytes()
                descriptor = SourceDescriptor(
                    source="local_file", kind="security_master", locator=args.file.name, fetched_at=utc_now(),
                )
                done = ingest_security_master(
                    store, descriptor, content, snapshot_date=args.snapshot_date,
                    snapshot_date_basis="operator_declared",
                )
                payload: dict = {
                    "snapshot_sha256": done.snapshot_sha256, "snapshot_date": done.snapshot_date.isoformat(),
                    "row_count": done.row_count, "skipped_rows": done.skipped_rows,
                    "changes": [c.model_dump(mode="json") for c in done.changes],
                }
            elif args.command == "fetch-lists":
                http = NseHttp(http_client, min_interval_seconds=args.min_interval_seconds)
                refs = [fetch_index_list(store, http, "nifty500"), fetch_index_list(store, http, "smallcap250")]
                payload = {"lists": [ref.model_dump(mode="json") for ref in refs]}
            elif args.command == "fetch-surveillance":
                http = NseHttp(http_client, min_interval_seconds=args.min_interval_seconds)
                asm, gsm = fetch_surveillance(store, http)
                payload = {"asm": asm.model_dump(mode="json"), "gsm": gsm.model_dump(mode="json")}
            else:
                result = build_target_universe(store, as_of=args.as_of, workspace=args.workspace)
                payload = {
                    "as_of": result.as_of.isoformat(), "target_sha256": result.target_sha256,
                    "members": len(result.members), "exclusions": len(result.exclusions),
                    "etf_rejected": len(result.etf_rejected),
                    "exclusions_by_reason": _count_reasons(result),
                }
    except PilotDataError as exc:
        print(json.dumps({"caveats": _caveat_payload(), "error_code": exc.code, "error": str(exc)}, indent=2))
        return 2
    finally:
        if owns_client and http_client is not None:
            http_client.close()
    print(json.dumps({"caveats": _caveat_payload(), "result": payload}, indent=2))
    return 0


def _count_reasons(result: TargetUniverseResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for exclusion in result.exclusions:
        counts[exclusion.reason] = counts.get(exclusion.reason, 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main())

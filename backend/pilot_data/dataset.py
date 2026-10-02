"""Immutable, hashed dataset snapshots of accepted bars with per-row provenance (PDAT-02, PDAT-04).

A snapshot is built only from a cross-check run whose Breeze series was not detected as
adjusted. Rows carry both layers: raw Breeze OHLCV for fills and back-adjusted values for
signals. Exports are read-only on disk and re-verifiable from their parquet bytes.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

import duckdb
from pydantic import BaseModel, ConfigDict

from .adjustment import (
    AdjustmentPolicy,
    adjusted_series,
    compute_factor_set,
    events_for_lineage,
)
from .core import (
    BREEZE_RAW_UNVERIFIED_CAVEAT,
    CaveatedResult,
    PilotDataError,
    canonical_sha256,
    dec_str,
    sha256_hex,
    standard_caveats,
    utc_naive,
)
from .crosscheck import CrossCheckReport, accepted_bars
from .history_quality import compute_span
from .lineage import build_lineage, primary_bars_for_lineage
from .models import RawDailyBar
from .store import PilotDataStore

SCHEMA_VERSION = "pilot-dataset/1"
ROW_SOURCE = "breeze_v2_1day_via_relay"
SNAPSHOTS_DDL = (
    "CREATE TABLE IF NOT EXISTS dataset_snapshots("
    "dataset_sha256 VARCHAR PRIMARY KEY, workspace VARCHAR NOT NULL, as_of DATE NOT NULL, "
    "crosscheck_run_id VARCHAR NOT NULL, manifest_json VARCHAR NOT NULL, created_at_utc TIMESTAMP NOT NULL, "
    "row_sha256 VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL)"
)
_DECIMAL_COLUMNS = ("raw_open", "raw_high", "raw_low", "raw_close", "adj_open", "adj_high", "adj_low", "adj_close")
_PARQUET_SQL = (
    "CREATE TEMP TABLE rows(workspace VARCHAR, anchor_isin VARCHAR, isin VARCHAR, nse_symbol VARCHAR, "
    "series VARCHAR, stock_code VARCHAR, trade_date DATE, raw_open DECIMAL(18,4), raw_high DECIMAL(18,4), "
    "raw_low DECIMAL(18,4), raw_close DECIMAL(18,4), raw_volume BIGINT, adj_open DECIMAL(18,4), "
    "adj_high DECIMAL(18,4), adj_low DECIMAL(18,4), adj_close DECIMAL(18,4), adj_volume BIGINT, "
    "adjusted_quarantined BOOLEAN, raw_adjustment_basis VARCHAR, adjusted_basis VARCHAR, source VARCHAR, "
    "source_sha256 VARCHAR, fetched_at_utc VARCHAR, bhavcopy_source_sha256 VARCHAR, crosscheck_run_id VARCHAR)"
)


def ensure_dataset_tables(store: PilotDataStore) -> None:
    store.ensure_table("dataset_snapshots", SNAPSHOTS_DDL, key_columns=("dataset_sha256",))


class DatasetRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: Literal["india"]
    anchor_isin: str
    isin: str
    nse_symbol: str
    series: str
    stock_code: str
    trade_date: date
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_close: Decimal
    raw_volume: int
    adj_open: Decimal | None
    adj_high: Decimal | None
    adj_low: Decimal | None
    adj_close: Decimal | None
    adj_volume: int | None
    adjusted_quarantined: bool
    raw_adjustment_basis: str
    adjusted_basis: str
    source: str
    source_sha256: str
    fetched_at_utc: str
    bhavcopy_source_sha256: str
    crosscheck_run_id: str

    def payload(self) -> dict[str, object]:
        """Scale-independent JSON form used for the dataset hash."""
        out: dict[str, object] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, Decimal):
                out[name] = dec_str(value)
            elif isinstance(value, date):
                out[name] = value.isoformat()
            else:
                out[name] = value
        return out


class DatasetManifest(CaveatedResult):
    schema_version: Literal["pilot-dataset/1"] = "pilot-dataset/1"
    dataset_sha256: str
    row_count: int
    anchor_count: int
    window_start: date
    window_end: date
    as_of: date
    crosscheck_run_id: str
    report_sha256: str
    targets_sha256: str
    lineage_hashes: dict[str, str]
    factor_set_hashes: dict[str, str]
    spans: dict[str, dict[str, object]]
    quarantine_totals: dict[str, int]
    rawness_counts: dict[str, int]
    created_at_utc: str
    parquet_sha256: str | None = None


def dataset_hash(rows: list[DatasetRow]) -> str:
    return canonical_sha256([row.payload() for row in rows])


def _write_parquet(rows: list[DatasetRow], path: Path) -> None:
    con = duckdb.connect(":memory:")
    try:
        con.execute(_PARQUET_SQL)
        names = list(DatasetRow.model_fields)
        con.executemany(
            f"INSERT INTO rows VALUES ({', '.join('?' for _ in names)})",
            [[getattr(row, name) for name in names] for row in rows],
        )
        con.execute(f"COPY (SELECT * FROM rows ORDER BY anchor_isin, trade_date) TO '{path}' (FORMAT PARQUET)")
    finally:
        con.close()


def _read_parquet(path: Path) -> list[DatasetRow]:
    con = duckdb.connect(":memory:")
    try:
        names = list(DatasetRow.model_fields)
        found = con.execute(
            f"SELECT {', '.join(names)} FROM read_parquet(?) ORDER BY anchor_isin, trade_date", [str(path)]
        ).fetchall()
    finally:
        con.close()
    return [DatasetRow(**dict(zip(names, values))) for values in found]


def build_dataset_snapshot(
    store: PilotDataStore,
    *,
    crosscheck_run_id: str,
    as_of: date,
    workspace: Literal["india"],
    export_root: Path | None,
) -> DatasetManifest:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    ensure_dataset_tables(store)
    found = store.query("SELECT result_json FROM crosscheck_runs WHERE run_id = ?", [crosscheck_run_id])
    if not found:
        raise PilotDataError("crosscheck_run_missing", "no cross-check run with that id")
    payload = json.loads(found[0][0])
    if "members" not in payload:
        raise PilotDataError("crosscheck_run_unsupported", "run is not a universe-wide cross-check")
    report = CrossCheckReport.model_validate(payload)
    if any(member.rawness_overall == "adjusted_detected" for member in report.members):
        raise PilotDataError("breeze_adjusted_detected", "Breeze history looks adjusted; no dataset is published")
    accepted = accepted_bars(store, crosscheck_run_id)
    if not accepted:
        raise PilotDataError("dataset_empty", "the cross-check run accepted no bars")
    policy = AdjustmentPolicy()
    members = {member.stock_code: member for member in report.members if member.status == "checked"}
    rows: list[DatasetRow] = []
    lineage_hashes: dict[str, str] = {}
    factor_hashes: dict[str, str] = {}
    spans: dict[str, dict[str, object]] = {}
    for stock_code in sorted({bar.stock_code for bar in accepted}):
        member = members[stock_code]
        mine = [bar for bar in accepted if bar.stock_code == stock_code]
        lineage = build_lineage(
            store, anchor_isin=member.anchor_isin, anchor_series=member.anchor_series, as_of=as_of,
            workspace=workspace, stock_code=stock_code,
        )
        if lineage.content_sha256() != member.lineage_sha256:
            raise PilotDataError("lineage_drift", f"lineage for {stock_code} differs from the cross-check run")
        reference = primary_bars_for_lineage(store, lineage, start=report.window_start, end=report.window_end)
        factor_set = compute_factor_set(
            lineage, reference, events_for_lineage(store, lineage), as_of=as_of, policy=policy
        )
        if factor_set.factor_set_sha256 != member.factor_set_sha256:
            raise PilotDataError("factor_set_drift", f"factor set for {stock_code} differs from the cross-check run")
        raw_bars: list[RawDailyBar] = []
        fetched: dict[str, str] = {}
        stored_rows = {
            (row[0], row[1]): row
            for row in store.query(
                "SELECT trade_date, source_sha256, open, high, low, close, volume FROM breeze_bars_raw "
                "WHERE stock_code = ? AND trade_date >= ? AND trade_date <= ?",
                [stock_code, report.window_start, report.window_end],
            )
        }
        for bar in mine:
            stored = stored_rows[(bar.trade_date, bar.breeze_source_sha256)][2:]
            raw_bars.append(
                RawDailyBar(
                    trade_date=bar.trade_date, isin=bar.isin, series=bar.series,
                    nse_symbol=lineage.symbol_on(bar.trade_date) or "", open=stored[0], high=stored[1],
                    low=stored[2], close=stored[3], volume=int(stored[4]), traded_value=None, source_kind="breeze",
                    source_sha256=bar.breeze_source_sha256,
                )
            )
            if bar.breeze_source_sha256 not in fetched:
                when = store.query(
                    "SELECT first_fetched_at_utc FROM source_files WHERE source_sha256 = ?", [bar.breeze_source_sha256]
                )[0][0]
                fetched[bar.breeze_source_sha256] = when.replace(tzinfo=timezone.utc).isoformat()
        series = adjusted_series(raw_bars, factor_set, as_of=as_of, workspace=workspace)
        basis = "as_traded_breeze_raw_confirmed" if member.rawness_overall == "raw_confirmed" \
            else "as_traded_breeze_unverified"
        by_day = {bar.trade_date: bar for bar in mine}
        symbols = {raw.trade_date: raw.nse_symbol for raw in raw_bars}
        for adj in series.bars:
            accepted_bar = by_day[adj.trade_date]
            rows.append(
                DatasetRow(
                    workspace="india", anchor_isin=member.anchor_isin, isin=adj.isin,
                    nse_symbol=symbols[adj.trade_date], series=accepted_bar.series, stock_code=stock_code, trade_date=adj.trade_date,
                    raw_open=adj.raw_open, raw_high=adj.raw_high, raw_low=adj.raw_low, raw_close=adj.raw_close,
                    raw_volume=adj.raw_volume, adj_open=adj.adj_open, adj_high=adj.adj_high, adj_low=adj.adj_low,
                    adj_close=adj.adj_close, adj_volume=adj.adj_volume, adjusted_quarantined=adj.adjusted_quarantined,
                    raw_adjustment_basis=basis, adjusted_basis=series.adjustment_basis, source=ROW_SOURCE,
                    source_sha256=accepted_bar.breeze_source_sha256,
                    fetched_at_utc=fetched[accepted_bar.breeze_source_sha256],
                    bhavcopy_source_sha256=accepted_bar.bhav_source_sha256, crosscheck_run_id=crosscheck_run_id,
                )
            )
        lineage_hashes[member.anchor_isin] = lineage.content_sha256()
        factor_hashes[member.anchor_isin] = factor_set.factor_set_sha256
        span = compute_span(store, lineage, window_start=report.window_start, window_end=report.window_end,
                            workspace=workspace)
        spans[member.anchor_isin] = {
            "first_session": span.first_session.isoformat() if span.first_session else None,
            "last_session": span.last_session.isoformat() if span.last_session else None,
            "short_history": span.short_history,
        }
    rows.sort(key=lambda row: (row.anchor_isin, row.trade_date))
    digest = dataset_hash(rows)
    all_raw = all(members[code].rawness_overall == "raw_confirmed" for code in {r.stock_code for r in rows})
    manifest = DatasetManifest(
        workspace="india", caveats=standard_caveats(*(() if all_raw else (BREEZE_RAW_UNVERIFIED_CAVEAT,))),
        dataset_sha256=digest, row_count=len(rows), anchor_count=len({row.anchor_isin for row in rows}),
        window_start=report.window_start, window_end=report.window_end, as_of=as_of,
        crosscheck_run_id=crosscheck_run_id, report_sha256=report.report_sha256, targets_sha256=report.targets_sha256,
        lineage_hashes=dict(sorted(lineage_hashes.items())), factor_set_hashes=dict(sorted(factor_hashes.items())),
        spans=dict(sorted(spans.items())), quarantine_totals=dict(report.totals_by_reason),
        rawness_counts=dict(report.rawness_counts),
        created_at_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    if export_root is not None:
        manifest = _export(rows, manifest, Path(export_root), workspace)
    store.append_rows(
        "dataset_snapshots",
        [
            {
                "dataset_sha256": digest, "workspace": store.workspace, "as_of": as_of,
                "crosscheck_run_id": crosscheck_run_id,
                "manifest_json": json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
                "created_at_utc": utc_naive(datetime.now(timezone.utc)), "row_sha256": digest,
                "source_sha256": report.report_sha256,
            }
        ],
        check="dataset_snapshot",
    )
    return manifest


def _export(rows: list[DatasetRow], manifest: DatasetManifest, export_root: Path, workspace: str) -> DatasetManifest:
    target = export_root / manifest.dataset_sha256
    if target.exists():
        return verify_dataset(target, workspace=workspace)  # never overwrite a published directory
    export_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=export_root, prefix=".staging-"))
    try:
        parquet = staging / "rows.parquet"
        _write_parquet(rows, parquet)
        published = manifest.model_copy(update={"parquet_sha256": sha256_hex(parquet.read_bytes())})
        (staging / "manifest.json").write_text(
            json.dumps(published.model_dump(mode="json"), sort_keys=True, indent=2), encoding="utf-8"
        )
        os.chmod(parquet, 0o444)
        os.chmod(staging / "manifest.json", 0o444)
        os.chmod(staging, 0o555)
        os.rename(staging, target)
    except BaseException:
        os.chmod(staging, 0o755)
        for leftover in staging.glob("*"):
            os.chmod(leftover, 0o644)
            leftover.unlink()
        staging.rmdir()
        raise
    return published


def verify_dataset(path: Path, *, workspace: str) -> DatasetManifest:
    path = Path(path)
    try:
        manifest = DatasetManifest.model_validate(json.loads((path / "manifest.json").read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise PilotDataError("dataset_integrity", "manifest.json is missing or unreadable") from exc
    if manifest.workspace != workspace:
        raise PilotDataError("workspace_mismatch", "dataset belongs to a different workspace")
    parquet = path / "rows.parquet"
    try:
        actual = sha256_hex(parquet.read_bytes())
    except OSError as exc:
        raise PilotDataError("dataset_integrity", "rows.parquet is missing") from exc
    if actual != manifest.parquet_sha256:
        raise PilotDataError("dataset_integrity", "rows.parquet differs from the hash in the manifest")
    try:
        rows = _read_parquet(parquet)
    except Exception as exc:  # corrupt content that still matched the hash cannot be trusted either
        raise PilotDataError("dataset_integrity", "rows.parquet could not be read") from exc
    if dataset_hash(rows) != manifest.dataset_sha256 or len(rows) != manifest.row_count:
        raise PilotDataError("dataset_integrity", "rows do not reproduce the dataset hash")
    return manifest


def _caveat_payload() -> list[dict[str, str]]:
    return [caveat.model_dump() for caveat in standard_caveats(BREEZE_RAW_UNVERIFIED_CAVEAT)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pilot_data.dataset", description="Build and verify dataset snapshots.")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--root", required=True, type=Path)
    build.add_argument("--workspace", required=True, choices=["india"])
    build.add_argument("--run-id", required=True)
    build.add_argument("--as-of", required=True, type=date.fromisoformat)
    build.add_argument("--export-root", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("--path", required=True, type=Path)
    verify.add_argument("--workspace", required=True, choices=["india"])
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            with PilotDataStore(args.root, workspace=args.workspace) as store:
                manifest = build_dataset_snapshot(
                    store, crosscheck_run_id=args.run_id, as_of=args.as_of, workspace=args.workspace,
                    export_root=args.export_root,
                )
        else:
            manifest = verify_dataset(args.path, workspace=args.workspace)
    except PilotDataError as exc:
        print(json.dumps({"caveats": _caveat_payload(), "error_code": exc.code, "error": str(exc)}, indent=2))
        return 2
    summary = {"dataset_sha256": manifest.dataset_sha256, "row_count": manifest.row_count,
               "anchor_count": manifest.anchor_count, "parquet_sha256": manifest.parquet_sha256}
    print(json.dumps({"caveats": [c.model_dump() for c in manifest.caveats], "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

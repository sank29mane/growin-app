"""Cross-check of Breeze daily bars against NSE bhavcopy.

The join key is (ISIN on that date, NSE series, trade date). Tolerances apply only after
the join, and only to raw prices (D-05, D-06). Nothing here picks a winner between
disagreeing sources: a disagreement becomes a quarantine record.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict

from .adjustment import (
    AdjustmentPolicy,
    FactorSet,
    RawnessReport,
    compute_factor_set,
    events_for_lineage,
    verify_source_unadjusted,
)
from .breeze_bars import BreezeBarsView, breeze_bars_for, ensure_breeze_tables
from .bhavcopy import ensure_bhavcopy_tables
from .core import (
    BREEZE_RAW_UNVERIFIED_CAVEAT,
    CaveatedResult,
    PilotDataError,
    canonical_sha256,
    standard_caveats,
    utc_naive,
)
from .lineage import lineage_for_target, primary_bars_for_lineage
from .models import CrossCheckTolerances, Lineage, QuarantineRecord, RawDailyBar
from .sessions import sessions_between
from .store import PilotDataStore
from .targets import TargetUniverseResult, latest_target_universe

RUNS_DDL = (
    "CREATE TABLE IF NOT EXISTS crosscheck_runs("
    "run_id VARCHAR PRIMARY KEY, workspace VARCHAR NOT NULL, started_at_utc TIMESTAMP NOT NULL, "
    "params_json VARCHAR NOT NULL, result_json VARCHAR NOT NULL, result_sha256 VARCHAR NOT NULL, "
    "source_sha256 VARCHAR NOT NULL, row_sha256 VARCHAR NOT NULL)"
)


def ensure_crosscheck_tables(store: PilotDataStore) -> None:
    ensure_bhavcopy_tables(store)
    ensure_breeze_tables(store)
    store.ensure_table("crosscheck_runs", RUNS_DDL, key_columns=("run_id",))


class AcceptedKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date
    isin: str
    series: str


class CrossCheckResult(CaveatedResult):
    run_id: str
    stock_code: str
    anchor_isin: str
    window_start: date
    window_end: date
    sessions_checked: int
    accepted: tuple[AcceptedKey, ...]
    quarantined_by_reason: dict[str, int]


def rel_diff_text(candidate: Decimal, reference: Decimal) -> str:
    return str((abs(candidate - reference) / reference).quantize(Decimal("0.000001")))


def exceeds(candidate: Decimal, reference: Decimal, max_rel: Decimal) -> bool:
    """True when |candidate - reference| / reference is strictly above max_rel (exact Decimal math)."""
    return abs(candidate - reference) > max_rel * reference


def crosscheck_window(
    store: PilotDataStore,
    *,
    lineage: Lineage,
    stock_code: str,
    series: str,
    sessions: Sequence[date],
    window_start: date,
    window_end: date,
    tolerances: CrossCheckTolerances,
    workspace: Literal["india"],
) -> CrossCheckResult:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    ensure_crosscheck_tables(store)
    ordered = tuple(sorted(sessions))
    if tuple(sessions) != ordered:
        raise PilotDataError("sessions_unsorted", "sessions must be an ascending tuple")
    bhav_files = {
        row[0]: row[1]
        for row in store.query(
            "SELECT trade_date, source_sha256 FROM bhavcopy_files WHERE file_kind IN ('udiff', 'cm_legacy') "
            "ORDER BY trade_date, source_sha256"
        )
    }
    missing = [day.isoformat() for day in ordered if day not in bhav_files]
    if missing:
        raise PilotDataError("calendar_incomplete", f"no primary bhavcopy ingested for: {missing[:10]}")
    bhav_hashes = sorted(
        row[0]
        for row in store.query(
            "SELECT source_sha256 FROM bhavcopy_files WHERE file_kind IN ('udiff', 'cm_legacy') "
            "AND trade_date >= ? AND trade_date <= ?",
            [ordered[0], ordered[-1]],
        )
    ) if ordered else []
    breeze_hashes = sorted(
        row[0]
        for row in store.query("SELECT source_sha256 FROM breeze_responses WHERE stock_code = ?", [stock_code])
    )
    run_id = canonical_sha256(
        {
            "lineage": lineage.content_sha256(),
            "stock_code": stock_code,
            "series": series,
            "sessions": [day.isoformat() for day in ordered],
            "window": [window_start.isoformat(), window_end.isoformat()],
            "tolerances": tolerances.model_dump(mode="json"),
            "inputs": sorted(set(bhav_hashes) | set(breeze_hashes)),
        }
    )

    records: list[QuarantineRecord] = []
    accepted: list[AcceptedKey] = []

    def quarantine(reason: str, day: date, isin: str | None, symbol: str | None,
                   detail: dict[str, str], evidence: tuple[str, ...]) -> None:
        records.append(
            QuarantineRecord(
                workspace="india", check="crosscheck", reason_code=reason, scope="raw", isin=isin,
                nse_symbol=symbol, stock_code=stock_code, series=series, date_from=day, date_to=day,
                detail=detail, evidence_sha256s=evidence, run_id=run_id,
            )
        )

    for day in ordered:
        isin = lineage.isin_on(day)
        if isin is None:
            quarantine("isin_unresolved", day, None, None, {}, ())
            continue
        bhav_rows = store.query(
            "SELECT file_kind, nse_symbol, open, high, low, close, source_sha256, row_sha256 FROM bhavcopy_bars "
            "WHERE trade_date = ? AND isin = ? AND series = ? AND file_kind IN ('udiff', 'cm_legacy') "
            "ORDER BY CASE file_kind WHEN 'udiff' THEN 0 ELSE 1 END",
            [day, isin, series],
        )
        bhav = bhav_rows[0] if bhav_rows else None
        breeze_rows = store.query(
            "SELECT open, high, low, close, source_sha256, row_sha256 FROM breeze_bars_raw "
            "WHERE stock_code = ? AND trade_date = ? ORDER BY source_sha256",
            [stock_code, day],
        )
        symbol = bhav[1] if bhav else lineage.symbol_on(day)
        if len({row[5] for row in breeze_rows}) > 1:
            quarantine(
                "breeze_duplicate_conflict", day, isin, symbol, {},
                tuple(row[4] for row in breeze_rows),
            )
            continue
        breeze = breeze_rows[0] if breeze_rows else None
        if bhav and breeze:
            evidence = (bhav[6], breeze[4])
            bhav_o, bhav_h, bhav_l, bhav_c = bhav[2], bhav[3], bhav[4], bhav[5]
            brz_o, brz_h, brz_l, brz_c = breeze[0], breeze[1], breeze[2], breeze[3]
            bad = False
            if exceeds(brz_c, bhav_c, tolerances.close_max_rel):
                bad = True
                quarantine(
                    "close_tolerance", day, isin, symbol,
                    {"field": "close", "rel_close": rel_diff_text(brz_c, bhav_c)}, evidence,
                )
            ohl = {
                name: (b, x)
                for name, b, x in (("open", brz_o, bhav_o), ("high", brz_h, bhav_h), ("low", brz_l, bhav_l))
                if exceeds(b, x, tolerances.ohl_max_rel)
            }
            if ohl:
                bad = True
                detail = {"fields": ",".join(sorted(ohl))}
                for name, (b, x) in ohl.items():
                    detail[f"rel_{name}"] = rel_diff_text(b, x)
                quarantine("ohl_tolerance", day, isin, symbol, detail, evidence)
            if not bad:
                accepted.append(AcceptedKey(trade_date=day, isin=isin, series=series))
        elif bhav:
            quarantine("one_sided_bhavcopy", day, isin, symbol, {}, (bhav[6],))
        elif breeze:
            quarantine("one_sided_breeze", day, isin, symbol, {}, (breeze[4],))

    session_set = set(ordered)
    for row in store.query(
        "SELECT DISTINCT trade_date, source_sha256 FROM breeze_bars_raw WHERE stock_code = ? "
        "AND trade_date >= ? AND trade_date <= ? ORDER BY trade_date",
        [stock_code, window_start, window_end],
    ):
        if row[0] not in session_set:
            quarantine("breeze_bar_on_non_session", row[0], lineage.isin_on(row[0]), lineage.symbol_on(row[0]),
                       {}, (row[1],))

    by_reason: dict[str, int] = {}
    for record in records:
        by_reason[record.reason_code] = by_reason.get(record.reason_code, 0) + 1
    result = CrossCheckResult(
        workspace="india",
        caveats=standard_caveats(BREEZE_RAW_UNVERIFIED_CAVEAT),
        run_id=run_id,
        stock_code=stock_code,
        anchor_isin=lineage.anchor_isin,
        window_start=window_start,
        window_end=window_end,
        sessions_checked=len(ordered),
        accepted=tuple(accepted),
        quarantined_by_reason=dict(sorted(by_reason.items())),
    )
    store.record_quarantine(records)
    result_hash = result.content_sha256()
    store.append_rows(
        "crosscheck_runs",
        [
            {
                "run_id": run_id,
                "workspace": store.workspace,
                "started_at_utc": utc_naive(datetime.now(timezone.utc)),
                "params_json": json.dumps(
                    {"stock_code": stock_code, "series": series, "tolerances": tolerances.model_dump(mode="json")},
                    sort_keys=True,
                ),
                "result_json": json.dumps(result.model_dump(mode="json"), sort_keys=True),
                "result_sha256": result_hash,
                "source_sha256": run_id,
                "row_sha256": result_hash,
            }
        ],
        check="crosscheck_run",
    )
    return result


# =========================================================================== universe-wide cross-check
ACCEPTED_DDL = (
    "CREATE TABLE IF NOT EXISTS crosscheck_accepted("
    "run_id VARCHAR NOT NULL, stock_code VARCHAR NOT NULL, trade_date DATE NOT NULL, isin VARCHAR NOT NULL, "
    "series VARCHAR NOT NULL, breeze_row_sha256 VARCHAR NOT NULL, bhav_row_sha256 VARCHAR NOT NULL, "
    "breeze_source_sha256 VARCHAR NOT NULL, bhav_source_sha256 VARCHAR NOT NULL, row_sha256 VARCHAR NOT NULL, "
    "source_sha256 VARCHAR NOT NULL, PRIMARY KEY(run_id, stock_code, trade_date))"
)
UPSTREAM_EXCLUDED_CHECKS = ("crosscheck", "rawness")


def ensure_universe_crosscheck_tables(store: PilotDataStore) -> None:
    ensure_crosscheck_tables(store)
    store.ensure_table("crosscheck_accepted", ACCEPTED_DDL, key_columns=("run_id", "stock_code", "trade_date"))


class MemberCrossCheck(CaveatedResult):
    stock_code: str
    anchor_isin: str
    anchor_series: str
    status: Literal["checked", "not_fetched"]
    accepted_count: int
    excluded_by_prior_quarantine: int
    quarantined_by_reason: dict[str, int]
    rawness_overall: Literal["raw_confirmed", "adjusted_detected", "unverified"] | None
    lineage_sha256: str | None
    factor_set_sha256: str | None


class CrossCheckReport(CaveatedResult):
    run_id: str
    targets_sha256: str
    window_start: date
    window_end: date
    as_of: date
    members: tuple[MemberCrossCheck, ...]
    totals_by_reason: dict[str, int]
    rawness_counts: dict[str, int]
    accepted_total: int
    report_sha256: str


class AcceptedBar(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stock_code: str
    trade_date: date
    isin: str
    series: str
    breeze_row_sha256: str
    bhav_row_sha256: str
    breeze_source_sha256: str
    bhav_source_sha256: str


@dataclass
class _Prepared:
    member: object
    lineage: Lineage
    view: BreezeBarsView
    factor_set: FactorSet
    rawness: RawnessReport


def _upstream_quarantine_hash(store: PilotDataStore) -> str:
    ids = [
        row[0]
        for row in store.query(
            "SELECT quarantine_id FROM quarantine_records WHERE scope IN ('raw', 'both') "
            "AND check_name NOT IN ('crosscheck', 'rawness') ORDER BY quarantine_id"
        )
    ]
    return canonical_sha256(ids)


def run_crosscheck(
    store: PilotDataStore,
    *,
    targets: TargetUniverseResult,
    window_start: date,
    window_end: date,
    as_of: date,
    tolerances: CrossCheckTolerances,
    workspace: Literal["india"],
) -> CrossCheckReport:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    ensure_universe_crosscheck_tables(store)
    sessions = sessions_between(store, window_start, window_end)
    session_set = set(sessions)
    udiff_dates = {
        row[0]
        for row in store.query(
            "SELECT DISTINCT trade_date FROM bhavcopy_files WHERE file_kind = 'udiff' "
            "AND trade_date >= ? AND trade_date <= ?", [window_start, window_end]
        )
    }
    bhav_hashes = sorted(
        row[0]
        for row in store.query(
            "SELECT source_sha256 FROM bhavcopy_files WHERE file_kind IN ('udiff', 'cm_legacy') "
            "AND trade_date >= ? AND trade_date <= ?", [window_start, window_end]
        )
    )
    policy = AdjustmentPolicy()
    prepared: list[_Prepared] = []
    not_fetched: list[object] = []
    for member in targets.members:
        view = breeze_bars_for(store, member.stock_code, start=window_start, end=window_end)
        if not any(view.covers(day) for day in sessions):
            not_fetched.append(member)
            continue
        lineage = lineage_for_target(store, member, as_of=as_of, workspace=workspace)
        reference = primary_bars_for_lineage(store, lineage, start=window_start, end=window_end)
        candidate = []
        for bar in view.bars:
            isin = lineage.isin_on(bar.trade_date)
            if isin is None:
                continue
            candidate.append(
                RawDailyBar(
                    trade_date=bar.trade_date, isin=isin, series=member.anchor_series,
                    nse_symbol=lineage.symbol_on(bar.trade_date) or member.nse_symbol, open=bar.open, high=bar.high,
                    low=bar.low, close=bar.close, volume=bar.volume, traded_value=None, source_kind="breeze",
                    source_sha256=bar.source_sha256,
                )
            )
        factor_set = compute_factor_set(
            lineage, reference, events_for_lineage(store, lineage), as_of=as_of, policy=policy
        )
        rawness = verify_source_unadjusted(
            reference, candidate, factor_set, source_label="breeze_v2_1day", workspace=workspace
        )
        prepared.append(_Prepared(member, lineage, view, factor_set, rawness))
    breeze_hashes = sorted(
        {
            row[0]
            for item in prepared
            for row in store.query(
                "SELECT source_sha256 FROM breeze_responses WHERE stock_code = ?", [item.member.stock_code]
            )
        }
    )
    run_id = canonical_sha256(
        {
            "targets": targets.target_sha256,
            "lineages": sorted(item.lineage.content_sha256() for item in prepared),
            "factor_sets": sorted(item.factor_set.factor_set_sha256 for item in prepared),
            "tolerances": tolerances.model_dump(mode="json"),
            "window": [window_start.isoformat(), window_end.isoformat()],
            "as_of": as_of.isoformat(),
            "breeze_responses": breeze_hashes,
            "bhavcopy_files": bhav_hashes,
            "upstream_quarantines": _upstream_quarantine_hash(store),
        }
    )

    records: list[QuarantineRecord] = []
    accepted_rows: list[dict[str, object]] = []
    members_out: list[MemberCrossCheck] = []
    for member in not_fetched:
        members_out.append(
            MemberCrossCheck(
                workspace="india", caveats=standard_caveats(), stock_code=member.stock_code,
                anchor_isin=member.anchor_isin, anchor_series=member.anchor_series, status="not_fetched",
                accepted_count=0, excluded_by_prior_quarantine=0, quarantined_by_reason={}, rawness_overall=None,
                lineage_sha256=None, factor_set_sha256=None,
            )
        )
    for item in prepared:
        member, lineage, view = item.member, item.lineage, item.view
        series = member.anchor_series
        isins = sorted({segment.isin for segment in lineage.segments})
        marks = ", ".join("?" for _ in isins)
        bhav_by_day: dict[date, list[tuple]] = {}
        for row in store.query(
            "SELECT trade_date, file_kind, series, nse_symbol, open, high, low, close, source_sha256, row_sha256, isin "
            f"FROM bhavcopy_bars WHERE isin IN ({marks}) AND file_kind IN ('udiff', 'cm_legacy') "
            "AND trade_date >= ? AND trade_date <= ? ORDER BY trade_date, series",
            [*isins, window_start, window_end],
        ):
            if (row[1] == "udiff") == (row[0] in udiff_dates):
                bhav_by_day.setdefault(row[0], []).append(row)
        prior_ranges = [
            (low, high)
            for low, high in store.query(
                f"SELECT date_from, date_to FROM quarantine_records WHERE scope IN ('raw', 'both') "
                f"AND check_name NOT IN ('crosscheck', 'rawness') AND (stock_code = ? OR isin IN ({marks}))",
                [member.stock_code, *isins],
            )
            if low is not None and high is not None
        ]
        reasons: dict[str, int] = {}
        accepted = excluded = 0

        def quarantine(reason: str, day: date, isin: str | None, symbol: str | None, detail: dict[str, str],
                       evidence: tuple[str, ...]) -> None:
            reasons[reason] = reasons.get(reason, 0) + 1
            records.append(
                QuarantineRecord(
                    workspace="india", check="crosscheck", reason_code=reason, scope="raw", isin=isin,
                    nse_symbol=symbol, stock_code=member.stock_code, series=series, date_from=day, date_to=day,
                    detail=detail, evidence_sha256s=evidence, run_id=run_id,
                )
            )

        for day in sessions:
            if not view.covers(day):
                continue
            isin = lineage.isin_on(day)
            if isin is None:
                quarantine("isin_unresolved", day, None, None, {}, ())
                continue
            symbol = lineage.symbol_on(day)
            conflict = view.conflict_on(day)
            if conflict is not None:
                quarantine("breeze_duplicate_conflict", day, isin, symbol, {}, conflict.source_sha256s)
                continue
            breeze = view.bar_on(day)
            rows = [row for row in bhav_by_day.get(day, []) if row[10] == isin]
            matching = [row for row in rows if row[2] == series]
            if breeze is not None and not matching and rows:
                quarantine("series_mismatch", day, isin, symbol, {"bhavcopy_series": rows[0][2]},
                           (rows[0][8], breeze.source_sha256))
                continue
            bhav = matching[0] if matching else None
            if bhav is None and breeze is None:
                continue
            if bhav is not None and breeze is None:
                quarantine("one_sided_bhavcopy", day, isin, symbol, {}, (bhav[8],))
                continue
            if breeze is not None and bhav is None:
                quarantine("one_sided_breeze", day, isin, symbol, {}, (breeze.source_sha256,))
                continue
            evidence = (bhav[8], breeze.source_sha256)
            bad = False
            if exceeds(breeze.close, bhav[7], tolerances.close_max_rel):
                bad = True
                quarantine("close_tolerance", day, isin, symbol,
                           {"field": "close", "rel_close": rel_diff_text(breeze.close, bhav[7])}, evidence)
            ohl = {
                name: (b, x)
                for name, b, x in (("open", breeze.open, bhav[4]), ("high", breeze.high, bhav[5]),
                                   ("low", breeze.low, bhav[6]))
                if exceeds(b, x, tolerances.ohl_max_rel)
            }
            if ohl:
                bad = True
                detail = {"fields": ",".join(sorted(ohl))}
                for name, (b, x) in ohl.items():
                    detail[f"rel_{name}"] = rel_diff_text(b, x)
                quarantine("ohl_tolerance", day, isin, symbol, detail, evidence)
            if bad:
                continue
            if any(low <= day <= high for low, high in prior_ranges):
                excluded += 1
                continue
            accepted += 1
            row_hash = canonical_sha256(
                {"stock_code": member.stock_code, "date": day.isoformat(), "breeze": breeze.row_sha256, "bhav": bhav[9]}
            )
            accepted_rows.append(
                {
                    "run_id": run_id, "stock_code": member.stock_code, "trade_date": day, "isin": isin,
                    "series": series, "breeze_row_sha256": breeze.row_sha256, "bhav_row_sha256": bhav[9],
                    "breeze_source_sha256": breeze.source_sha256, "bhav_source_sha256": bhav[8],
                    "row_sha256": row_hash, "source_sha256": run_id,
                }
            )
        for bar in view.bars:
            if window_start <= bar.trade_date <= window_end and bar.trade_date not in session_set:
                quarantine("breeze_bar_on_non_session", bar.trade_date, lineage.isin_on(bar.trade_date),
                           lineage.symbol_on(bar.trade_date), {}, (bar.source_sha256,))
        for verdict in item.rawness.verdicts:
            if verdict.verdict == "adjusted_detected":
                reasons["breeze_series_adjusted"] = reasons.get("breeze_series_adjusted", 0) + 1
                records.append(
                    QuarantineRecord(
                        workspace="india", check="rawness", reason_code="breeze_series_adjusted", scope="raw",
                        isin=lineage.anchor_isin, nse_symbol=lineage.segments[-1].nse_symbol,
                        stock_code=member.stock_code, series=series, date_from=window_start,
                        date_to=verdict.ex_date - timedelta(days=1),
                        detail={"event_id": verdict.event_id, "factor": str(verdict.factor),
                                "pre_ratio": str(verdict.pre_ratio)},
                        evidence_sha256s=(item.factor_set.factor_set_sha256,), run_id=run_id,
                    )
                )
        members_out.append(
            MemberCrossCheck(
                workspace="india", caveats=standard_caveats(), stock_code=member.stock_code,
                anchor_isin=member.anchor_isin, anchor_series=series, status="checked", accepted_count=accepted,
                excluded_by_prior_quarantine=excluded, quarantined_by_reason=dict(sorted(reasons.items())),
                rawness_overall=item.rawness.overall, lineage_sha256=lineage.content_sha256(),
                factor_set_sha256=item.factor_set.factor_set_sha256,
            )
        )
    members_out.sort(key=lambda m: m.stock_code)
    totals: dict[str, int] = {}
    rawness_counts: dict[str, int] = {}
    for member_out in members_out:
        for reason, count in member_out.quarantined_by_reason.items():
            totals[reason] = totals.get(reason, 0) + count
        if member_out.rawness_overall is not None:
            rawness_counts[member_out.rawness_overall] = rawness_counts.get(member_out.rawness_overall, 0) + 1
    checked = [m for m in members_out if m.status == "checked"]
    all_raw = bool(checked) and all(m.rawness_overall == "raw_confirmed" for m in checked)
    caveats = standard_caveats(*(() if all_raw else (BREEZE_RAW_UNVERIFIED_CAVEAT,)))
    fields = {
        "run_id": run_id, "targets_sha256": targets.target_sha256, "window_start": window_start,
        "window_end": window_end, "as_of": as_of, "members": tuple(members_out),
        "totals_by_reason": dict(sorted(totals.items())), "rawness_counts": dict(sorted(rawness_counts.items())),
        "accepted_total": sum(m.accepted_count for m in members_out),
    }
    digest = canonical_sha256(
        {
            "run_id": run_id, "targets": targets.target_sha256, "window": [window_start.isoformat(), window_end.isoformat()],
            "as_of": as_of.isoformat(), "members": [m.model_dump(mode="json") for m in members_out],
            "totals": fields["totals_by_reason"], "rawness": fields["rawness_counts"],
            "caveats": [c.code for c in caveats],
        }
    )
    report = CrossCheckReport(workspace="india", caveats=caveats, report_sha256=digest, **fields)
    store.record_quarantine(records)
    store.append_rows("crosscheck_accepted", accepted_rows, check="crosscheck_accepted")
    store.append_rows(
        "crosscheck_runs",
        [
            {
                "run_id": run_id, "workspace": store.workspace, "started_at_utc": utc_naive(datetime.now(timezone.utc)),
                "params_json": json.dumps(
                    {"window": [window_start.isoformat(), window_end.isoformat()], "as_of": as_of.isoformat(),
                     "tolerances": tolerances.model_dump(mode="json"), "targets": targets.target_sha256},
                    sort_keys=True,
                ),
                "result_json": json.dumps(report.model_dump(mode="json"), sort_keys=True),
                "result_sha256": digest, "source_sha256": run_id, "row_sha256": digest,
            }
        ],
        check="crosscheck_run",
    )
    return report


def accepted_bars(store: PilotDataStore, run_id: str) -> tuple[AcceptedBar, ...]:
    ensure_universe_crosscheck_tables(store)
    rows = store.query(
        "SELECT stock_code, trade_date, isin, series, breeze_row_sha256, bhav_row_sha256, breeze_source_sha256, "
        "bhav_source_sha256 FROM crosscheck_accepted WHERE run_id = ? ORDER BY stock_code, trade_date",
        [run_id],
    )
    return tuple(AcceptedBar(**dict(zip(AcceptedBar.model_fields, row))) for row in rows)


def _caveat_payload() -> list[dict[str, str]]:
    return [caveat.model_dump() for caveat in standard_caveats(BREEZE_RAW_UNVERIFIED_CAVEAT)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pilot_data.crosscheck", description="Cross-check Breeze bars against bhavcopy.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--root", required=True, type=Path)
    run.add_argument("--workspace", required=True, choices=["india"])
    run.add_argument("--as-of", required=True, type=date.fromisoformat)
    run.add_argument("--window-start", required=True, type=date.fromisoformat)
    run.add_argument("--window-end", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    try:
        with PilotDataStore(args.root, workspace=args.workspace) as store:
            targets = latest_target_universe(store, workspace=args.workspace)
            if targets is None:
                raise PilotDataError("target_universe_missing", "build the target universe first")
            report = run_crosscheck(
                store, targets=targets, window_start=args.window_start, window_end=args.window_end,
                as_of=args.as_of, tolerances=CrossCheckTolerances(), workspace=args.workspace,
            )
    except PilotDataError as exc:
        print(json.dumps({"caveats": _caveat_payload(), "error_code": exc.code, "error": str(exc)}, indent=2))
        return 2
    summary = {
        "run_id": report.run_id, "report_sha256": report.report_sha256, "accepted_total": report.accepted_total,
        "totals_by_reason": report.totals_by_reason, "rawness_counts": report.rawness_counts,
        "members_checked": sum(1 for m in report.members if m.status == "checked"),
        "members_not_fetched": sum(1 for m in report.members if m.status == "not_fetched"),
    }
    print(json.dumps({"caveats": [c.model_dump() for c in report.caveats], "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

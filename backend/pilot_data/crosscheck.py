"""Cross-check of Breeze daily bars against NSE bhavcopy.

The join key is (ISIN on that date, NSE series, trade date). Tolerances apply only after
the join, and only to raw prices (D-05, D-06). Nothing here picks a winner between
disagreeing sources: a disagreement becomes a quarantine record.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict

from .breeze_bars import ensure_breeze_tables
from .bhavcopy import ensure_bhavcopy_tables
from .core import (
    BREEZE_RAW_UNVERIFIED_CAVEAT,
    CaveatedResult,
    PilotDataError,
    canonical_sha256,
    standard_caveats,
    utc_naive,
)
from .models import CrossCheckTolerances, Lineage, QuarantineRecord
from .store import PilotDataStore

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

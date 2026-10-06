"""Shared synthetic builders for the strategy_india tests. Not a test module (it holds no tests).

Every number here is synthetic. Nothing reads private/ or the real pilot data store. Names that
GATE-02 scans for are never written out: the broker SDK name is built from fragments where needed.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from costs.fills import FillScenarioSet, load_fill_scenarios
from costs.schedule import PricingBasis, ScheduleSet, load_schedule_set
from pilot_data.dataset import DatasetRow
from pilot_data.price_bands import BandCoverageReport, BandObservation, UnavailableBand, write_coverage_report
from pilot_data.core import standard_caveats
from pilot_data.universe import UniversePolicy
from private_config.schemas import IndiaLimits

from strategy_india.data import (
    DatasetView,
    DividendEvents,
    EligibilitySnapshot,
)
from strategy_india.engine import RunContext
from strategy_india.ticks import TickTables as _TT  # noqa: F401
from strategy_india.holdout import HoldoutRange
from strategy_india.ticks import EQUITY, NON_GOLD_ETF, TickTables, load_default_tables, load_table
from strategy_india.gate import recompute_report_sha256
from strategy_india.params import StrategyParams, parse_params, placeholder_params

SCHEDULE_VERSION = "icici-prime9999-ivalue-nse-cash-2024-10-01.r1"
SESSION_START = date(2025, 5, 1)  # after the 2025-04-15 tick revision, so ticks resolve


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def weekday_sessions(start: date, count: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


@dataclass(frozen=True)
class NameSpec:
    anchor: str
    code: str
    drift_bps: int = 0
    vol_bps: int = 120
    start_price: Decimal = Decimal("500")
    volume: int = 2_000_000
    isin: str | None = None


def default_names(n: int = 12) -> list[NameSpec]:
    names = []
    for i in range(n):
        drift = 60 - 12 * i  # a clear cross-section: early names trend up, late names trend down
        names.append(NameSpec(f"INE000A01{i:03d}", f"STK{i:02d}", drift_bps=drift))
    return names


ETF_ISINS = ("INF000000ETF1", "INF000000ETF2")


def etf_names() -> list[NameSpec]:
    return [
        NameSpec(ETF_ISINS[0], "ETFONE", drift_bps=5, vol_bps=60, start_price=Decimal("250"), volume=9_000_000),
        NameSpec(ETF_ISINS[1], "ETFTWO", drift_bps=5, vol_bps=60, start_price=Decimal("120"), volume=900_000),
    ]


def make_rows(
    sessions: Sequence[date],
    names: Sequence[NameSpec],
    *,
    seed: str = "synthetic",
    ex_gaps: Mapping[tuple[str, date], Decimal] | None = None,
    quarantined: Iterable[tuple[str, date]] = (),
    drop: Iterable[tuple[str, date]] = (),
) -> list[DatasetRow]:
    """Deterministic daily bars. ``ex_gaps`` puts a price gap at a session open (raw and adjusted alike)."""
    gaps = dict(ex_gaps or {})
    quarantine = set(quarantined)
    dropped = set(drop)
    rows: list[DatasetRow] = []
    for spec in names:
        rng = random.Random(f"{seed}-{spec.anchor}")
        close = spec.start_price
        for day in sessions:
            gap = gaps.get((spec.anchor, day), Decimal(0))
            opened = (close * (1 + gap)).quantize(Decimal("0.01"))
            move = Decimal(spec.drift_bps + rng.randint(-spec.vol_bps, spec.vol_bps)) / Decimal(10000)
            new_close = (opened * (1 + move)).quantize(Decimal("0.01"))
            low = (min(opened, new_close) * Decimal("0.997")).quantize(Decimal("0.01"))
            high = (max(opened, new_close) * Decimal("1.003")).quantize(Decimal("0.01"))
            close = new_close
            if (spec.anchor, day) in dropped:
                continue
            q = (spec.anchor, day) in quarantine
            adj = (None, None, None, None) if q else (opened, high, low, new_close)
            rows.append(
                DatasetRow(
                    workspace="india", anchor_isin=spec.anchor, isin=spec.isin or spec.anchor, nse_symbol=spec.code,
                    series="EQ", stock_code=spec.code, trade_date=day, raw_open=opened, raw_high=high, raw_low=low,
                    raw_close=new_close, raw_volume=spec.volume, adj_open=adj[0], adj_high=adj[1], adj_low=adj[2],
                    adj_close=adj[3], adj_volume=None if q else spec.volume, adjusted_quarantined=q,
                    raw_adjustment_basis="as_traded_breeze_raw_confirmed", adjusted_basis="synthetic",
                    source="breeze_v2_1day_via_relay", source_sha256=sha("src"), fetched_at_utc="2026-10-01T00:00:00+00:00",
                    bhavcopy_source_sha256=sha("bhav"), crosscheck_run_id="synthetic-run",
                )
            )
    return rows


def limits(**overrides: str) -> IndiaLimits:
    base = {
        "schema_version": 1, "workspace": "india", "currency": "INR", "capital_cap": "50000",
        "per_position_cap": "10000", "drawdown_halt": "-0.08", "drawdown_flatten": "-0.15", "position_stop": "-0.12",
    }
    base.update(overrides)
    return IndiaLimits.model_validate(base)


def params(**overrides) -> StrategyParams:
    raw = placeholder_params()
    raw.update(overrides)
    return parse_params(raw)


class StaticBands:
    """Every (isin, session) is a 20 percent fixed band unless listed as unknown."""

    def __init__(self, unknown: Iterable[tuple[str, date]] = (), no_band: Iterable[tuple[str, date]] = ()) -> None:
        self.unknown = set(unknown)
        self.no_band = set(no_band)
        self.calls: list[tuple[str, date]] = []

    def observe(self, isin: str, session: date) -> BandObservation:
        self.calls.append((isin, session))
        base = dict(isin=isin, session=session, nse_symbol="X", series="EQ", source_sha256s=(sha("band"),))
        if (isin, session) in self.unknown:
            return BandObservation(status="unknown", percent=None, source_kind=None, reason="band_crosscheck_row_conflict", **base)
        if (isin, session) in self.no_band:
            return BandObservation(status="no_band", percent=None, source_kind="list", reason=None, **base)
        return BandObservation(status="fixed", percent=Decimal("20"), source_kind="list", reason=None, **base)


class StaticEligibility:
    def __init__(self, eligible: Iterable[str], smallcap: Mapping[str, str] | None = None) -> None:
        self.eligible = frozenset(eligible)
        self.smallcap = dict(smallcap or {})
        self.calls: list[date] = []

    def snapshot(self, as_of: date) -> EligibilitySnapshot:
        self.calls.append(as_of)
        classes = {a: self.smallcap.get(a, "not_small") for a in self.eligible}
        return EligibilitySnapshot(as_of, self.eligible, classes, sha(f"elig-{as_of}"))


def coverage_report(
    start: date,
    end: date,
    *,
    unavailable: Sequence[tuple[str, str, date]] = (),
    blocked: Sequence[str] = (),
    digest: str | None = None,
) -> BandCoverageReport:
    """A synthetic, internally consistent band coverage report. ``unavailable`` holds (stock_code, isin, session)."""
    items = tuple(
        UnavailableBand(stock_code=code, isin=isin, session=day, nse_symbol=code, series="EQ",
                        reason="band_crosscheck_row_conflict", source_sha256s=(sha("band"),))
        for code, isin, day in unavailable
    )
    report = BandCoverageReport(
        workspace="india", caveats=standard_caveats(), period_start=start, period_end=end, sessions=100,
        sessions_by_status={"list": 100}, unsupported_sessions=(), targets_checked=10, target_unknown_counts={},
        fixed_count=900, no_band_count=0, unknown_count=len(items),
        unknown_by_reason={"band_crosscheck_row_conflict": len(items)} if items else {},
        convention=None, archive_depth=None, phase62_blocked=bool(blocked), blocked_reasons=tuple(blocked),
        row_conflict_sessions=(), nonblocking_reasons=(), unavailable_bands=items,
        report_sha256="0" * 64,
    )
    real = recompute_report_sha256(report, TARGETS_SHA)  # the way 59 derives it
    return report.model_copy(update={"report_sha256": digest or real})


def write_coverage(root: Path, report: BandCoverageReport) -> Path:
    return write_coverage_report(root, report)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "strategy_india"
TARGETS_SHA = sha("synthetic-target-universe")


def default_criteria() -> dict:
    """The tracked EXAMPLE D-19 criteria (a fixture, not a default in code)."""
    import json

    return json.loads((FIXTURE_DIR / "d19_criteria_example.json").read_text())


def tick_tables(with_etf: bool = True) -> TickTables:
    """Equity from the encoded table. The ETF class gets its OWN synthetic table, never an alias of the equity one."""
    tables = {EQUITY: load_default_tables().table_for(EQUITY)}
    if with_etf:
        tables[NON_GOLD_ETF] = load_table(FIXTURE_DIR / "synthetic_etf_tick_table.json")
    return TickTables(tables)


def costs_inputs() -> tuple[FillScenarioSet, ScheduleSet, TickTables, PricingBasis]:
    return load_fill_scenarios(), load_schedule_set(), tick_tables(), PricingBasis.pinned(SCHEDULE_VERSION)


def make_context(
    rows: Sequence[DatasetRow],
    holdout: HoldoutRange,
    *,
    eligible: Iterable[str] | None = None,
    smallcap: Mapping[str, str] | None = None,
    params_obj: StrategyParams | None = None,
    limits_obj: IndiaLimits | None = None,
    bands: StaticBands | None = None,
    unavailable: Mapping[tuple[str, date], str] | None = None,
    events: DividendEvents | None = None,
    view: DatasetView | None = None,
    eligibility: StaticEligibility | None = None,
) -> RunContext:
    scenarios, schedules, ticks, basis = costs_inputs()
    names = sorted({row.anchor_isin for row in rows} - set(ETF_ISINS))
    return RunContext(
        view=view if view is not None else DatasetView.from_rows(rows, holdout=holdout),
        params=params_obj or params(),
        limits=limits_obj or limits(),
        eligibility=eligibility or StaticEligibility(eligible if eligible is not None else names, smallcap),
        universe_policy=UniversePolicy(),
        bands=bands or StaticBands(),
        unavailable=dict(unavailable or {}),
        events=events or DividendEvents(),
        scenarios=scenarios, schedules=schedules, pricing_basis=basis, ticks=ticks,
    )


GIT_COMMIT = "b" * 40


def study_inputs(
    tmp_path: Path,
    *,
    sessions_n: int = 400,
    holdout_sessions: int = 60,
    n_names: int = 10,
    unavailable: Sequence[tuple[str, str, date]] = (),
    events: DividendEvents | None = None,
    ex_gaps: Mapping[tuple[str, date], Decimal] | None = None,
    tri: tuple[Path, str] | None = None,
    params_overrides: Mapping | None = None,
    provider=None,
    rows=None,
    bands: StaticBands | None = None,
    registry_name: str = "registry.jsonl",
    ticks: TickTables | None = None,
    start: date = SESSION_START,
    criteria: Mapping | None = None,
):
    """A complete synthetic study: dataset, gate report, registry path, every 60 input."""
    from pilot_data.dataset import dataset_hash
    from strategy_india.folds import FoldRules
    from strategy_india.registry import Registry
    from strategy_india.study import StudyInputs

    sessions = weekday_sessions(start, sessions_n)
    names = default_names(n_names) + etf_names()
    rows = rows if rows is not None else make_rows(sessions, names, ex_gaps=ex_gaps)
    cov_root = tmp_path / "cov"
    cov_path = write_coverage(cov_root, coverage_report(sessions[0], sessions[-1], unavailable=unavailable))
    scenarios, schedules, tick_obj, _ = costs_inputs()
    raw = placeholder_params()
    raw.update(params_overrides or {})
    eligible = sorted({row.anchor_isin for row in rows} - set(ETF_ISINS))
    return StudyInputs(
        rows=rows, dataset_sha256=dataset_hash(sorted(rows, key=lambda r: (r.anchor_isin, r.trade_date))),
        params_raw=raw, limits=limits(), coverage_path=cov_path, eligibility=StaticEligibility(eligible),
        universe_policy=UniversePolicy(), bands=bands or StaticBands(), scenarios=scenarios, schedules=schedules,
        schedule_version=SCHEDULE_VERSION, ticks=ticks or tick_obj,
        fold_rules=FoldRules(n_folds=3, test_sessions=50, min_train_sessions=120), git_commit=GIT_COMMIT,
        parameter_budget_n=12, registry=Registry(tmp_path / "private" / registry_name),
        events=events or DividendEvents(), tri_path=tri[0] if tri else None, tri_sha256=tri[1] if tri else None,
        provider=provider, holdout_sessions=holdout_sessions, targets_sha256=TARGETS_SHA,
        criteria=criteria if criteria is not None else default_criteria(),
    )


def write_tri(path: Path, sessions: Sequence[date], base: Decimal = Decimal("91234.5678")) -> str:
    """A synthetic gross TRI file (obviously fake levels). Returns its sha256."""
    lines = ["IndexName,Date,Total Returns Index"]
    level = base
    for day in sessions:
        level = (level * Decimal("1.0004")).quantize(Decimal("0.0001"))
        lines.append(f"NIFTY 500,{day.strftime('%d %b %Y')},{level}")
    path.write_text("\n".join(lines) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def registration_record(**overrides):
    """A valid registration payload with distinct synthetic hashes."""
    from strategy_india.registry import HASH_FIELDS, criteria_hash

    criteria = default_criteria()
    record = {name: sha(name) for name in HASH_FIELDS}
    record.update(
        hypothesis="synthetic cross-sectional momentum with a swing exit",
        parameter_budget_n=12,
        git_commit="a" * 40,
        seed=7,
        benchmark_ids=["INF000000ETF1", "nifty500_tri"],
        charge_schedule_version=SCHEDULE_VERSION,
        fold_rules={"scheme": "expanding", "n_folds": 3, "test_sessions": 40, "min_train_sessions": 60},
        holdout_range={"start": "2026-01-01", "end": "2026-06-30"},
        holdout_criteria=criteria,
        holdout_criteria_sha256=criteria_hash(criteria),
        spent_holdout_event_hashes=[],
    )
    record.update(overrides)
    return record

"""Study report (D-01a, D-16, D-17, D-18).

The result is a 59 ``CaveatedResult`` (survivorship and hindsight caveats are
mandatory) with Decimal money. It reports net return, max and trailing drawdown,
turnover, cost drag, hit rate and exposure against both benchmarks, keeps
``decision_drift_*`` apart from ``execution_slippage_*``, and adds PSR, MinTRL,
DSR with the registered trial count and a stationary-bootstrap interval.

D-01a: unknown band coverage and affected entry and exit attempts are listed by
target and by fold. A fold with any of them is ``missing_evidence``: it is never
counted as a neutral outcome or as a win. TRI levels never appear here, only a
derived excess return, or the label "TRI unavailable".

The file is written like 59 ``write_report``: read-only, atomic and idempotent
(the name carries the report hash, so the same content is never rewritten).
There are no timestamps, so the same inputs give byte-identical output.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from costs.schedule import ChargeSchedule
from pilot_data.core import Caveat, CaveatedResult, canonical_sha256, standard_caveats
from pilot_data.price_bands import UnavailableBand

from . import metrics
from .benchmark import TRI_LABEL, EtfChoice, EtfResult, TriSeries, TriUnavailable
from .data import DividendEvents
from .engine import SegmentResult
from .holdout import HoldoutEvidence, HoldoutVerdict
from .params import StrategyParams
from .signals import SENSITIVITY_FACTOR

ZERO = Decimal(0)
ONE = Decimal(1)
GATE_ID = "pessimistic"
HAIRCUTS = (Decimal("0.03"), Decimal("0.05"), Decimal("0.07"))  # RESEARCH Q6: survivorship inflation, points a year
TRIAL_SHARPE_SD_ANNUAL = Decimal("0.5")  # RESEARCH Q3 assumption for the spread of trial Sharpe ratios
CONFIDENCE = Decimal("0.95")

EXTRA_CAVEATS = (
    Caveat(code="RAW_PRICE_RETURN_BASIS", text=(
        "P&L is on the raw-price basis with no dividend cash credited, for the strategy and for the ETF alike.")),
    Caveat(code="STATUTORY_CLASSIFICATION_PROVISIONAL", text=(
        "Statutory charge classification is provisional (60 D12) until matched to the operator's contract notes.")),
    Caveat(code="CHARGE_SCHEDULE_PINNED", text=(
        "The charge schedule starts 2024-10-01 and is pinned for every trade, including earlier sessions (F2).")),
    Caveat(code="TICK_AND_BAND_REFERENCE_ASSUMED", text=(
        "The tick table band and the price band base use the previous session's raw close; this is not confirmed "
        "against NSE rules (the 59 band rule carries the same assumption).")),
    Caveat(code="ETF_IS_NIFTY_FIFTY_PROXY", text="The primary benchmark ETF is a Nifty 50 proxy, not Nifty 500."),
    Caveat(code="HOLDOUT_IS_A_VETO", text=(
        "A 12-month holdout cannot prove an edge; it is a veto with criteria fixed before it is opened.")),
)


class ExecutionStats(BaseModel):
    fills: int
    decision_drift_bps_mean: Decimal | None
    decision_drift_adverse_share: Decimal | None
    execution_slippage_bps_mean: Decimal | None
    execution_slippage_bps_max_abs: Decimal | None


class SegmentSummary(BaseModel):
    scenario_id: str
    mode: str
    net_return: Decimal
    max_drawdown: Decimal
    trailing_drawdown: Decimal
    turnover_ratio: Decimal
    swaps: int
    entries: int
    swaps_per_week: Decimal
    turnover_budget_swaps_per_week: Decimal
    within_turnover_budget: bool
    charges_total: Decimal
    cost_drag: Decimal
    hit_rate: Decimal | None
    average_exposure: Decimal
    trades_closed: int
    open_positions_at_end: int
    affected_entry_attempts: int
    affected_exit_attempts: int
    affected_by_target: dict[str, dict[str, int]]
    hurdle_rejections: int
    smallcap_rejections: int
    halt_events: int
    flatten_events: int
    run_chain_sha256: str
    scenario_refs: list[list[str]]
    schedule_refs: list[list[str]]
    tick_refs: list[list[str]]
    execution: ExecutionStats


class UnitReport(BaseModel):
    label: str
    test_start: date
    test_end: date
    train_cutoff: date | None
    regime: dict[str, Any] | None
    edge_slope: Decimal
    edge_slope_fitted: bool
    training_trades_kept: int
    training_trades_purged: int
    scenarios: dict[str, SegmentSummary]
    dividend_sensitivity: SegmentSummary | None
    etf_net_return: Decimal | None
    etf_unknown_reason: str | None
    excess_vs_etf: Decimal | None
    excess_vs_tri: Decimal | None
    unknown_bands_in_window: int
    unknown_bands_by_target: dict[str, int]
    status: Literal["evidence", "missing_evidence"]
    beats_etf: bool | None  # None when the unit is missing evidence or the ETF is unknown


class UnknownBandCoverage(BaseModel):
    total_in_run_window: int
    by_target: dict[str, int]
    by_fold: dict[str, int]
    outside_test_windows: int
    by_reason: dict[str, int]
    affected_attempts_by_fold: dict[str, dict[str, int]]
    affected_attempts_by_target: dict[str, dict[str, int]]
    affected_attempts_by_target_and_fold: dict[str, dict[str, dict[str, int]]]


class SignificanceReport(BaseModel):
    bars: int
    per_bar_sharpe: Decimal | None
    annual_sharpe: Decimal | None
    psr_vs_zero: Decimal | None
    psr_vs_etf: Decimal | None
    min_trl_bars: Decimal | None
    dsr: Decimal | None
    registered_trials: int
    trial_sharpe_sd_annual_assumed: Decimal
    excess_vs_etf_sum_interval_95: list[Decimal] | None
    note: str


class AggregateReport(BaseModel):
    units: int
    evidence_units: list[str]
    missing_evidence_units: list[str]
    units_beating_etf: int  # evidence units only
    net_return_compounded: Decimal
    complete: bool  # false while any unit is missing evidence
    max_drawdown_concatenated: Decimal
    survivorship_haircut_rows: list[dict[str, Decimal]]
    significance: SignificanceReport


class BenchmarkReport(BaseModel):
    etf_anchor_isin: str
    etf_stock_code: str
    etf_label: str
    etf_median_traded_value: Decimal
    etf_completeness: Decimal
    tri_label: str | None
    tri_available: bool
    tri_sha256: str | None
    tri_unavailable_reason: str | None


class DividendSection(BaseModel):
    events_tagged_dividend_amount_unknown: list[dict[str, str]]
    sensitivity_factor: Decimal
    sensitivity_run_present: bool


class StudyReport(CaveatedResult):
    schema_version: Literal["strategy-india-report/1"] = "strategy-india-report/1"
    kind: Literal["research", "holdout"]
    registration_entry_hash: str
    input_hashes: dict[str, str]
    pricing_schedule_version: str
    pricing_schedule_hash: str
    pricing_basis: str
    statutory_note: str
    capital: Decimal
    run_window_start: date
    run_window_end: date
    unknown_band_coverage: UnknownBandCoverage
    units: list[UnitReport]
    aggregate: AggregateReport
    benchmark: BenchmarkReport
    dividend: DividendSection
    holdout_verdict: dict[str, Any] | None
    notes: list[str]
    report_sha256: str


@dataclass(frozen=True)
class Unit:
    label: str
    test_start: date
    test_end: date
    train_cutoff: date | None
    regime: dict[str, Any] | None
    slope: Decimal
    slope_fitted: bool
    training_kept: int
    training_purged: int
    scenarios: Mapping[str, SegmentResult]
    sensitivity: SegmentResult | None
    etf: EtfResult | None
    etf_unknown_reason: str | None


# ---- summaries --------------------------------------------------------------------
def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, ZERO) / len(values) if values else None


def execution_stats(seg: SegmentResult) -> ExecutionStats:
    results = [f.result for f in seg.fills]
    drift = [r.decision_drift_bps for r in results if r.decision_drift_bps is not None]
    slip = [r.execution_slippage_bps for r in results if r.execution_slippage_bps is not None]
    adverse = Decimal(sum(1 for r in results if r.drift_adverse)) / len(results) if results else None
    return ExecutionStats(
        fills=len(results), decision_drift_bps_mean=_mean(drift), decision_drift_adverse_share=adverse,
        execution_slippage_bps_mean=_mean(slip), execution_slippage_bps_max_abs=max((abs(v) for v in slip), default=None),
    )


def affected_counts(seg: SegmentResult) -> tuple[int, int, dict[str, dict[str, int]]]:
    entry = exit_ = 0
    by_target: dict[str, dict[str, int]] = {}
    for a in seg.attempts:
        if not a.affected:
            continue
        slot = by_target.setdefault(a.stock_code, {"entry": 0, "exit": 0})
        slot[a.kind] += 1
        if a.kind == "entry":
            entry += 1
        else:
            exit_ += 1
    return entry, exit_, dict(sorted(by_target.items()))


def summarise_segment(seg: SegmentResult, params: StrategyParams) -> SegmentSummary:
    curve = [seg.start_equity] + [value for _, value in seg.curve]
    sessions = len(seg.sessions)
    spw = metrics.swaps_per_week(seg.swaps, sessions)
    budget = Decimal(params.turnover_budget_swaps_per_week)
    entry, exit_, by_target = affected_counts(seg)
    exposure = [value for _, value in seg.exposure]
    return SegmentSummary(
        scenario_id=seg.scenario_id, mode=seg.mode, net_return=seg.net_return, max_drawdown=metrics.max_drawdown(curve),
        trailing_drawdown=metrics.trailing_drawdown(curve), turnover_ratio=seg.traded_notional / seg.start_equity,
        swaps=seg.swaps, entries=seg.entries, swaps_per_week=spw, turnover_budget_swaps_per_week=budget,
        within_turnover_budget=spw <= budget, charges_total=seg.charges_total,
        cost_drag=seg.charges_total / seg.start_equity, hit_rate=metrics.hit_rate([t.net_return for t in seg.closed]),
        average_exposure=_mean(exposure) or ZERO, trades_closed=len(seg.closed),
        open_positions_at_end=len(seg.open_positions), affected_entry_attempts=entry, affected_exit_attempts=exit_,
        affected_by_target=by_target, hurdle_rejections=seg.hurdle_rejections, smallcap_rejections=seg.smallcap_rejections,
        halt_events=seg.halt_events, flatten_events=seg.flatten_events, run_chain_sha256=seg.run_chain_sha256,
        scenario_refs=[list(item) for item in seg.scenario_refs], schedule_refs=[list(i) for i in seg.schedule_refs],
        tick_refs=[list(i) for i in seg.tick_refs], execution=execution_stats(seg),
    )


def holdout_evidence(seg: SegmentResult, etf: EtfResult | None) -> HoldoutEvidence:
    curve = [seg.start_equity] + [value for _, value in seg.curve]
    return HoldoutEvidence(
        net_return=seg.net_return, benchmark_net_return=etf.net_return if etf is not None else None,
        max_drawdown=metrics.max_drawdown(curve), flatten_events=seg.flatten_events, swaps=seg.swaps,
        holdout_sessions=len(seg.sessions), no_assumed_fill_attempts=seg.affected_attempts,
    )


def _unknown_in(unavailable: Sequence[UnavailableBand], start: date, end: date) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in unavailable:
        if start <= item.session <= end:
            out[item.stock_code] = out.get(item.stock_code, 0) + 1
    return dict(sorted(out.items()))


def _unit_report(unit: Unit, params: StrategyParams, unavailable: Sequence[UnavailableBand],
                 tri: TriSeries | TriUnavailable) -> UnitReport:
    summaries = {sid: summarise_segment(seg, params) for sid, seg in unit.scenarios.items()}
    gate = summaries[GATE_ID]
    unknown = _unknown_in(unavailable, unit.test_start, unit.test_end)
    missing = bool(unknown) or gate.affected_entry_attempts + gate.affected_exit_attempts > 0
    etf_return = unit.etf.net_return if unit.etf is not None else None
    excess_etf = gate.net_return - etf_return if etf_return is not None else None
    excess_tri = None
    if isinstance(tri, TriSeries):
        tri_return = tri.period_return(unit.test_start, unit.test_end)
        excess_tri = gate.net_return - tri_return if tri_return is not None else None
    return UnitReport(
        label=unit.label, test_start=unit.test_start, test_end=unit.test_end, train_cutoff=unit.train_cutoff,
        regime=unit.regime, edge_slope=unit.slope, edge_slope_fitted=unit.slope_fitted,
        training_trades_kept=unit.training_kept, training_trades_purged=unit.training_purged, scenarios=summaries,
        dividend_sensitivity=summarise_segment(unit.sensitivity, params) if unit.sensitivity is not None else None,
        etf_net_return=etf_return, etf_unknown_reason=unit.etf_unknown_reason, excess_vs_etf=excess_etf,
        excess_vs_tri=excess_tri, unknown_bands_in_window=sum(unknown.values()), unknown_bands_by_target=unknown,
        status="missing_evidence" if missing else "evidence",
        beats_etf=None if missing or excess_etf is None else excess_etf > 0,
    )


def _coverage(units: Sequence[UnitReport], unavailable: Sequence[UnavailableBand], window: tuple[date, date]) -> UnknownBandCoverage:
    in_window = [u for u in unavailable if window[0] <= u.session <= window[1]]
    by_target: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for item in in_window:
        by_target[item.stock_code] = by_target.get(item.stock_code, 0) + 1
        by_reason[item.reason] = by_reason.get(item.reason, 0) + 1
    by_fold = {u.label: u.unknown_bands_in_window for u in units}
    att_fold: dict[str, dict[str, int]] = {}
    att_target: dict[str, dict[str, int]] = {}
    att_both: dict[str, dict[str, dict[str, int]]] = {}
    for u in units:
        gate = u.scenarios[GATE_ID]
        att_fold[u.label] = {"entry": gate.affected_entry_attempts, "exit": gate.affected_exit_attempts}
        for target, counts in gate.affected_by_target.items():
            slot = att_target.setdefault(target, {"entry": 0, "exit": 0})
            slot["entry"] += counts["entry"]
            slot["exit"] += counts["exit"]
            att_both.setdefault(target, {})[u.label] = dict(counts)
    return UnknownBandCoverage(
        total_in_run_window=len(in_window), by_target=dict(sorted(by_target.items())), by_fold=by_fold,
        outside_test_windows=len(in_window) - sum(by_fold.values()), by_reason=dict(sorted(by_reason.items())),
        affected_attempts_by_fold=att_fold, affected_attempts_by_target=dict(sorted(att_target.items())),
        affected_attempts_by_target_and_fold={k: att_both[k] for k in sorted(att_both)},
    )


def _daily(seg: SegmentResult) -> list[Decimal]:
    return metrics.daily_returns([seg.start_equity] + [v for _, v in seg.curve])


def _aggregate(units: Sequence[Unit], reports: Sequence[UnitReport], params: StrategyParams, trials: int) -> AggregateReport:
    compounded = ONE
    returns: list[Decimal] = []
    etf_returns: list[Decimal] = []
    excess: list[Decimal] = []
    chain = [ONE]
    for unit in units:
        seg = unit.scenarios[GATE_ID]
        daily = _daily(seg)
        returns += daily
        for r in daily:
            chain.append(chain[-1] * (ONE + r))
        compounded *= ONE + seg.net_return
        if unit.etf is not None:
            etf_daily = metrics.daily_returns([c for _, c in unit.etf.curve])
            if len(etf_daily) == len(daily):
                etf_returns += etf_daily
                excess += [a - b for a, b in zip(daily, etf_daily)]
    sig = metrics.significance(returns, trials=trials, trial_sd_annual=TRIAL_SHARPE_SD_ANNUAL, confidence=CONFIDENCE)
    psr_etf = None
    if sig.per_bar_sharpe is not None and len(etf_returns) == len(returns) and len(returns) >= 4:
        etf_sr = metrics.sharpe_per_bar(etf_returns)
        if etf_sr is not None:
            skew, kurt = metrics.skew_kurtosis(returns)
            psr_etf = metrics.psr(sig.per_bar_sharpe, etf_sr, len(returns), skew, kurt)
    interval = metrics.stationary_bootstrap_interval(
        excess, seed=params.seed, resamples=1000, mean_block=5, lower=Decimal("0.025"), upper=Decimal("0.975")
    )
    sessions = sum(len(u.scenarios[GATE_ID].sessions) for u in units)
    net = compounded - ONE
    haircuts = [
        {"haircut_points_per_year": h, "net_return_after_haircut": net - h * Decimal(sessions) / Decimal(metrics.SESSIONS_PER_YEAR)}
        for h in HAIRCUTS
    ]
    evidence = [r.label for r in reports if r.status == "evidence"]
    missing = [r.label for r in reports if r.status == "missing_evidence"]
    return AggregateReport(
        units=len(units), evidence_units=evidence, missing_evidence_units=missing,
        units_beating_etf=sum(1 for r in reports if r.beats_etf is True), net_return_compounded=net,
        complete=not missing, max_drawdown_concatenated=metrics.max_drawdown(chain), survivorship_haircut_rows=haircuts,
        significance=SignificanceReport(
            bars=sig.bars, per_bar_sharpe=sig.per_bar_sharpe, annual_sharpe=sig.annual_sharpe, psr_vs_zero=sig.psr_vs_zero,
            psr_vs_etf=psr_etf, min_trl_bars=sig.min_trl_bars, dsr=sig.dsr, registered_trials=trials,
            trial_sharpe_sd_annual_assumed=TRIAL_SHARPE_SD_ANNUAL,
            excess_vs_etf_sum_interval_95=list(interval) if interval is not None else None,
            note=("A holdout or fold set this short cannot prove an edge (minimum detectable annual Sharpe about 2.5 to 3.0); "
                  "read these as a veto, not as proof."),
        ),
    )


STATUTORY_NOTE = (
    "Statutory charge classification is provisional (60 D12). Every trade is priced on one pinned schedule version "
    "(full 0.07% brokerage plus GST, no prepaid credit); the version and hash are recorded here."
)


def build_report(
    *,
    kind: Literal["research", "holdout"],
    registration_entry_hash: str,
    input_hashes: Mapping[str, str],
    units: Sequence[Unit],
    unavailable: Sequence[UnavailableBand],
    run_window: tuple[date, date],
    etf_choice: EtfChoice,
    tri: TriSeries | TriUnavailable,
    params: StrategyParams,
    capital: Decimal,
    schedule: ChargeSchedule,
    trials: int,
    dividend_events: DividendEvents,
    verdict: HoldoutVerdict | None = None,
    holdout_evidence_: Mapping[str, HoldoutEvidence | None] | None = None,
) -> StudyReport:
    reports = [_unit_report(u, params, unavailable, tri) for u in units]
    verdict_payload: dict[str, Any] | None = None
    if verdict is not None:
        verdict_payload = {
            "verdict": verdict.verdict, "criteria_sha256": verdict.criteria_sha256, "breaches": list(verdict.breaches),
            "missing_evidence": list(verdict.missing_evidence), "sensitivity_flips": list(verdict.sensitivity_flips),
            "annualised_swaps": str(verdict.annualised_swaps), "passed": verdict.passed,
            "criteria_status": "PROPOSED (D-19) until the operator confirms",
        }
        for name, ev in (holdout_evidence_ or {}).items():
            if ev is not None:
                verdict_payload[name] = {k: str(v) if v is not None else None for k, v in ev.__dict__.items()}
    tri_available = isinstance(tri, TriSeries)
    benchmark = BenchmarkReport(
        etf_anchor_isin=etf_choice.anchor_isin, etf_stock_code=etf_choice.stock_code,
        etf_label="Nifty 50 proxy ETF (buy and hold, one priced round trip)",
        etf_median_traded_value=etf_choice.median_traded_value, etf_completeness=etf_choice.completeness,
        tri_label=None if tri_available else TRI_LABEL, tri_available=tri_available,
        tri_sha256=tri.sha256 if isinstance(tri, TriSeries) else None,
        tri_unavailable_reason=None if tri_available else tri.reason,
    )
    events = [{"anchor_isin": e.anchor_isin, "ex_date": e.ex_date.isoformat()} for e in dividend_events.all()
              if run_window[0] <= e.ex_date <= run_window[1]]
    body = dict(
        workspace="india", caveats=standard_caveats(*EXTRA_CAVEATS), kind=kind,
        registration_entry_hash=registration_entry_hash, input_hashes=dict(sorted(input_hashes.items())),
        pricing_schedule_version=schedule.version, pricing_schedule_hash=schedule.schedule_hash,
        pricing_basis="pinned", statutory_note=STATUTORY_NOTE, capital=capital, run_window_start=run_window[0],
        run_window_end=run_window[1], unknown_band_coverage=_coverage(reports, unavailable, run_window), units=reports,
        aggregate=_aggregate(units, reports, params, trials), benchmark=benchmark,
        dividend=DividendSection(
            events_tagged_dividend_amount_unknown=events, sensitivity_factor=SENSITIVITY_FACTOR,
            sensitivity_run_present=any(u.sensitivity is not None for u in units),
        ),
        holdout_verdict=verdict_payload,
        notes=[
            "Unknown bands and affected attempts are missing evidence, never neutral outcomes or wins (D-01a).",
            "decision_drift_* (decision price to fill) is reported apart from execution_slippage_* (limit to fill).",
            "Per-trade charge attribution splits a day's charges pro rata by notional; cash effects are exact.",
        ],
        report_sha256="",
    )
    draft = StudyReport(**body)
    digest = canonical_sha256(draft.model_dump(mode="json"))
    return draft.model_copy(update={"report_sha256": digest})


def write_report(root: Path, report: StudyReport) -> Path:
    """Read-only, atomic and idempotent, like 59 ``write_report``."""
    reports = Path(root) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"strategy-india-{report.kind}-{report.report_sha256[:12]}.json"
    if path.exists():
        return path
    dumped = report.model_dump(mode="json")
    ordered = {"caveats": dumped.pop("caveats"), **dumped}
    fd, tmp = tempfile.mkstemp(dir=reports, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(ordered, indent=2, sort_keys=False))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o444)
    return path

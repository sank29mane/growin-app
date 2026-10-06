"""Orchestration: register, research run, one-shot holdout (D-01, D-10, D-12, D-19).

Order of every run: registry chain, then the Phase 59 gate (against the registered
hashes), then the live-input check (D-10), and only then any evaluation. The holdout
path additionally verifies the pinned registry head, opens the holdout (logged in the
registry) and only then reads a holdout row.

The CLI helpers at the bottom wire real inputs (private config, the 59 dataset and
store) and are not exercised against real data by the tests, which use the Python API
with synthetic inputs.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from costs.core import TickSizeUnavailable
from costs.fills import FillScenarioSet
from costs.schedule import PricingBasis, ScheduleSet
from private_config.schemas import IndiaLimits

from .benchmark import EtfChoice, EtfResult, TriSeries, TriUnavailable, choose_etf, etf_buy_and_hold, load_tri
from .data import BandSource, DatasetView, DividendEvents, EligibilitySource
from .engine import RunContext, run_holdout_segment, run_walk_forward
from .errors import RegistryError, StrategyIndiaError
from .folds import FoldRules
from .gate import GateResult, load_coverage_report
from .holdout import (
    HoldoutRange,
    HoldoutVerdict,
    check_gate_scenario,
    default_criteria,
    evaluate_verdict,
    holdout_digest,
    holdout_range_for,
    open_holdout,
)
from .hurdle import hurdle_map_sha256
from .params import StrategyParams, params_sha256, parse_params
from .registry import Entry, LIVE_CHECKED_FIELDS, Registry, check_live_inputs, criteria_hash
from .report import Unit, StudyReport, build_report, holdout_evidence, write_report
from .ticks import TickTables

TRI_ID = "nifty500_tri"


@dataclass
class StudyInputs:
    rows: Sequence[Any]
    dataset_sha256: str
    params_raw: dict[str, Any]
    limits: IndiaLimits
    coverage_path: Path
    eligibility: EligibilitySource
    universe_policy: Any
    bands: BandSource
    scenarios: FillScenarioSet
    schedules: ScheduleSet
    schedule_version: str
    ticks: TickTables
    fold_rules: FoldRules
    git_commit: str
    parameter_budget_n: int
    registry: Registry
    events: DividendEvents = field(default_factory=DividendEvents)
    tri_path: Path | None = None
    tri_sha256: str | None = None
    provider: Any = None
    holdout_sessions: int = 250


@dataclass(frozen=True)
class Prepared:
    params: StrategyParams
    holdout: HoldoutRange
    sessions: tuple[date, ...]
    view: DatasetView
    pricing_basis: PricingBasis


def prepare(inputs: StudyInputs) -> Prepared:
    params = parse_params(inputs.params_raw)
    sessions = tuple(sorted({getattr(row, "trade_date", None) or row.session for row in inputs.rows}))
    holdout = holdout_range_for(sessions, inputs.holdout_sessions)
    view = DatasetView.from_rows(inputs.rows, holdout=holdout)
    return Prepared(params, holdout, sessions, view, PricingBasis.pinned(inputs.schedule_version))


def _context(inputs: StudyInputs, prep: Prepared, gate: GateResult, view: DatasetView) -> RunContext:
    kwargs: dict[str, Any] = {}
    if inputs.provider is not None:
        kwargs["provider"] = inputs.provider
    return RunContext(
        view=view, params=prep.params, limits=inputs.limits, eligibility=inputs.eligibility,
        universe_policy=inputs.universe_policy, bands=inputs.bands, unavailable=gate.index(), events=inputs.events,
        scenarios=inputs.scenarios, schedules=inputs.schedules, pricing_basis=prep.pricing_basis, ticks=inputs.ticks,
        **kwargs,
    )


def _dev_window(prep: Prepared) -> tuple[date, date]:
    return prep.sessions[0], prep.view.visible_end


def live_inputs(inputs: StudyInputs, prep: Prepared, gate: GateResult, etf_anchor: str, criteria: Mapping[str, Any]) -> dict[str, Any]:
    """Everything D-10 compares against the registration, computed from the live inputs."""
    schedule = inputs.schedules.get(inputs.schedule_version)
    return {
        "params_sha256": params_sha256(inputs.params_raw),
        "dataset_sha256": inputs.dataset_sha256,
        "coverage_report_sha256": gate.report_sha256,
        "coverage_file_sha256": gate.file_sha256,
        "fill_scenarios_sha256": inputs.scenarios.scenarios_hash,
        "charge_schedule_sha256": schedule.schedule_hash,
        "charge_schedule_version": schedule.version,
        "tick_table_sha256": inputs.ticks.sha256(),
        "hurdle_map_sha256": hurdle_map_sha256(prep.params),
        "holdout_sha256": holdout_digest(inputs.dataset_sha256, prep.holdout, prep.sessions),
        "holdout_range": prep.holdout.as_payload(),
        "holdout_criteria_sha256": criteria_hash(criteria),
        "git_commit": inputs.git_commit,
        "seed": prep.params.seed,
        "parameter_budget_n": inputs.parameter_budget_n,
        "benchmark_ids": [etf_anchor, TRI_ID],
        "fold_rules": inputs.fold_rules.as_payload(),
    }


def require_registration(registry: Registry, live: Mapping[str, Any], *, expected_head: str | None,
                         entry_hash: str | None = None) -> Entry:
    """Refuse unless the chain verifies, a registration exists and every recorded value equals the live input."""
    registry.entries(expected_head=expected_head)
    entry = registry.registration(entry_hash)
    check_live_inputs(entry.payload, live)
    return entry


def register(inputs: StudyInputs, *, hypothesis: str, criteria: Mapping[str, Any] | None = None) -> Entry:
    """Seal a registration. Runs the gate and picks the benchmark ETF from development data only."""
    prep = prepare(inputs)
    criteria = dict(criteria) if criteria is not None else default_criteria()
    start, end = prep.sessions[0], prep.sessions[-1]
    gate = load_coverage_report(inputs.coverage_path, run_start=start, run_end=end)
    check_gate_scenario(criteria, k_ticks=inputs.scenarios.gate().k_ticks, phase62_gate=inputs.scenarios.gate().phase62_gate)
    dev_start, dev_end = _dev_window(prep)
    etf = choose_etf(prep.view, prep.params.benchmark, start=dev_start, end=dev_end)
    live = live_inputs(inputs, prep, gate, etf.anchor_isin, criteria)
    spent = [e.entry_hash for e in inputs.registry.holdout_events()] if inputs.registry.path.exists() else []
    record = {name: live[name] for name in LIVE_CHECKED_FIELDS}
    record.update(hypothesis=hypothesis, holdout_criteria=criteria, spent_holdout_event_hashes=spent)
    return inputs.registry.register(record)


def _gate_for_registration(inputs: StudyInputs, prep: Prepared, entry: Entry, window: tuple[date, date]) -> GateResult:
    return load_coverage_report(
        inputs.coverage_path, run_start=window[0], run_end=window[1],
        expected_report_sha256=entry.payload["coverage_report_sha256"],
        expected_file_sha256=entry.payload["coverage_file_sha256"],
    )


def _tri(inputs: StudyInputs) -> TriSeries | TriUnavailable:
    return load_tri(inputs.tri_path, inputs.tri_sha256)


def _etf(view: DatasetView, anchor: str, start: date, end: date, ctx: RunContext, capital: Decimal) -> tuple[EtfResult | None, str | None]:
    try:
        return (
            etf_buy_and_hold(view, anchor, start=start, end=end, capital=capital, ticks=ctx.ticks,
                             schedules=ctx.schedules, pricing_basis=ctx.pricing_basis),
            None,
        )
    except TickSizeUnavailable as exc:
        return None, f"tick_unavailable: {exc}"


def _input_hashes(entry: Entry) -> dict[str, str]:
    return {name: entry.payload[name] for name in entry.payload if name.endswith("_sha256")}


def run_research(inputs: StudyInputs, *, expected_head: str | None = None, registration_hash: str | None = None) -> StudyReport:
    """Purged walk-forward over the development window under the registered inputs."""
    prep = prepare(inputs)
    dev_start, dev_end = _dev_window(prep)
    reg = inputs.registry
    entry = reg.registration(registration_hash) if reg.path.exists() else None
    if entry is None:
        raise RegistryError("registry file is missing; the engine refuses to run")
    gate = _gate_for_registration(inputs, prep, entry, (dev_start, dev_end))
    etf_anchor = entry.payload["benchmark_ids"][0]
    criteria = entry.payload["holdout_criteria"]
    live = live_inputs(inputs, prep, gate, etf_anchor, criteria)
    entry = require_registration(reg, live, expected_head=expected_head, entry_hash=entry.entry_hash)
    ctx = _context(inputs, prep, gate, prep.view)
    walk = run_walk_forward(ctx, inputs.fold_rules)
    etf_choice = choose_etf(prep.view, prep.params.benchmark, start=dev_start, end=dev_end)
    units = []
    for fr in walk.folds:
        etf, why = _etf(prep.view, etf_anchor, fr.fold.test_start, fr.fold.test_end, ctx, inputs.limits.capital_cap)
        units.append(Unit(str(fr.fold.index), fr.fold.test_start, fr.fold.test_end, fr.fold.cutoff, fr.regime, fr.slope,
                          fr.slope_fitted, fr.training_trades_kept, fr.training_trades_purged, fr.scenarios,
                          fr.sensitivity, etf, why))
    return build_report(
        kind="research", registration_entry_hash=entry.entry_hash, input_hashes=_input_hashes(entry), units=units,
        unavailable=gate.unavailable, run_window=(dev_start, dev_end), etf_choice=etf_choice, tri=_tri(inputs),
        params=prep.params, capital=inputs.limits.capital_cap, schedule=inputs.schedules.get(inputs.schedule_version),
        trials=entry.payload["parameter_budget_n"], dividend_events=inputs.events,
    )


@dataclass(frozen=True)
class HoldoutOutcome:
    report: StudyReport
    verdict: HoldoutVerdict
    event_hash: str


def run_holdout(inputs: StudyInputs, *, expected_head: str, logged_at: str | None = None,
                registration_hash: str | None = None) -> HoldoutOutcome:
    """Open the holdout once (logged in the registry) and judge it against the sealed D-19 criteria."""
    prep = prepare(inputs)
    reg = inputs.registry
    entry = reg.registration(registration_hash) if reg.path.exists() else None
    if entry is None:
        raise RegistryError("registry file is missing; the holdout stays sealed")
    window = (prep.sessions[0], prep.sessions[-1])
    gate = _gate_for_registration(inputs, prep, entry, window)
    etf_anchor = entry.payload["benchmark_ids"][0]
    criteria = entry.payload["holdout_criteria"]
    live = live_inputs(inputs, prep, gate, etf_anchor, criteria)
    entry = require_registration(reg, live, expected_head=expected_head, entry_hash=entry.entry_hash)
    gate_scenario = inputs.scenarios.gate()
    check_gate_scenario(criteria, k_ticks=gate_scenario.k_ticks, phase62_gate=gate_scenario.phase62_gate)
    grant = open_holdout(reg, criteria=criteria, expected_head=expected_head, registration_hash=entry.entry_hash,
                         logged_at=logged_at)
    opened = prep.view.open(grant)
    ctx = _context(inputs, prep, gate, opened)
    run = run_holdout_segment(ctx, grant.holdout)
    etf, why = _etf(opened, etf_anchor, grant.holdout.start, grant.holdout.end, ctx, inputs.limits.capital_cap)
    base = holdout_evidence(run.scenarios[gate_scenario.scenario_id], etf)
    sens = holdout_evidence(run.sensitivity, etf) if run.sensitivity is not None else None
    verdict = evaluate_verdict(criteria, base, sens)
    dev_start, dev_end = _dev_window(prep)
    etf_choice = choose_etf(prep.view, prep.params.benchmark, start=dev_start, end=dev_end)
    unit = Unit("holdout", grant.holdout.start, grant.holdout.end, dev_end, run.regime, run.slope, False, 0, 0,
                run.scenarios, run.sensitivity, etf, why)
    report = build_report(
        kind="holdout", registration_entry_hash=entry.entry_hash, input_hashes=_input_hashes(entry), units=[unit],
        unavailable=gate.unavailable, run_window=(grant.holdout.start, grant.holdout.end), etf_choice=etf_choice,
        tri=_tri(inputs), params=prep.params, capital=inputs.limits.capital_cap,
        schedule=inputs.schedules.get(inputs.schedule_version), trials=entry.payload["parameter_budget_n"],
        dividend_events=inputs.events, verdict=verdict, holdout_evidence_={"evidence": base, "sensitivity_evidence": sens},
    )
    return HoldoutOutcome(report, verdict, grant.event_hash)


# ---- CLI wiring ---------------------------------------------------------------------
def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StrategyIndiaError("the config file is missing or not JSON", code="config_invalid") from exc
    needed = ("private_dir", "dataset_dir", "store_root", "coverage_report", "registry", "report_root", "git_commit",
              "fold_rules", "parameter_budget_n")
    missing = [key for key in needed if key not in config]
    if missing:
        raise StrategyIndiaError(f"config is missing: {', '.join(missing)}", code="config_invalid")
    return config


def build_inputs(config: Mapping[str, Any]) -> StudyInputs:
    """Real wiring: 58 private config, the verified 59 dataset and a read-only 59 store."""
    from costs.fills import load_fill_scenarios
    from costs.schedule import load_schedule_set
    from pilot_data.price_bands import BandResolver
    from pilot_data.store import PilotDataStore
    from pilot_data.targets import latest_target_universe
    from pilot_data.universe import UniversePolicy
    from private_config.loader import load_workspace_config

    from .data import DividendUnknownEvent, UniverseEligibility, load_dataset_rows
    from .ticks import load_default_tables

    cfg = load_workspace_config(config["private_dir"], "india")
    assert cfg.strategy is not None and cfg.limits is not None
    manifest, rows = load_dataset_rows(Path(config["dataset_dir"]))
    store = PilotDataStore(Path(config["store_root"]), workspace="india", read_only=True)
    targets = latest_target_universe(store, workspace="india")
    if targets is None:
        raise StrategyIndiaError("the 59 store has no target universe", code="targets_missing")
    resolver = BandResolver(store, record_quarantines=False)

    class _Bands:
        def observe(self, isin: str, session: date):
            return resolver.observe(isin, session)

    schedules = load_schedule_set()
    events = DividendEvents(
        DividendUnknownEvent(item["anchor_isin"], date.fromisoformat(item["ex_date"]))
        for item in config.get("dividend_unknown_events", [])
    )
    return StudyInputs(
        rows=rows, dataset_sha256=manifest.dataset_sha256, params_raw=cfg.strategy.params, limits=cfg.limits,
        coverage_path=Path(config["coverage_report"]),
        eligibility=UniverseEligibility(store, targets, UniversePolicy(), mode="research"),
        universe_policy=UniversePolicy(), bands=_Bands(), scenarios=load_fill_scenarios(), schedules=schedules,
        schedule_version=config.get("schedule_version", schedules.versions[-1].version), ticks=load_default_tables(),
        fold_rules=FoldRules(**config["fold_rules"]), git_commit=config["git_commit"],
        parameter_budget_n=int(config["parameter_budget_n"]), registry=Registry(Path(config["registry"])),
        events=events, tri_path=Path(config["tri"]["path"]) if config.get("tri") else None,
        tri_sha256=config["tri"]["sha256"] if config.get("tri") else None,
    )


def cli_register(config: Mapping[str, Any]) -> dict[str, Any]:
    inputs = build_inputs(config)
    entry = register(inputs, hypothesis=config.get("hypothesis", "cross-sectional momentum with a swing exit"))
    return {"registered": entry.entry_hash, "registry_head": inputs.registry.head_hash()}


def cli_run(config: Mapping[str, Any]) -> dict[str, Any]:
    inputs = build_inputs(config)
    report = run_research(inputs, expected_head=config.get("registry_head_sha256"))
    path = write_report(Path(config["report_root"]), report)
    return {"report": str(path), "report_sha256": report.report_sha256}


def cli_holdout(config: Mapping[str, Any], *, logged_at: str) -> dict[str, Any]:
    if "registry_head_sha256" not in config:
        raise StrategyIndiaError("opening the holdout needs the pinned registry_head_sha256", code="config_invalid")
    inputs = build_inputs(config)
    outcome = run_holdout(inputs, expected_head=config["registry_head_sha256"], logged_at=logged_at)
    path = write_report(Path(config["report_root"]), outcome.report)
    return {"report": str(path), "verdict": outcome.verdict.verdict, "holdout_event": outcome.event_hash}

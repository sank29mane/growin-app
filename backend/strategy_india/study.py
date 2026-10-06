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
import re
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
from .errors import RegistryError, RegistryMismatch, StrategyIndiaError
from .folds import FoldRules
from .gate import GateResult, load_coverage_report
from .holdout import (
    HoldoutRange,
    HoldoutVerdict,
    check_gate_scenario,
    evaluate_verdict,
    holdout_digest,
    holdout_range_for,
    open_holdout,
)
from .hurdle import hurdle_map_sha256
from .params import StrategyParams, params_sha256, parse_params
from .registry import Entry, LIVE_CHECKED_FIELDS, Registry, check_live_inputs, criteria_hash
from .report import Unit, StudyReport, build_report, holdout_evidence, write_report
from .ticks import EQUITY, NON_GOLD_ETF, TickTables

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
    targets_sha256: str  # 59 TargetUniverseResult.target_sha256, needed to recompute the coverage report hash
    criteria: Mapping[str, Any] | None  # D-19 criteria read from the private file; None refuses
    events: DividendEvents = field(default_factory=DividendEvents)
    sensitivity_factor: Decimal | None = None  # if given, must equal the sealed D-19 factor
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


def required_criteria(inputs: StudyInputs) -> dict[str, Any]:
    if not inputs.criteria:
        raise RegistryError("D-19 criteria are absent; the engine refuses to run")
    return dict(inputs.criteria)


def sealed_factor(inputs: StudyInputs, criteria: Mapping[str, Any]) -> Decimal:
    """The D-20 sensitivity factor comes from the sealed criteria. A different factor from the caller is refused."""
    factor = Decimal(criteria["dividend_sensitivity_factor"])
    if inputs.sensitivity_factor is not None and inputs.sensitivity_factor != factor:
        raise RegistryMismatch("dividend_sensitivity_factor", "the sensitivity factor differs from the sealed D-19 factor")
    return factor


def _context(inputs: StudyInputs, prep: Prepared, gate: GateResult, view: DatasetView,
             criteria: Mapping[str, Any]) -> RunContext:
    kwargs: dict[str, Any] = {}
    if inputs.provider is not None:
        kwargs["provider"] = inputs.provider
    return RunContext(
        view=view, params=prep.params, limits=inputs.limits, eligibility=inputs.eligibility,
        universe_policy=inputs.universe_policy, bands=inputs.bands, unavailable=gate.index(), events=inputs.events,
        scenarios=inputs.scenarios, schedules=inputs.schedules, pricing_basis=prep.pricing_basis, ticks=inputs.ticks,
        sensitivity_factor=sealed_factor(inputs, criteria), **kwargs,
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
        "dividend_events_sha256": inputs.events.sealed_sha256(),
        "holdout_sha256": holdout_digest(inputs.dataset_sha256, prep.holdout, prep.sessions),
        "holdout_range": prep.holdout.as_payload(),
        "holdout_criteria_sha256": criteria_hash(criteria),
        "git_commit": inputs.git_commit,
        "seed": prep.params.seed,
        "parameter_budget_n": inputs.parameter_budget_n,
        "benchmark_ids": [etf_anchor, TRI_ID],
        "fold_rules": inputs.fold_rules.as_payload(),
    }


def require_head(expected_head: str | None) -> str:
    if not expected_head:
        raise RegistryError("a pinned registry head hash is required; an unpinned registry cannot be trusted")
    return expected_head


def require_registration(registry: Registry, live: Mapping[str, Any], *, expected_head: str | None,
                         entry_hash: str | None = None) -> Entry:
    """Refuse unless the pinned head matches, the chain verifies, a registration exists and every recorded
    value equals the live input."""
    registry.entries(expected_head=require_head(expected_head))
    entry = registry.registration(entry_hash)
    check_live_inputs(entry.payload, live)
    return entry


def register(inputs: StudyInputs, *, hypothesis: str, expected_head: str | None = None) -> Entry:
    """Seal a registration. Runs the gate and picks the benchmark ETF from development data only.

    The first registration needs no pin. Any later one must present the pinned head so a truncated chain
    (a removed holdout-open event) cannot be extended.
    """
    prep = prepare(inputs)
    criteria = required_criteria(inputs)
    reg = inputs.registry
    if reg.path.exists() and reg.path.stat().st_size > 0:
        reg.entries(expected_head=require_head(expected_head))
    start, end = prep.sessions[0], prep.sessions[-1]
    gate = load_coverage_report(inputs.coverage_path, run_start=start, run_end=end, targets_sha256=inputs.targets_sha256)
    check_gate_scenario(criteria, k_ticks=inputs.scenarios.gate().k_ticks, phase62_gate=inputs.scenarios.gate().phase62_gate)
    sealed_factor(inputs, criteria)
    dev_start, dev_end = _dev_window(prep)
    etf = choose_etf(prep.view, prep.params.benchmark, start=dev_start, end=dev_end)
    live = live_inputs(inputs, prep, gate, etf.anchor_isin, criteria)
    spent = [e.entry_hash for e in reg.holdout_events()] if reg.path.exists() else []
    record = {name: live[name] for name in LIVE_CHECKED_FIELDS}
    record.update(hypothesis=hypothesis, holdout_criteria=criteria, spent_holdout_event_hashes=spent)
    return reg.register(record)


def _gate_for_registration(inputs: StudyInputs, prep: Prepared, entry: Entry, window: tuple[date, date]) -> GateResult:
    return load_coverage_report(
        inputs.coverage_path, run_start=window[0], run_end=window[1], targets_sha256=inputs.targets_sha256,
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


def _registered(inputs: StudyInputs, registration_hash: str | None, what: str) -> Entry:
    reg = inputs.registry
    if not reg.path.exists():
        raise RegistryError(f"registry file is missing; {what}")
    return reg.registration(registration_hash)


def run_research(inputs: StudyInputs, *, expected_head: str, registration_hash: str | None = None) -> StudyReport:
    """Purged walk-forward over the development window under the registered inputs. The head pin is required."""
    require_head(expected_head)
    prep = prepare(inputs)
    dev_start, dev_end = _dev_window(prep)
    reg = inputs.registry
    entry = _registered(inputs, registration_hash, "the engine refuses to run")
    criteria = required_criteria(inputs)
    gate = _gate_for_registration(inputs, prep, entry, (dev_start, dev_end))
    etf_anchor = entry.payload["benchmark_ids"][0]
    live = live_inputs(inputs, prep, gate, etf_anchor, criteria)
    entry = require_registration(reg, live, expected_head=expected_head, entry_hash=entry.entry_hash)
    ctx = _context(inputs, prep, gate, prep.view, criteria)
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
        sensitivity_factor=ctx.sensitivity_factor,
    )


def unrunnable(message: str) -> StrategyIndiaError:
    return StrategyIndiaError(f"{message}; the holdout is NOT spent", code="holdout_unrunnable")


def preflight_holdout(inputs: StudyInputs, prep: Prepared, criteria: Mapping[str, Any]) -> None:
    """Everything that can be known before the holdout is opened. Any failure refuses WITHOUT spending it.

    Uses session dates and table coverage only; no holdout price is read. The registry, gate, D-19 criteria
    and D-20 event hashes were already checked against the registration by the caller.
    """
    gate_scenario = inputs.scenarios.gate()
    check_gate_scenario(criteria, k_ticks=gate_scenario.k_ticks, phase62_gate=gate_scenario.phase62_gate)
    sealed_factor(inputs, criteria)
    if not inputs.rows or not all(hasattr(row, "payload") for row in inputs.rows):
        raise unrunnable("the dataset rows cannot be verified against dataset_sha256")
    from pilot_data.dataset import dataset_hash

    if dataset_hash(sorted(inputs.rows, key=lambda r: (r.anchor_isin, r.trade_date))) != inputs.dataset_sha256:
        raise unrunnable("the dataset rows do not reproduce dataset_sha256")
    days = [day for day in prep.sessions if prep.holdout.contains(day)]
    if not days:
        raise unrunnable("the holdout range holds no sessions")
    for instrument_class in (EQUITY, NON_GOLD_ETF):
        if instrument_class not in inputs.ticks.classes():
            raise unrunnable(f"no tick table is registered for instrument class {instrument_class}")
        gaps = [day for day in days if not inputs.ticks.covers(instrument_class, day)]
        if gaps:
            raise unrunnable(
                f"the {instrument_class} tick table does not cover {len(gaps)} holdout sessions, first {gaps[0].isoformat()}"
            )


@dataclass(frozen=True)
class HoldoutOutcome:
    report: StudyReport
    verdict: HoldoutVerdict
    event_hash: str


def run_holdout(inputs: StudyInputs, *, expected_head: str, logged_at: str | None = None,
                registration_hash: str | None = None) -> HoldoutOutcome:
    """Open the holdout once (logged in the registry) and judge it against the sealed D-19 criteria."""
    require_head(expected_head)
    prep = prepare(inputs)
    reg = inputs.registry
    entry = _registered(inputs, registration_hash, "the holdout stays sealed")
    criteria = required_criteria(inputs)
    window = (prep.sessions[0], prep.sessions[-1])
    gate = _gate_for_registration(inputs, prep, entry, window)
    etf_anchor = entry.payload["benchmark_ids"][0]
    live = live_inputs(inputs, prep, gate, etf_anchor, criteria)
    entry = require_registration(reg, live, expected_head=expected_head, entry_hash=entry.entry_hash)
    preflight_holdout(inputs, prep, criteria)  # nothing below this line can be refused for a known reason
    gate_scenario = inputs.scenarios.gate()
    grant = open_holdout(reg, criteria=criteria, expected_head=expected_head, registration_hash=entry.entry_hash,
                         logged_at=logged_at)
    opened = prep.view.open(grant)
    ctx = _context(inputs, prep, gate, opened, criteria)
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
        dividend_events=inputs.events, sensitivity_factor=ctx.sensitivity_factor, verdict=verdict,
        holdout_evidence_={"evidence": base, "sensitivity_evidence": sens},
    )
    return HoldoutOutcome(report, verdict, grant.event_hash)


# ---- CLI wiring ---------------------------------------------------------------------
HEAD_REF_NAME = "registry_head.txt"  # a holdout_refs entry in private/india/strategy.json (path plus sha256, 58)
_HEX64 = re.compile(r"[0-9a-f]{64}")


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StrategyIndiaError("the config file is missing or not JSON", code="config_invalid") from exc
    needed = ("private_dir", "dataset_dir", "store_root", "coverage_report", "registry", "report_root", "git_commit",
              "fold_rules", "parameter_budget_n", "criteria")
    missing = [key for key in needed if key not in config]
    if missing:
        raise StrategyIndiaError(f"config is missing: {', '.join(missing)}", code="config_invalid")
    return config


def resolve_registry_head(config: Mapping[str, Any], holdout_refs: Sequence[Any], workspace_dir: Path) -> str:
    """The pinned head: from the 58 ``holdout_refs`` (a ``registry_head.txt`` file whose sha256 58 already verified)
    and/or an explicit ``registry_head_sha256`` (``--registry-head``). If both are present they must agree."""
    found: str | None = None
    for ref in holdout_refs:
        if ref.path.split("/")[-1] == HEAD_REF_NAME:
            found = (Path(workspace_dir) / ref.path).read_text(encoding="utf-8").strip()
            if not _HEX64.fullmatch(found):
                raise StrategyIndiaError("registry_head.txt does not hold a sha256", code="registry_head_invalid")
    explicit = config.get("registry_head_sha256")
    if explicit is not None and not _HEX64.fullmatch(str(explicit)):
        raise StrategyIndiaError("--registry-head must be a lowercase sha256", code="registry_head_invalid")
    if found is not None and explicit is not None and found != explicit:
        raise StrategyIndiaError("the explicit registry head differs from the one in holdout_refs", code="registry_head_invalid")
    pin = explicit if explicit is not None else found
    if pin is None:
        raise StrategyIndiaError(
            f"no registry head pin: list {HEAD_REF_NAME} in holdout_refs or pass --registry-head", code="registry_head_missing"
        )
    return pin


def build_inputs(config: Mapping[str, Any]) -> tuple[StudyInputs, str | None]:
    """Real wiring: 58 private config, the verified 59 dataset and a read-only 59 store.

    Returns the inputs and the registry head pin if one can be resolved (None only when none exists).
    """
    from costs.fills import load_fill_scenarios
    from costs.schedule import load_schedule_set
    from pilot_data.price_bands import BandResolver
    from pilot_data.store import PilotDataStore
    from pilot_data.targets import latest_target_universe
    from pilot_data.universe import UniversePolicy
    from private_config.loader import load_workspace_config

    from .data import DividendUnknownEvent, UniverseEligibility, load_dataset_rows
    from .holdout import load_criteria_file
    from .ticks import load_default_tables

    cfg = load_workspace_config(config["private_dir"], "india")
    assert cfg.strategy is not None and cfg.limits is not None
    workspace_dir = Path(config["private_dir"]) / "india"
    ref = config["criteria"]
    criteria = load_criteria_file(workspace_dir, ref.get("path", ""), ref.get("sha256"))
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
        DividendUnknownEvent(item["anchor_isin"], item["event_id"], date.fromisoformat(item["ex_date"]))
        for item in config.get("dividend_unknown_events", [])
    )
    try:
        pin: str | None = resolve_registry_head(config, cfg.strategy.holdout_refs, workspace_dir)
    except StrategyIndiaError as exc:
        if exc.code != "registry_head_missing":
            raise
        pin = None
    inputs = StudyInputs(
        rows=rows, dataset_sha256=manifest.dataset_sha256, params_raw=cfg.strategy.params, limits=cfg.limits,
        coverage_path=Path(config["coverage_report"]),
        eligibility=UniverseEligibility(store, targets, UniversePolicy(), mode="research"),
        universe_policy=UniversePolicy(), bands=_Bands(), scenarios=load_fill_scenarios(), schedules=schedules,
        schedule_version=config.get("schedule_version", schedules.versions[-1].version), ticks=load_default_tables(),
        fold_rules=FoldRules(**config["fold_rules"]), git_commit=config["git_commit"],
        parameter_budget_n=int(config["parameter_budget_n"]), registry=Registry(Path(config["registry"])),
        targets_sha256=targets.target_sha256, criteria=criteria, events=events,
        tri_path=Path(config["tri"]["path"]) if config.get("tri") else None,
        tri_sha256=config["tri"]["sha256"] if config.get("tri") else None,
    )
    return inputs, pin


def _need_pin(pin: str | None) -> str:
    if pin is None:
        raise StrategyIndiaError(
            f"no registry head pin: list {HEAD_REF_NAME} in holdout_refs or pass --registry-head", code="registry_head_missing"
        )
    return pin


def cli_register(config: Mapping[str, Any]) -> dict[str, Any]:
    inputs, pin = build_inputs(config)
    entry = register(inputs, hypothesis=config.get("hypothesis", "cross-sectional momentum with a swing exit"),
                     expected_head=pin)
    return {"registered": entry.entry_hash, "registry_head": inputs.registry.head_hash(),
            "next": f"pin this head in {HEAD_REF_NAME} (holdout_refs) or pass --registry-head"}


def cli_run(config: Mapping[str, Any]) -> dict[str, Any]:
    inputs, pin = build_inputs(config)
    report = run_research(inputs, expected_head=_need_pin(pin))
    path = write_report(Path(config["report_root"]), report)
    return {"report": str(path), "report_sha256": report.report_sha256, "registry_head": inputs.registry.head_hash()}


def cli_holdout(config: Mapping[str, Any], *, logged_at: str) -> dict[str, Any]:
    inputs, pin = build_inputs(config)
    outcome = run_holdout(inputs, expected_head=_need_pin(pin), logged_at=logged_at)
    path = write_report(Path(config["report_root"]), outcome.report)
    return {"report": str(path), "verdict": outcome.verdict.verdict, "holdout_event": outcome.event_hash,
            "registry_head": inputs.registry.head_hash(),
            "next": f"the holdout is spent; pin the new head in {HEAD_REF_NAME} or pass --registry-head"}

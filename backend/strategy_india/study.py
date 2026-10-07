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
import os
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
from .data import BandSource, DatasetView, DividendEvents, EligibilitySource, check_events_against_rows, dataset_digest
from .engine import RunContext, fit_fold_components, run_holdout_segment, run_walk_forward
from .errors import HoldoutInvalid, HoldoutSpent, RegistryError, RegistryMismatch, StrategyIndiaError
from .folds import FoldRules
from .gate import GateResult, load_coverage_report
from .holdout import (
    HoldoutRange,
    HoldoutVerdict,
    check_gate_scenario,
    evaluate_verdict,
    holdout_digest,
    parse_criteria,
    holdout_range_for,
    open_holdout,
)
from .hurdle import hurdle_map_sha256
from .params import StrategyParams, params_sha256, parse_params
from .registry import (
    Entry, LIVE_CHECKED_FIELDS, Registry, canonical_sha256, check_live_inputs, criteria_hash,
    durable_jsonl, trim_incomplete_tail,
)
from .report import Unit, StudyReport, build_report, holdout_evidence, write_report
from .signals import MODE_BASE
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
    check_events_against_rows(inputs.events, inputs.rows)
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
    criteria = parse_criteria(required_criteria(inputs))
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
    entry = reg.register(record)
    write_head_file(reg)
    return entry


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


SPENT_LEDGER_NAME = "spent_holdouts.jsonl"  # append-only, private, next to the registry
HEAD_LATEST_NAME = "registry_head_latest.txt"  # written by the tool after every registry append (0600)


def _private_append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        trim_incomplete_tail(fd, path)
    except BaseException:
        os.close(fd)
        raise
    size_before = os.fstat(fd).st_size
    try:
        data = memoryview((line + "\n").encode("utf-8"))
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
    except OSError:
        # A failed reservation must not leave a partial or complete ledger line.
        os.ftruncate(fd, size_before)
        try:
            os.fsync(fd)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


def spent_ledger_path(registry: Registry) -> Path:
    return registry.path.with_name(SPENT_LEDGER_NAME)


def head_file_path(registry: Registry) -> Path:
    return registry.path.with_name(HEAD_LATEST_NAME)


def write_head_file(registry: Registry) -> str:
    """Record the current head next to the registry so a stale pin is noticed (0600)."""
    head = registry.head_hash()
    path = head_file_path(registry)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(head + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return head


def _heal_head_file(registry: Registry, recorded: str, pin: str) -> bool:
    """The pin is the verified chain head and the file holds an earlier entry of that chain: rewrite the file.

    This is the state a failed head write after the verdict leaves. A pin whose file hash is not in the chain
    stays refused.
    """
    try:
        chain = registry.entries(expected_head=pin)
    except RegistryError:
        return False
    if recorded not in {entry.entry_hash for entry in chain[:-1]}:
        return False
    try:
        write_head_file(registry)
    except OSError as exc:
        raise StrategyIndiaError("the recorded registry head is stale and could not be rewritten",
                                 code="registry_head_stale") from exc
    return True


def check_pin_fresh(registry: Registry, pin: str) -> None:
    """Refuse a pin that is not the head the tool last wrote (a pre-open pin replayed after the event was deleted)."""
    path = head_file_path(registry)
    try:
        if not path.exists():
            if registry.path.exists() and registry.entries():
                raise StrategyIndiaError("a non-empty registry has no recorded head", code="registry_head_stale")
            return
        recorded = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise StrategyIndiaError("the recorded registry head is unreadable", code="registry_head_stale") from exc
    if recorded != pin and _heal_head_file(registry, recorded, pin):
        return
    if recorded != pin:
        raise StrategyIndiaError(
            "the pinned registry head is stale: it is not the head this tool last recorded", code="registry_head_stale"
        )


def _ledger_records(registry: Registry) -> list[dict[str, Any]]:
    path = spent_ledger_path(registry)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = durable_jsonl(path).decode("utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise StrategyIndiaError("spent-holdouts ledger is unreadable", code="ledger_invalid") from exc
    for number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
            HoldoutRange.from_payload(record["holdout_range"])
            if not isinstance(record.get("registration_entry_hash"), str) or not _HEX64.fullmatch(
                record["registration_entry_hash"]
            ):
                raise ValueError("missing registration hash")
            out.append(record)
        except (ValueError, KeyError, TypeError) as exc:
            raise StrategyIndiaError(f"spent-holdouts ledger line {number} is malformed", code="ledger_invalid") from exc
    return out


def interrupted_reservations(registry: Registry) -> list[dict[str, Any]]:
    """Durable ledger reservations with no matching open remain spent and INVALID."""
    opens = registry.holdout_events()
    return [record for record in _ledger_records(registry) if not any(
        opened.payload["registration_entry_hash"] == record["registration_entry_hash"]
        and opened.payload["holdout_range"] == record["holdout_range"] for opened in opens
    )]


def check_ledger_clear(registry: Registry, holdout: HoldoutRange) -> None:
    """Refuse every overlapping spend, including a reservation interrupted before the open."""
    interrupted = interrupted_reservations(registry)
    for record in _ledger_records(registry):
        if HoldoutRange.from_payload(record["holdout_range"]).overlaps(holdout):
            if record in interrupted:
                raise HoldoutInvalid("INVALID: interrupted; the ledger reserved the holdout without a matching open")
            raise HoldoutSpent("the private spent-holdouts ledger lists this holdout as spent")


def record_spent(registry: Registry, entry: Entry, holdout: HoldoutRange) -> None:
    """Reserve the range durably before appending the open event or reading holdout data."""
    try:
        _private_append(
            spent_ledger_path(registry),
            json.dumps({"holdout_range": holdout.as_payload(), "registration_entry_hash": entry.entry_hash},
                       sort_keys=True),
        )
    except OSError as exc:
        raise StrategyIndiaError("spent-holdouts ledger cannot be written; the holdout stays sealed",
                                 code="ledger_invalid") from exc


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


def preflight_holdout(inputs: StudyInputs, prep: Prepared, criteria: Mapping[str, Any], *, etf_anchor: str,
                      dev_ctx: RunContext) -> None:
    """Everything that can be known before the holdout is opened. Any failure refuses WITHOUT spending it.

    The contract is that nothing that can be checked in advance may fail after the open. Uses session dates,
    table coverage, stored-input availability and development data only, with two holdout price reads: the
    ETF's first and last raw close (a validity check, not an outcome), and, for the A5 ETF tick inference, the
    benchmark ETF's open, high, low and close on every session of the uncovered window, holdout sessions
    included (done when the tick tables are built, before this runs). Only the inferred tick, the method and
    the provenance hash reach the operator output, plus, when the inference refuses, a failure category with no digit
    in it; never a count, a price or a date from those sessions (the counts stay in the sealed provenance). The
    registry, gate, D-19 criteria hash and D-20 event hash were already checked against the registration by the caller.
    """
    try:
        parse_criteria(criteria)  # every policy value must be one the verdict logic honours
    except StrategyIndiaError as exc:
        raise unrunnable(f"the sealed criteria cannot be honoured ({exc})") from exc
    check_ledger_clear(inputs.registry, prep.holdout)
    gate_scenario = inputs.scenarios.gate()
    check_gate_scenario(criteria, k_ticks=gate_scenario.k_ticks, phase62_gate=gate_scenario.phase62_gate)
    sealed_factor(inputs, criteria)
    if not inputs.rows or not all(hasattr(row, "payload") for row in inputs.rows):
        raise unrunnable("the dataset rows cannot be verified against dataset_sha256")
    if dataset_digest(inputs.rows, inputs.events) != inputs.dataset_sha256:
        raise unrunnable("the dataset rows do not reproduce dataset_sha256")
    days = [day for day in prep.sessions if prep.holdout.contains(day)]
    day_set = set(days)
    if len(days) < 2:
        raise unrunnable("the holdout range needs at least two sessions")
    for instrument_class in (EQUITY, NON_GOLD_ETF):
        if instrument_class not in inputs.ticks.classes():
            raise unrunnable(f"no tick table is registered for instrument class {instrument_class}")
        security = etf_anchor if instrument_class == NON_GOLD_ETF else None  # only the benchmark ETF may use an inferred tick
        gaps = [day for day in days if not inputs.ticks.covers(instrument_class, day, series="EQ", security=security)]
        if gaps:
            why = inputs.ticks.uncovered_reason(instrument_class, gaps[0], security=security)
            if why:  # a category only: the count and first date of the gap come from the same holdout sample
                raise unrunnable(f"the {instrument_class} tick table does not cover the holdout (inferred tick unavailable: {why})")
            raise unrunnable(
                f"the {instrument_class} tick table does not cover {len(gaps)} holdout sessions, first {gaps[0].isoformat()}"
            )
    # The registered ETF benchmark must exist on EVERY holdout session, or it would be silently truncated or fail later.
    etf_rows = {row.trade_date: row for row in inputs.rows if row.anchor_isin == etf_anchor}
    absent = [day for day in days if day not in etf_rows]
    if absent:
        raise unrunnable(f"the benchmark ETF has no bar on {len(absent)} of {len(days)} holdout sessions, first {absent[0].isoformat()}")
    # The benchmark must remain runnable. Universe series changes become ineligibility or missed fills.
    for row in inputs.rows:
        if row.trade_date not in day_set or row.anchor_isin != etf_anchor:
            continue
        instrument_class = NON_GOLD_ETF
        if not inputs.ticks.covers(instrument_class, row.trade_date, series=row.series, security=etf_anchor):
            raise unrunnable(
                f"the {instrument_class} tick table does not cover series {row.series!r} "
                f"on {row.trade_date.isoformat()} for {row.anchor_isin}"
            )
    first, last = etf_rows[days[0]], etf_rows[days[-1]]
    if first.raw_close <= 0 or last.raw_close <= 0:
        raise unrunnable("the benchmark ETF has a non-positive raw close on its first or last holdout session")
    if inputs.limits.capital_cap // first.raw_close < 1:
        raise unrunnable("the capital cap cannot buy one benchmark ETF share at the first holdout session")
    # Eligibility inputs must exist for every holdout decision date, where the source can say so.
    available = getattr(inputs.eligibility, "inputs_available", None)
    if available is not None:
        for day in days:
            reason = available(day)
            if reason is not None:
                raise unrunnable(f"eligibility inputs are missing: {reason}")
    # The regime filter is fit on development rows only, so a trial fit can run now.
    try:
        features = dev_ctx.table(MODE_BASE).market_features()
        fit_fold_components(dev_ctx, features, cutoff=prep.view.visible_end, observations=None)
    except (StrategyIndiaError, ValueError) as exc:
        raise unrunnable(f"the regime filter cannot be fit on development data ({type(exc).__name__})") from exc


@dataclass(frozen=True)
class HoldoutOutcome:
    report: StudyReport
    verdict: HoldoutVerdict
    event_hash: str
    report_path: Path | None = None


def run_holdout(inputs: StudyInputs, *, expected_head: str, logged_at: str | None = None,
                registration_hash: str | None = None, report_root: Path | None = None) -> HoldoutOutcome:
    """Open the holdout once (logged in the registry) and judge it against the sealed D-19 criteria."""
    require_head(expected_head)
    reg = inputs.registry
    entry = _registered(inputs, registration_hash, "the holdout stays sealed")
    check_ledger_clear(reg, HoldoutRange.from_payload(entry.payload["holdout_range"]))
    check_pin_fresh(reg, expected_head)
    prep = prepare(inputs)
    criteria = required_criteria(inputs)
    window = (prep.sessions[0], prep.sessions[-1])
    gate = _gate_for_registration(inputs, prep, entry, window)
    etf_anchor = entry.payload["benchmark_ids"][0]
    live = live_inputs(inputs, prep, gate, etf_anchor, criteria)
    entry = require_registration(reg, live, expected_head=expected_head, entry_hash=entry.entry_hash)
    dev_ctx = _context(inputs, prep, gate, prep.view, criteria)
    preflight_holdout(inputs, prep, criteria, etf_anchor=etf_anchor, dev_ctx=dev_ctx)  # nothing after this can be known in advance
    gate_scenario = inputs.scenarios.gate()
    entries_before_open = reg.entries(expected_head=expected_head)
    for event in entries_before_open:
        if event.kind == "holdout_open" and (
            event.payload["registration_entry_hash"] == entry.entry_hash
            or prep.holdout.overlaps(HoldoutRange.from_payload(event.payload["holdout_range"]))
        ):
            raise HoldoutSpent("the registry already records this holdout as spent")
    record_spent(reg, entry, prep.holdout)
    try:
        grant = open_holdout(reg, criteria=criteria, expected_head=expected_head, registration_hash=entry.entry_hash,
                             logged_at=logged_at)
        write_head_file(reg)
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
        verdict_payload = report.holdout_verdict
        reg.append_holdout_verdict({
            "holdout_open_event_hash": grant.event_hash, "registration_entry_hash": entry.entry_hash,
            "verdict": verdict.verdict, "verdict_payload": verdict_payload,
            "verdict_sha256": canonical_sha256(verdict_payload),
        })
        write_head_file(reg)
        report_path = write_report(report_root, report) if report_root is not None else None
    except BaseException as exc:  # the holdout is already spent: record a typed INVALID verdict, never a silent spend
        # An append can succeed before its caller raises. Recover the durable open in that case.
        opened_events = [event for event in reg.holdout_events()
                         if event.seq >= len(entries_before_open)
                         and event.payload["registration_entry_hash"] == entry.entry_hash]
        if not opened_events:
            raise HoldoutInvalid("INVALID: interrupted; the ledger reserved the holdout but its open could not be recorded") from exc
        opened_hash = opened_events[-1].entry_hash
        result = next(result for result in reg.holdout_results() if (
            result.entry_hash == opened_hash or result.payload.get("holdout_open_event_hash") == opened_hash
        ))
        if result.kind == "holdout_verdict" or (
            result.kind == "holdout_invalid" and result.payload["reason"] != "in_progress"
        ):
            try:
                write_head_file(reg)  # retry: the file must not stay on a pre-outcome head
            except OSError:
                pass  # check_pin_fresh heals a file that is an earlier entry of the verified chain
            raise HoldoutInvalid(
                f"the durable {result.payload.get('verdict', 'INVALID')} outcome is retained; "
                f"post-outcome processing failed ({type(exc).__name__})"
            ) from exc
        reg.append_holdout_invalid({
            "holdout_open_event_hash": opened_hash, "registration_entry_hash": entry.entry_hash,
            "error_type": type(exc).__name__, "error_code": getattr(exc, "code", "unexpected_error"),
            "reason": "interrupted" if not isinstance(exc, Exception) else "evaluation_failed",
        })
        try:
            write_head_file(reg)
        except OSError:
            pass  # INVALID is already durable; a head-write failure must not mask it.
        raise HoldoutInvalid(
            f"INVALID: {'interrupted' if not isinstance(exc, Exception) else 'evaluation_failed'}; "
            f"the holdout was opened and then failed ({type(exc).__name__}); INVALID was recorded in the registry"
        ) from exc
    return HoldoutOutcome(report, verdict, grant.event_hash, report_path)


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


def expected_dataset_sha256(config: Mapping[str, Any], *, bind_to_registration: bool) -> str | None:
    """The dataset hash the load must reproduce: the registered one for a run or a holdout, optionally an explicit
    ``dataset_sha256`` from the config (it must then agree). A first registration has nothing registered yet."""
    explicit = config.get("dataset_sha256")
    registered: str | None = None
    registry = Registry(Path(config["registry"]))
    if bind_to_registration and registry.path.exists() and registry.registrations():
        registered = registry.registration().payload["dataset_sha256"]
    if explicit is not None and registered is not None and explicit != registered:
        raise StrategyIndiaError("the configured dataset_sha256 differs from the registered one", code="dataset_mismatch")
    return registered if registered is not None else explicit


def load_bound_dataset(config: Mapping[str, Any], *, bind_to_registration: bool) -> tuple[Any, list[Any]]:
    """Load the 59 dataset directory, refusing before any evaluation if it is not the registered dataset."""
    from .data import load_dataset_rows

    return load_dataset_rows(Path(config["dataset_dir"]),
                             expected_dataset_sha256=expected_dataset_sha256(config, bind_to_registration=bind_to_registration))


def build_inputs(config: Mapping[str, Any], *, bind_to_registration: bool = True) -> tuple[StudyInputs, str | None]:
    """Real wiring: 58 private config, the verified 59 dataset and a read-only 59 store.

    ``bind_to_registration`` (research, preflight and holdout) makes the dataset load refuse any dataset whose hash
    differs from the registered one, so an edited and re-hashed copy in a new directory cannot load.

    Returns the inputs and the registry head pin if one can be resolved (None only when none exists).
    """
    from costs.fills import load_fill_scenarios
    from costs.schedule import load_schedule_set
    from pilot_data.price_bands import BandResolver
    from pilot_data.store import PilotDataStore
    from pilot_data.targets import latest_target_universe
    from pilot_data.universe import UniversePolicy
    from private_config.loader import load_workspace_config

    from .data import UniverseEligibility, events_from_manifest
    from .holdout import load_criteria_file
    from .ticks import load_default_tables

    cfg = load_workspace_config(config["private_dir"], "india")
    assert cfg.strategy is not None and cfg.limits is not None
    workspace_dir = Path(config["private_dir"]) / "india"
    ref = config["criteria"]
    criteria = load_criteria_file(workspace_dir, ref.get("path", ""), ref.get("sha256"))
    manifest, rows = load_bound_dataset(config, bind_to_registration=bind_to_registration)
    # A5: the configured benchmark candidates get an inferred ETF tick for the uncovered window. The inference
    # provenance is part of tick_table_sha256, which the registration seals.
    benchmark_isins = parse_params(cfg.strategy.params).benchmark.candidate_isins
    store = PilotDataStore(Path(config["store_root"]), workspace="india", read_only=True)
    targets = latest_target_universe(store, workspace="india")
    if targets is None:
        raise StrategyIndiaError("the 59 store has no target universe", code="targets_missing")
    resolver = BandResolver(store, record_quarantines=False)

    class _Bands:
        def observe(self, isin: str, session: date):
            return resolver.observe(isin, session)

    schedules = load_schedule_set()
    events = events_from_manifest(manifest)  # sealed as dividend_events_sha256 by the registration
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
        schedule_version=config.get("schedule_version", schedules.versions[-1].version), ticks=load_default_tables(rows=rows, benchmark_isins=benchmark_isins),
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
    inputs, pin = build_inputs(config, bind_to_registration=False)
    if pin is not None:
        check_pin_fresh(inputs.registry, pin)
    entry = register(inputs, hypothesis=config.get("hypothesis", "cross-sectional momentum with a swing exit"),
                     expected_head=pin)
    return {"registered": entry.entry_hash, "registry_head": inputs.registry.head_hash(),
            "etf_tick_inference": [  # D-12: no sample counts, they are computed over holdout sessions too
                {key: record[key] for key in ("security", "status", "tick", "method", "provenance_sha256")}
                for record in inputs.ticks.inference_provenance()
            ],
            "next": f"pin this head in {HEAD_REF_NAME} (holdout_refs) or pass --registry-head"}


def cli_run(config: Mapping[str, Any]) -> dict[str, Any]:
    inputs, pin = build_inputs(config)
    check_pin_fresh(inputs.registry, _need_pin(pin))
    report = run_research(inputs, expected_head=_need_pin(pin))
    path = write_report(Path(config["report_root"]), report)
    return {"report": str(path), "report_sha256": report.report_sha256, "registry_head": inputs.registry.head_hash()}


def cli_holdout(config: Mapping[str, Any], *, logged_at: str) -> dict[str, Any]:
    inputs, pin = build_inputs(config)
    registration = inputs.registry.registration()
    for result in inputs.registry.holdout_results():
        if result.payload["registration_entry_hash"] != registration.entry_hash:
            continue
        if result.kind in ("holdout_open", "holdout_invalid"):
            reason = result.payload.get("reason", "evaluation_failed")
            if result.kind == "holdout_open" or reason == "in_progress":
                reason = "interrupted"
            raise HoldoutInvalid(f"INVALID: {reason}; {_anchor_instruction(inputs.registry)}")
    check_ledger_clear(inputs.registry, HoldoutRange.from_payload(registration.payload["holdout_range"]))
    check_pin_fresh(inputs.registry, _need_pin(pin))
    try:
        outcome = run_holdout(inputs, expected_head=_need_pin(pin), logged_at=logged_at,
                              report_root=Path(config["report_root"]))
    except HoldoutInvalid as exc:
        raise HoldoutInvalid(f"{exc}; {_anchor_instruction(inputs.registry)}") from exc
    path = outcome.report_path
    return {"report": str(path), "verdict": outcome.verdict.verdict, "holdout_event": outcome.event_hash,
            "registry_head": inputs.registry.head_hash(),
            "next": _anchor_instruction(inputs.registry)}


def _anchor_instruction(registry: Registry) -> str:
    return (f"the holdout is spent; record post-open registry head {registry.head_hash()} in Phase 58 holdout_refs "
            f"via {HEAD_REF_NAME}, and have the orchestrator record its hash in the phase SUMMARY")

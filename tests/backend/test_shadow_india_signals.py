"""Phase 64-03 Task 2B: the signal inputs of one shadow decide (D-17, D-22, D-31, D-32, Q-2, Q-7).

Two kinds of rows. Stub rows replace the four 59 reads (``bars_as_of``, freshness, the surveillance lists, the band
for S) with recorders over synthetic 62 rows, so a row can shock, quarantine or count a call exactly. Real rows
(``# real``) run the whole step on the 64-08 world: a seeded session store, the real ``bars_as_of`` and the real
band call. Every value is synthetic. Nothing reads ``private/``, the network, a broker, the relay or the VM.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import socket
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pilot_data import reference_refresh as refresh
from pilot_data.core import PilotDataError, standard_caveats
from pilot_data.dataset import DividendAmountUnknownEvent
from pilot_data.live_bars import LiveBarsResult, bars_as_of
from pilot_data.models import CrossCheckTolerances
from pilot_data.nse_http import NseHttp
from pilot_data.price_bands import BandObservation
from pilot_data.session_store import SEEDS_DDL
from pilot_data.store import PilotDataStore
from pilot_data.surveillance import ingest_surveillance
from pilot_data.universe import UniversePolicy
from shadow_india import policy, signals
from shadow_india.evidence import EvidenceStore
from shadow_india.signals import (
    HASH_NAMES,
    SignalInputs,
    SignalsError,
    StrategyConfig,
    decision_dataset_view,
    evaluate_session,
    evaluation_due,
    load_strategy_config,
    previous_session,
    sessions_before,
)
from strategy_india.data import (
    DatasetView, DecisionView, DividendEvents, UniverseEligibility, events_from_manifest,
)
from strategy_india.errors import HoldoutViolation
from strategy_india.holdout import HoldoutRange
from strategy_india.params import placeholder_params
from strategy_india.signals import MODE_BASE, DecisionContext, MomentumProvider, SignalTable, zscores

import pilot_data_testkit as kit
import test_pilot_data_crosscheck as cc
import test_pilot_data_live_bars as lb
import test_pilot_data_nse_calendar as nc
import test_pilot_data_reference_refresh as rrt
import test_strategy_india_support as sup

CANARY_TOKEN = "CANARY-SESSION-TOKEN-7731"
CANARY_IP = "203.0.113.77"
CANARY_ACCOUNT = "CANARY-ACCT-48151"
FORBIDDEN = (CANARY_TOKEN, CANARY_IP, CANARY_ACCOUNT)
FIXED = datetime(2026, 10, 19, 4, 0, tzinfo=timezone.utc)
REAL_SEED_ROWS = signals.seed_rows  # the ``stubs`` fixture replaces the module attribute, not this reference

HIST = sup.weekday_sessions(date(2026, 9, 7), 30)  # Mon 7 Sept to Fri 16 Oct 2026
AS_OF = HIST[-1]
S = date(2026, 10, 19)  # the decision session, a Monday: the previous session is Friday AS_OF
NAMES = sup.default_names(12)
ANCHORS = [spec.anchor for spec in NAMES]
S_TARGETS = cc.make_targets(*[cc.member(spec.code, spec.anchor, spec.code) for spec in NAMES])


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network used in a signals test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(NseHttp, "__init__", refuse)


# --- builders ------------------------------------------------------------------------------------------------


def strategy(**overrides: Any) -> StrategyConfig:
    params = sup.params(**overrides)
    return StrategyConfig(params=params, params_sha256=sha("params"), params_version="fixture-1",
                          limits=sup.limits(), limits_sha256=sha("limits"))


def _new_dir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def make_calendar(tmp_path: Path, **kwargs: Any):
    kwargs.setdefault("coverage", (date(2025, 1, 1), date(2026, 12, 31)))
    return nc.pinned(_new_dir(tmp_path, f"cal-{len(list(tmp_path.glob('cal-*')))}"), **kwargs)


def make_inputs(tmp_path: Path, *, store: Any = None, config: StrategyConfig | None = None, targets=S_TARGETS,
                run_start: date | None = None, eligibility=None, window_start: date = HIST[0], **overrides: Any) -> SignalInputs:
    return SignalInputs(
        store=store if store is not None else SimpleNamespace(name="session-store"), strategy=config or strategy(),
        targets=targets, universe_policy=UniversePolicy(), calendar=make_calendar(tmp_path),
        tolerances=CrossCheckTolerances(), window_start=window_start, run_start_session=run_start or AS_OF,
        **{"dataset_sha256": sha("dataset"), "tick_sha256": sha("tick"), "cost_sha256": sha("cost"),
           "scenario_sha256": sha("scenario"), "eligibility": eligibility, **overrides})


def make_evidence(tmp_path: Path, inputs: SignalInputs, *, forbidden=()) -> EvidenceStore:
    return EvidenceStore.create(tmp_path / f"ev-{len(list(tmp_path.glob('ev-*')))}", run_id="run1", run_class="plumbing", session_id="s-1",
                                hashes=inputs.current_hashes(), holdout_dated_inputs=True, clock=lambda: FIXED,
                                forbidden=forbidden)


def live_result(rows, *, as_of: date = AS_OF, decision: date = S, composition: str = "c" * 64, lineages=None,
                factor_sets=None, unknown=None, sources=()) -> LiveBarsResult:
    return LiveBarsResult(
        workspace="india", caveats=standard_caveats(), decision_session=decision, as_of=as_of, window_start=HIST[0],
        targets_sha256="t" * 64, tolerances=CrossCheckTolerances(), rows=tuple(rows), lineage_hashes=lineages or {},
        factor_set_hashes=factor_sets or {}, dividend_amount_unknown_events=unknown or {}, source_sha256s=tuple(sources),
        store_crosscheck_run_id="run", composition_sha256=composition)


def history(**kwargs: Any):
    return sup.make_rows(HIST, NAMES, **kwargs)


def future(*, seed: str = "future", scale: Decimal = Decimal(1), days: int = 2):
    rows = sup.make_rows([S + timedelta(days=i) for i in range(days)], NAMES, seed=seed)
    if scale == 1:
        return rows
    out = []
    for row in rows:
        prices = {name: getattr(row, name) * scale for name in ("raw_open", "raw_high", "raw_low", "raw_close",
                                                                  "adj_open", "adj_high", "adj_low", "adj_close")}
        out.append(row.model_copy(update=prices))
    return out


@dataclass
class Stubs:
    """The four 59 reads, recorded. ``rows`` is what ``bars_as_of`` hands back."""

    rows: list[Any]
    lists: dict[str, list[tuple[str | None, str]]] = field(default_factory=lambda: {"asm": [], "gsm": []})
    missing_list: str | None = None
    fresh_error: PilotDataError | None = None
    live_error: PilotDataError | None = None
    live_calls: list[dict[str, Any]] = field(default_factory=list)
    fresh_calls: list[date] = field(default_factory=list)
    fresh_calendars: list[Any] = field(default_factory=list)
    band_error: PilotDataError | None = None
    convention: Any = None
    composition: str = "c" * 64
    lineages: dict[str, str] = field(default_factory=dict)
    factor_sets: dict[str, str] = field(default_factory=dict)
    unknown: dict[str, Any] = field(default_factory=dict)
    sources: tuple[str, ...] = ()
    band_calls: list[dict[str, Any]] = field(default_factory=list)

    def bars(self, store, **kwargs):
        self.live_calls.append(kwargs)
        if self.live_error is not None:
            raise self.live_error
        return live_result(self.rows, as_of=kwargs["as_of"], decision=kwargs["decision_session"],
                           composition=self.composition, lineages=self.lineages, factor_sets=self.factor_sets,
                           unknown=self.unknown, sources=self.sources)

    def freshness(self, store, *, today_ist, calendar=None):
        self.fresh_calls.append(today_ist)
        self.fresh_calendars.append(calendar)
        if self.fresh_error is not None:
            raise self.fresh_error

    def snapshot(self, store, kind, effective):
        if self.missing_list == kind:
            return None
        return SimpleNamespace(source_sha256=sha(f"{kind}-{effective}"), list_name=kind)

    def entries(self, store, ref):
        return tuple(SimpleNamespace(isin=isin, nse_symbol=symbol) for isin, symbol in self.lists[ref.list_name])

    def seeds(self, store):
        return [["a" * 64, HIST[0].isoformat(), AS_OF.isoformat()]]

    def band_convention(self, store):
        return self.convention

    def band(self, store, *, isin, prior_session, session, calendar, workspace):
        self.band_calls.append({"isin": isin, "prior_session": prior_session, "session": session})
        if self.band_error is not None:
            raise self.band_error
        return BandObservation(isin=isin, session=session, status="fixed", percent=Decimal("20"), nse_symbol="X",
                               series="EQ", source_kind="list", source_sha256s=(sha("band"),), reason=None)


@pytest.fixture
def stubs(monkeypatch):
    held = Stubs(rows=history())
    monkeypatch.setattr(signals, "bars_as_of", held.bars)
    monkeypatch.setattr(signals, "require_reference_freshness", held.freshness)
    monkeypatch.setattr(signals, "snapshot_for", held.snapshot)
    monkeypatch.setattr(signals, "surveillance_entries", held.entries)
    monkeypatch.setattr(signals, "band_for_upcoming_session", held.band)
    monkeypatch.setattr(signals, "seed_rows", held.seeds)
    monkeypatch.setattr(signals, "current_band_convention", held.band_convention)
    return held


def run_step(tmp_path: Path, *, forbidden=(), **kwargs: Any):
    root = _new_dir(tmp_path, f"run-{len(list(tmp_path.glob('run-*')))}")
    kwargs.setdefault("eligibility", sup.StaticEligibility(ANCHORS))
    inputs = make_inputs(root, **kwargs)
    evidence = make_evidence(root, inputs, forbidden=forbidden)
    return inputs, evidence, evaluate_session(evidence, inputs, decision_session=S)


def kinds(evidence: EvidenceStore, kind: str) -> list[dict[str, Any]]:
    return [r for r in evidence.records() if r["kind"] == kind]


def log_bytes(evidence: EvidenceStore) -> bytes:
    return (evidence.run_dir / "evidence.jsonl").read_bytes()


# --- the private loader (D-17, fail closed) ----------------------------------------------------------------


def write_strategy_file(private: Path, params: dict[str, Any]) -> None:
    path = private / "india" / "strategy.json"
    body = json.loads(path.read_text())
    body["params"] = params
    body["params_sha256"] = hashlib.sha256(json.dumps(
        params, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    path.write_text(json.dumps(body))
    os.chmod(path, 0o600)


def test_the_loader_returns_the_plumbing_params_from_a_fixture_file(private_config_dir):
    write_strategy_file(private_config_dir, placeholder_params())
    config = load_strategy_config(private_config_dir)
    assert config.params.rebalance_every == 2 and config.params_version == "test-fixture-1"
    assert len(config.params_sha256) == 64 and len(config.limits_sha256) == 64
    assert "test-fixture-1" in repr(config) and "capital" not in repr(config).lower()
    assert config.strategy_sha256() == config.strategy_sha256() and len(config.strategy_sha256()) == 64


@pytest.mark.parametrize("private, code", [
    (None, "strategy_config_refused:PRIVATE_DIR_MISSING"),
    ("missing-dir", "strategy_config_refused:PRIVATE_DIR_MISSING"),
])
def test_a_missing_private_dir_is_refused_with_a_code_never_a_default(private, code, tmp_path):
    target = None if private is None else tmp_path / private
    with pytest.raises(SignalsError) as refused:
        load_strategy_config(target)
    assert refused.value.code == code


def test_params_the_schema_refuses_are_a_code_that_carries_no_value(private_config_dir):
    write_strategy_file(private_config_dir, {**placeholder_params(), "lookback_sessions": 1, "seed": CANARY_ACCOUNT})
    with pytest.raises(SignalsError) as refused:
        load_strategy_config(private_config_dir)
    assert refused.value.code == "strategy_params_invalid" and CANARY_ACCOUNT not in str(refused.value)


def test_a_params_file_whose_hash_does_not_match_is_refused(private_config_dir):
    path = private_config_dir / "india" / "strategy.json"
    body = json.loads(path.read_text())
    body["params_sha256"] = "0" * 64
    path.write_text(json.dumps(body))
    os.chmod(path, 0o600)
    with pytest.raises(SignalsError) as refused:
        load_strategy_config(private_config_dir)
    assert refused.value.code == "strategy_config_refused:PARAMS_HASH_MISMATCH"


# --- calendar and cadence (OI-11) -----------------------------------------------------------------------------


def test_the_previous_session_skips_weekends_and_holidays_and_needs_coverage(tmp_path):
    calendar = make_calendar(tmp_path, holidays=[date(2026, 10, 15)])
    assert previous_session(calendar, S) == AS_OF
    assert previous_session(calendar, date(2026, 10, 16)) == date(2026, 10, 14)  # Thursday 15 is a holiday
    with pytest.raises(SignalsError) as refused:
        previous_session(calendar, date(2027, 1, 4))
    assert refused.value.code == "session_calendar_unknown"
    short = make_calendar(tmp_path, coverage=(date(2026, 10, 19), date(2026, 12, 31)))
    with pytest.raises(SignalsError) as walk:
        previous_session(short, S)  # the walk back leaves the covered range
    assert walk.value.code == "session_calendar_unknown"


def test_session_counts_follow_the_calendar_and_the_cadence_is_62s(tmp_path):
    calendar = make_calendar(tmp_path)
    assert sessions_before(calendar, S, S) == 0
    assert sessions_before(calendar, AS_OF, S) == 1  # Friday counts, the weekend does not
    assert sessions_before(calendar, date(2026, 10, 12), S) == 5
    with pytest.raises(SignalsError) as refused:
        sessions_before(calendar, date(2026, 10, 20), S)
    assert refused.value.code == "run_start_after_session"
    assert [evaluation_due(i, 2) for i in range(6)] == [True, False, True, False, True, False]
    assert [evaluation_due(i, 3) for i in range(6)] == [True, False, False, True, False, False]


def test_a_non_evaluation_day_records_itself_and_reads_no_price(tmp_path, stubs):
    # run start Friday AS_OF: index of S is 1, so with a 2-session cadence S is not an evaluation day
    inputs, evidence, result = run_step(tmp_path, run_start=AS_OF)
    assert result.status == signals.STATUS_NON_EVALUATION_DAY and result.cadence_index == 1 and not result.evaluation_due
    assert result.holdout_dated_inputs is True and result.signals == ()
    assert stubs.live_calls == [] and stubs.fresh_calls == [] and stubs.band_calls == []  # no price, no list, no band
    rows = kinds(evidence, "non_evaluation_day")
    assert len(rows) == 1 and rows[0]["as_of_date"] == AS_OF.isoformat() and rows[0]["reason"] == "cadence"
    assert "index=1" in rows[0]["cadence_state"] and signals.HOLDOUT_FLAG in rows[0]["cadence_state"]
    assert kinds(evidence, "signal") == []
    again = evaluate_session(evidence, inputs, decision_session=S)  # a restarted decide writes it once
    assert again.status == signals.STATUS_NON_EVALUATION_DAY and len(kinds(evidence, "non_evaluation_day")) == 1


def test_an_evaluation_day_follows_the_cadence_and_a_three_session_cadence_is_honoured(tmp_path, stubs):
    _, _, first = run_step(tmp_path, run_start=S)
    assert first.status == signals.STATUS_EVALUATED and first.cadence_index == 0 and first.evaluation_due
    config = strategy(rebalance_every=3)
    for start, index, status in ((AS_OF, 1, signals.STATUS_NON_EVALUATION_DAY),
                                 (date(2026, 10, 15), 2, signals.STATUS_NON_EVALUATION_DAY),
                                 (date(2026, 10, 14), 3, signals.STATUS_EVALUATED)):
        _, _, result = run_step(tmp_path, config=config, run_start=start)
        assert result.status == status and result.cadence_index == index, start


# --- the evaluation: records, hashes, flags ----------------------------------------------------------------------


def test_an_evaluation_scores_ranks_and_records_every_eligible_name_with_the_nine_pins(tmp_path, stubs):
    inputs, evidence, result = run_step(tmp_path, run_start=S)
    assert result.status == signals.STATUS_EVALUATED and result.holdout_dated_inputs is True
    assert result.as_of == AS_OF and result.session == S and result.surveillance_as_of == AS_OF
    assert [c["decision_session"] for c in stubs.live_calls] == [S] and stubs.live_calls[0]["as_of"] == AS_OF
    assert stubs.fresh_calls == [S]
    assert len(result.signals) == 12 and result.entries_possible
    assert sorted(s.rank for s in result.signals) == list(range(1, 13))
    assert abs(sum(s.zscore for s in result.signals)) < Decimal("1e-20")
    records = kinds(evidence, "signal")
    assert len(records) == 12 == result.recorded
    for record in records:
        pins = inputs.current_hashes()
        assert pins == evidence.manifest.hashes and set(pins) == set(signals.PIN_NAMES)
        assert {name: record[name] for name in HASH_NAMES} == {name: pins[name] for name in HASH_NAMES}
        assert f"{signals.EVALUATION_KEY}={result.evaluation_sha256}" in record["reason"]
        assert record["as_of_date"] == AS_OF.isoformat() and record["run_class"] == "plumbing"
        assert signals.HOLDOUT_FLAG in record["reason"] and f"surveillance_as_of={AS_OF.isoformat()}" in record["reason"]
        assert record["signal_id"] == f"sig-{AS_OF.strftime('%Y%m%d')}-{record['isin']}"
        assert record["adjusted_inputs_sha256"] == result.adjusted_inputs_sha256 != result.raw_inputs_sha256
    assert {r["isin"] for r in records} == set(ANCHORS)


def test_a_restarted_decide_writes_each_signal_once(tmp_path, stubs):
    inputs, evidence, first = run_step(tmp_path, run_start=S)
    again = evaluate_session(evidence, inputs, decision_session=S)
    assert again.decision_bytes() == first.decision_bytes() and again.recorded == 0
    assert len(kinds(evidence, "signal")) == 12


def test_shadow_scores_equal_62s_research_code_on_the_pinned_inputs(tmp_path, stubs):
    """Replay hook: 62's own table, provider and z-scores on the same rows, a research-style view, same answer."""
    _, _, result = run_step(tmp_path, run_start=S)
    rows = stubs.rows
    view = DatasetView.from_rows(rows, holdout=HoldoutRange(S + timedelta(days=400), S + timedelta(days=400)))
    table = SignalTable(view, strategy().params, DividendEvents(), mode=MODE_BASE)
    eligibility = sup.StaticEligibility(ANCHORS).snapshot(AS_OF)
    ctx = DecisionContext(AS_OF, eligibility.eligible, DecisionView(view, AS_OF), table)
    raw = MomentumProvider().scores(ctx)
    z = zscores(raw)
    ranked = {a: i + 1 for i, (a, _) in enumerate(sorted(z.items(), key=lambda kv: (-kv[1], kv[0])))}
    mine = {s.anchor_isin: (s.raw_score, s.zscore, s.rank) for s in result.signals}
    assert mine == {a: (raw[a], z[a], ranked[a]) for a in raw} and len(mine) == 12
    assert result.eligibility_sha256 == eligibility.result_sha256 and result.eligible == eligibility.eligible


def test_eligibility_is_asked_at_s_minus_one_in_pilot_mode(tmp_path, stubs, monkeypatch):
    seen = {}

    class Spy:
        def __init__(self, store, targets, policy_, *, mode="research", **kwargs):
            seen["mode"] = mode
            self.eligibility = sup.StaticEligibility(ANCHORS)

        def snapshot(self, as_of):
            seen["as_of"] = as_of
            return self.eligibility.snapshot(as_of)

    monkeypatch.setattr(signals, "AsOfEligibility", Spy)
    inputs = make_inputs(tmp_path, eligibility=None, run_start=S)  # no injected source: the default is built
    result = evaluate_session(make_evidence(tmp_path, inputs), inputs, decision_session=S)
    assert result.status == signals.STATUS_EVALUATED and seen == {"mode": "pilot", "as_of": AS_OF}


def test_the_injected_eligibility_source_is_asked_exactly_for_s_minus_one(tmp_path, stubs):
    eligibility = sup.StaticEligibility(ANCHORS)
    inputs = make_inputs(tmp_path, eligibility=eligibility, run_start=S)
    evaluate_session(make_evidence(tmp_path, inputs), inputs, decision_session=S)
    assert eligibility.calls == [AS_OF]


# --- the shock test (T-64-17): nothing dated on or after S moves the decision -------------------------------------


def shock_variants():
    return {
        "scaled_up": future(scale=Decimal("1.6")), "scaled_down": future(scale=Decimal("0.4")),
        "other_seed": future(seed="other"), "later_only": future(days=5), "none": [],
    }


def test_changing_any_bar_dated_on_or_after_s_leaves_the_decision_byte_identical(tmp_path, stubs):
    stubs.rows = history()
    _, _, base = run_step(tmp_path, run_start=S)
    reference = (base.decision_bytes(), base.adjusted_inputs_sha256, base.raw_inputs_sha256)
    assert base.signals and base.decision_sha256()
    for name, extra in shock_variants().items():
        stubs.rows = history() + extra
        _, evidence, shocked = run_step(tmp_path, run_start=S)
        assert (shocked.decision_bytes(), shocked.adjusted_inputs_sha256, shocked.raw_inputs_sha256) == reference, name
        assert [r["adjusted_inputs_sha256"] for r in kinds(evidence, "signal")] == [reference[1]] * 12, name


def test_a_shock_to_a_bar_dated_s_minus_one_does_move_the_decision(tmp_path, stubs):
    """The converse, so the shock rows above are not green by accident: a bar the decision may read does matter."""
    _, _, base = run_step(tmp_path, run_start=S)
    stubs.rows = [row if row.trade_date != AS_OF else row.model_copy(update={"adj_close": row.adj_close * Decimal("1.5")})
                  for row in history()]
    _, _, moved = run_step(tmp_path, run_start=S)
    assert moved.decision_bytes() != base.decision_bytes() and moved.adjusted_inputs_sha256 != base.adjusted_inputs_sha256


# --- D-32: the view is sealed at S -----------------------------------------------------------------------------------


def test_a_view_sealed_at_s_refuses_every_read_of_s_or_later(stubs):
    rows = history() + future()
    view = decision_dataset_view(rows, S)
    anchor = ANCHORS[0]
    reads = {
        "bar": lambda: view.bar(anchor, S), "bars_on": lambda: view.bars_on(S), "series_end": lambda: view.series(anchor, end=S),
        "sessions_end": lambda: view.sessions(end=S), "sessions_start": lambda: view.sessions(start=S),
        "calendar_index": lambda: view.calendar_index(S), "previous_bar": lambda: view.previous_bar(anchor, S),
        "bar_later": lambda: view.bar(anchor, S + timedelta(days=1)),
        "decision_view": lambda: DecisionView(view, S).close(anchor, S),
    }
    for name, read in reads.items():
        with pytest.raises(Exception) as refused:
            read()
        assert isinstance(refused.value, HoldoutViolation) or type(refused.value).__name__ == "LookaheadError", name
    assert view.bar(anchor, AS_OF) is not None and view.visible_end == AS_OF  # the day before is readable
    assert max(bar.session for bar in view.series(anchor)) == AS_OF


def test_the_signal_module_never_reaches_the_62_engine_gate_report_or_holdout_opener():
    tree = ast.parse((Path(signals.__file__)).read_text())
    banned_modules = {"engine", "gate", "report", "study", "registry", "benchmark", "hurdle", "folds"}
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("strategy_india"):
            parts = (node.module or "").split(".")
            if len(parts) > 1:
                names.add(parts[1])
            names.update(alias.name for alias in node.names)
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[-1] for alias in node.names)
    assert names.isdisjoint(banned_modules | {"open_holdout", "HoldoutGrant", "run_walk_forward", "run_holdout_segment"})
    assert tree is not None and "holdout" in names  # it does import the range type from 62's holdout module


# --- suppression (D-05): quarantine, a missing bar and thin history never produce a signal ----------------------------


def test_a_quarantined_bar_suppresses_that_signal_and_is_recorded(tmp_path, stubs):
    victim = ANCHORS[3]
    stubs.rows = history(quarantined=[(victim, AS_OF)])
    _, evidence, result = run_step(tmp_path, run_start=S)
    by = {s.anchor_isin: s for s in result.signals}
    assert by[victim].raw_score is None and by[victim].zscore is None and by[victim].rank is None
    assert by[victim].suppressed == signals.SUPPRESS_QUARANTINED
    assert all(s.suppressed is None and s.rank is not None for a, s in by.items() if a != victim)
    assert sorted(s.rank for s in result.signals if s.rank) == list(range(1, 12))
    record = next(r for r in kinds(evidence, "signal") if r["isin"] == victim)
    assert "suppressed=adjusted_quarantined" in record["reason"] and "rank=" not in record["reason"]
    assert len(kinds(evidence, "signal")) == 12  # every eligible name is on the record, scored or suppressed


def test_a_quarantine_inside_the_lookback_window_also_suppresses(tmp_path, stubs):
    victim = ANCHORS[5]
    stubs.rows = history(quarantined=[(victim, HIST[-12])])  # inside lookback 20 + skip 1
    _, _, result = run_step(tmp_path, run_start=S)
    by = {s.anchor_isin: s for s in result.signals}
    assert by[victim].suppressed == signals.SUPPRESS_QUARANTINED and by[victim].raw_score is None


def test_a_missing_s_minus_one_bar_and_thin_history_are_suppressed_with_their_own_reasons(tmp_path, stubs):
    gone, thin = ANCHORS[1], ANCHORS[2]
    rows = [row for row in history(drop=[(gone, AS_OF)]) if not (row.anchor_isin == thin and row.trade_date < HIST[-10])]
    stubs.rows = rows
    _, _, result = run_step(tmp_path, run_start=S)
    by = {s.anchor_isin: s for s in result.signals}
    assert by[gone].suppressed == signals.SUPPRESS_MISSING_BAR and by[thin].suppressed == signals.SUPPRESS_HISTORY
    assert by[gone].raw_score is None and by[thin].raw_score is None
    assert sum(1 for s in result.signals if s.rank is not None) == 10


def test_too_few_scored_names_gives_scores_but_no_entries(tmp_path, stubs):
    keep = ANCHORS[:6]  # placeholder min_universe_for_entry is 8
    stubs.rows = [row for row in history() if row.anchor_isin in keep]
    inputs = make_inputs(tmp_path, eligibility=sup.StaticEligibility(keep), run_start=S)
    result = evaluate_session(make_evidence(tmp_path, inputs), inputs, decision_session=S)
    assert result.status == signals.STATUS_EVALUATED and not result.entries_possible
    assert all(s.raw_score is not None and s.zscore is None and s.rank is None for s in result.signals)


# --- the ASM/GSM overlay (Q-2, Q-7) --------------------------------------------------------------------------------


def test_names_on_the_lists_effective_s_minus_one_are_buy_blocked_and_labelled(tmp_path, stubs):
    stubs.lists = {"asm": [(ANCHORS[0], "STK00")], "gsm": [(None, "STK04")]}  # one by ISIN, one by symbol only
    _, evidence, result = run_step(tmp_path, run_start=S)
    blocked = {s.anchor_isin for s in result.signals if s.buy_blocked}
    assert blocked == {ANCHORS[0], ANCHORS[4]} == set(result.overlay_blocked) and result.surveillance_as_of == AS_OF
    assert len(result.overlay_sha256) == 64
    reasons = {r["isin"]: r["reason"] for r in kinds(evidence, "signal")}
    assert "buy_blocked=surveillance" in reasons[ANCHORS[0]] and "buy_blocked=surveillance" in reasons[ANCHORS[4]]
    assert "buy_blocked" not in reasons[ANCHORS[1]]
    assert all(f"surveillance_as_of={AS_OF.isoformat()}" in reason for reason in reasons.values())


def test_a_missing_surveillance_list_pauses_all_processing(tmp_path, stubs):
    stubs.missing_list = "gsm"
    _, evidence, result = run_step(tmp_path, run_start=S)
    assert result.status == signals.STATUS_PAUSED and result.pause_event == "stale_data"
    assert result.pause_reason == "surveillance_snapshot_missing" and kinds(evidence, "signal") == []
    assert [p.scope for p in policy.run_state(evidence).open_pauses] == ["all"]


# --- bands for S (G-3) -------------------------------------------------------------------------------------------------


def test_the_band_for_s_is_resolved_from_s_minus_one_for_the_names_that_could_be_held(tmp_path, stubs):
    _, _, result = run_step(tmp_path, run_start=S)
    cutoff = strategy().params.hold_rank_cutoff
    expected = {s.isin for s in result.signals if s.rank is not None and s.rank <= cutoff}
    assert set(result.bands) == expected and len(expected) == cutoff
    assert {c["isin"] for c in stubs.band_calls} == expected
    assert all(c["prior_session"] == AS_OF and c["session"] == S for c in stubs.band_calls)


# --- the pins: a hash differing from the manifest pauses all processing (typed code) ---------------------------------------


@pytest.mark.parametrize("name", signals.PIN_NAMES)
def test_a_hash_differing_from_the_manifest_pauses_all_processing(name, tmp_path, stubs):
    inputs = make_inputs(tmp_path, eligibility=sup.StaticEligibility(ANCHORS), run_start=S)
    evidence = make_evidence(tmp_path, inputs)
    changed = {
        "strategy_sha256": replace(inputs, strategy=replace(inputs.strategy, params_version="other-version")),
        "params_sha256": replace(inputs, strategy=replace(inputs.strategy, params_sha256=sha("other"))),
        "dataset_sha256": replace(inputs, dataset_sha256=sha("other")),
        "universe_sha256": replace(inputs, universe_policy=UniversePolicy(min_price=Decimal("51"))),
        "band_sha256": replace(inputs, calendar=nc.pinned(_new_dir(tmp_path, "cal2"), holidays=[date(2026, 12, 25)],
                                                          coverage=(date(2025, 1, 1), date(2026, 12, 31)))),
        "tick_sha256": replace(inputs, tick_sha256=sha("other")),
        "limits_sha256": replace(inputs, strategy=replace(inputs.strategy, limits_sha256=sha("other"))),
        "cost_sha256": replace(inputs, cost_sha256=sha("other")),
        "scenario_sha256": replace(inputs, scenario_sha256=sha("other")),
        "seed_sha256": replace(inputs, window_start=HIST[1]),  # the run window is pinned with the seed rows
    }[name]
    assert changed.current_hashes()[name] != inputs.current_hashes()[name]
    result = evaluate_session(evidence, changed, decision_session=S)
    assert result.status == signals.STATUS_PAUSED and result.pause_event == "hash_mismatch"
    named = result.pause_reason.removeprefix("hash_mismatch:").split(",")
    # the strategy pin is derived from the params pin, so changing the params pin moves both
    assert named == ([name] if name != "params_sha256" else ["strategy_sha256", "params_sha256"])
    assert result.holdout_dated_inputs is True
    assert stubs.live_calls == [] and stubs.fresh_calls == [] and kinds(evidence, "signal") == []
    pause = kinds(evidence, "pause")[0]
    assert pause["event"] == "hash_mismatch" and pause["scope"] == "all"
    assert policy.run_state(evidence).blocks(policy.STEP_BOOK) and policy.run_state(evidence).blocks(policy.STEP_EXPIRE)
    # still paused for the next call, which reads nothing and records nothing
    again = evaluate_session(evidence, inputs, decision_session=S)
    assert again.status == signals.STATUS_PAUSED and again.pause_reason == "already_paused" and len(kinds(evidence, "pause")) == 1
    # only a written reason clears it, and the same mismatch pauses again
    policy.resume(evidence, "operator reviewed the pins")
    repeat = evaluate_session(evidence, changed, decision_session=S)
    assert repeat.status == signals.STATUS_PAUSED and len(kinds(evidence, "pause")) == 2


def test_a_manifest_missing_a_pin_is_a_mismatch(tmp_path, stubs):
    inputs = make_inputs(tmp_path, eligibility=sup.StaticEligibility(ANCHORS), run_start=S)
    hashes = {k: v for k, v in inputs.current_hashes().items() if k != "tick_sha256"}
    evidence = EvidenceStore.create(tmp_path / "ev-pins", run_id="run1", run_class="plumbing", session_id="s-1",
                                    hashes=hashes, holdout_dated_inputs=True, clock=lambda: FIXED)
    result = evaluate_session(evidence, inputs, decision_session=S)
    assert result.status == signals.STATUS_PAUSED and result.pause_reason == "hash_mismatch:tick_sha256"


# --- the other typed pauses and refusals -------------------------------------------------------------------------------------


def test_a_failing_freshness_check_pauses_all_before_any_bar_is_read(tmp_path, stubs):
    stubs.fresh_error = PilotDataError("reference_refresh_stale", f"stale {CANARY_TOKEN}")
    _, evidence, result = run_step(tmp_path, run_start=S)
    assert result.status == signals.STATUS_PAUSED and result.pause_event == "freshness_check_failed"
    assert result.pause_reason == "reference_refresh_stale" and stubs.live_calls == []
    assert kinds(evidence, "pause")[0]["scope"] == "all" and kinds(evidence, "signal") == []


def test_bars_that_cannot_be_composed_pause_all_with_the_pilot_code(tmp_path, stubs):
    stubs.live_error = PilotDataError("stale_as_of", f"detail {CANARY_TOKEN} {CANARY_IP}")
    _, evidence, result = run_step(tmp_path, run_start=S, forbidden=FORBIDDEN)
    assert result.status == signals.STATUS_PAUSED and result.pause_event == "stale_data" and result.pause_reason == "stale_as_of"
    assert kinds(evidence, "signal") == []


def test_an_empty_eligible_universe_pauses_rather_than_writing_nothing(tmp_path, stubs):
    inputs = make_inputs(tmp_path, eligibility=sup.StaticEligibility([]), run_start=S)
    evidence = make_evidence(tmp_path, inputs)
    result = evaluate_session(evidence, inputs, decision_session=S)
    assert result.status == signals.STATUS_PAUSED and result.pause_reason == "eligible_universe_empty"


def test_an_aborted_run_refuses_the_step_and_a_non_session_is_a_contract_error(tmp_path, stubs):
    inputs = make_inputs(tmp_path, eligibility=sup.StaticEligibility(ANCHORS), run_start=S)
    evidence = make_evidence(tmp_path, inputs)
    with pytest.raises(SignalsError) as refused:
        evaluate_session(evidence, inputs, decision_session=date(2026, 10, 17))  # a Saturday
    assert refused.value.code == "decision_session_not_a_session"
    policy.raise_event(evidence, "evidence_corrupt", "test abort")
    with pytest.raises(policy.RunAborted):
        evaluate_session(evidence, inputs, decision_session=S)
    assert stubs.live_calls == []


# --- canaries (T-64-15) -------------------------------------------------------------------------------------------------------


def test_no_token_ip_or_account_id_reaches_a_record_a_result_or_a_repr(tmp_path, stubs):
    """The pilot error text carries canaries; only its code may travel. Every outcome this module records is read."""
    stubs.live_error = PilotDataError("breeze_adjusted_detected", f"{CANARY_TOKEN} {CANARY_IP} {CANARY_ACCOUNT}")
    _, paused_store, paused = run_step(tmp_path, run_start=S, forbidden=FORBIDDEN)
    stubs.live_error = None
    _, skipped_store, skipped = run_step(tmp_path, run_start=AS_OF, forbidden=FORBIDDEN)  # a non-evaluation day
    _, scored_store, scored = run_step(tmp_path, run_start=S, forbidden=FORBIDDEN)
    for store, result in ((paused_store, paused), (skipped_store, skipped), (scored_store, scored)):
        raw = log_bytes(store)
        assert raw and not any(canary.encode() in raw for canary in FORBIDDEN)
        blob = repr(result) + str(result.decision_payload()) + str(result.pause_reason)
        assert not any(canary in blob for canary in FORBIDDEN)
    assert kinds(paused_store, "pause")[0]["reason"] == "signals:breeze_adjusted_detected;holdout_dated_inputs=true"
    assert {r["kind"] for r in scored_store.records()} == {"signal"}


# --- real: the whole step on the 64-08 world ------------------------------------------------------------------------------------

D1, D2, D3, D4, D5 = lb.D1, lb.D2, lb.D3, lb.D4, lb.D5
WORLD_S = lb.S  # Monday 10 March 2025, the real decision session
REAL_ELIGIBLE = [cc.NEW, cc.STEADY_ISIN]


def world_params() -> StrategyConfig:
    return strategy(lookback_sessions=2, skip_sessions=0, vol_window=2, vol_adjusted=False, min_universe_for_entry=2)


def surveillance_lists(*, asm_names=()):
    def add(store):
        asm = [kit.asm_row("OTHERCO", cc.OTHER_ISIN, when="07-Mar-2025")] + [
            kit.asm_row(symbol, isin, when="07-Mar-2025") for symbol, isin in asm_names]
        ingest_surveillance(store, cc.desc("nse_api", "surveillance_asm"), kit.asm_json(asm, []), list_name="asm")
        ingest_surveillance(store, cc.desc("nse_api", "surveillance_gsm"),
                            kit.gsm_json([kit.gsm_row("OTHERCO", cc.OTHER_ISIN, when="07-Mar-2025 08:07:02")]),
                            list_name="gsm")

    return add


def real_world(tmp_path: Path, shocks=frozenset(), *, extra=None, name="world") -> Path:
    root = tmp_path / name
    lb.build_world(root, frozenset(shocks), extra=extra or surveillance_lists())
    return root


_STATIC = object()  # the default eligibility source of ``real_step`` is the injected static one
WORLD_POLICY = UniversePolicy(lookback_sessions=5, min_known_sessions=4, min_median_traded_value=Decimal("1"),
                              min_price=Decimal("1"))


def real_step(tmp_path: Path, root: Path, *, free: bool = True, monkeypatch=None, eligibility: Any = _STATIC,
              universe_policy: UniversePolicy | None = None):
    calendar = nc.pinned(_new_dir(tmp_path, f"cal-{root.name}"), coverage=(date(2025, 3, 1), date(2025, 3, 31)))
    with PilotDataStore(root, workspace="india") as store:
        inputs = SignalInputs(
            store=store, strategy=world_params(), targets=lb.TARGETS, universe_policy=universe_policy or UniversePolicy(),
            calendar=calendar, tolerances=lb.TOLERANCES, window_start=cc.START, run_start_session=D2,
            dataset_sha256=sha("dataset"), tick_sha256=sha("tick"), cost_sha256=sha("cost"),
            scenario_sha256=sha("scenario"),
            eligibility=sup.StaticEligibility(REAL_ELIGIBLE) if eligibility is _STATIC else eligibility)
        evidence = EvidenceStore.create(_new_dir(tmp_path, f"ev-{root.name}"), run_id="run1", run_class="plumbing",
                                        session_id="s-1", hashes=inputs.current_hashes(), holdout_dated_inputs=True,
                                        clock=lambda: FIXED)
        if free and monkeypatch is not None:
            monkeypatch.setattr(signals, "require_reference_freshness", lambda *a, **k: None)
        return inputs, evidence, evaluate_session(evidence, inputs, decision_session=WORLD_S)


def test_real_the_decision_for_s_is_byte_identical_whatever_is_dated_s_or_later(tmp_path, monkeypatch):
    """The end-to-end shock: real session stores whose S and later data differ, real composition, real bands."""
    reference = None
    for name, flags in {"base": set(), "scaled": {"scale"}, "future_deleted": {"no_future"},
                        "everything": {"scale", "new_breeze", "ca_structural", "bands_surv", "fetch_extra", "quarantine",
                                       "relink", "factor_event", "old_bonus"}}.items():
        root = real_world(tmp_path, flags, name=name)
        _, evidence, result = real_step(tmp_path, root, monkeypatch=monkeypatch)
        assert result.status == signals.STATUS_EVALUATED, (name, result.pause_reason)
        assert {s.anchor_isin for s in result.signals} == set(REAL_ELIGIBLE) and result.holdout_dated_inputs
        view = (result.decision_bytes(), result.adjusted_inputs_sha256, result.raw_inputs_sha256,
                result.composition_sha256, result.eligibility_sha256, result.evaluation_sha256,
                sorted(r["signal_id"] for r in kinds(evidence, "signal")))
        reference = reference or view
        assert view == reference, name
    assert result.as_of == D5 and result.surveillance_as_of == D5


def test_real_shadow_equals_62s_research_code_on_the_composed_rows_and_the_eligibility_hash(tmp_path, monkeypatch):
    root = real_world(tmp_path)
    _, _, result = real_step(tmp_path, root, monkeypatch=monkeypatch)
    with PilotDataStore(root, workspace="india") as store:
        live = bars_as_of(store, targets=lb.TARGETS, decision_session=WORLD_S, as_of=D5, window_start=cc.START,
                          tolerances=lb.TOLERANCES, workspace="india")
    assert live.composition_sha256 == result.composition_sha256
    far = date(2030, 1, 1)
    view = DatasetView.from_rows(live.rows, holdout=HoldoutRange(far, far))
    table = SignalTable(view, world_params().params, events_from_manifest(live), mode=MODE_BASE)
    snapshot = sup.StaticEligibility(REAL_ELIGIBLE).snapshot(D5)
    raw = MomentumProvider().scores(DecisionContext(D5, snapshot.eligible, DecisionView(view, D5), table))
    z = zscores(raw)
    assert raw and {s.anchor_isin: (s.raw_score, s.zscore) for s in result.signals} == {a: (raw[a], z[a]) for a in raw}
    assert result.eligibility_sha256 == snapshot.result_sha256 and result.eligible == snapshot.eligible


def test_real_an_unresolved_corporate_action_suppresses_the_signal_and_the_overlay_blocks_a_listed_name(tmp_path, monkeypatch):
    rights = kit.bc_row("EQ", "STEADYCO", "STEADY CO", "RIGHTS 1:1 @ PRM RS 3/-", ex_date=D4)

    def first_seen_d2(store):
        lb.ingest_pr_zip(store, cc.desc(kind="pr_zip"), lb.pr_zip_on(D2, [rights]), trade_date=D2)
        lb.derive_corporate_actions(store, workspace="india")
        surveillance_lists(asm_names=[("CANBK", cc.NEW)])(store)

    root = real_world(tmp_path, extra=first_seen_d2)
    _, evidence, result = real_step(tmp_path, root, monkeypatch=monkeypatch)
    by = {s.anchor_isin: s for s in result.signals}
    assert result.status == signals.STATUS_EVALUATED
    assert by[cc.STEADY_ISIN].raw_score is None and by[cc.STEADY_ISIN].suppressed == signals.SUPPRESS_QUARANTINED
    assert by[cc.NEW].raw_score is not None and by[cc.NEW].buy_blocked and not by[cc.STEADY_ISIN].buy_blocked
    record = next(r for r in kinds(evidence, "signal") if r["isin"] == cc.STEADY_ISIN)
    assert "suppressed=adjusted_quarantined" in record["reason"]


def test_real_the_freshness_check_runs_for_s_and_a_store_without_refresh_runs_pauses_all(tmp_path, monkeypatch):
    root = real_world(tmp_path)
    _, evidence, result = real_step(tmp_path, root, free=False)  # the real require_reference_freshness
    assert result.status == signals.STATUS_PAUSED and result.pause_event == "freshness_check_failed"
    assert kinds(evidence, "signal") == [] and kinds(evidence, "pause")[0]["scope"] == "all"


# --- review round 1 (Codex r1, Opus r1 at 21105911) --------------------------------------------------------------------


def fresh_extra(store):
    surveillance_lists()(store)
    lb.pb.put_list(store, D5)
    rrt.record_success(store, D5)


def late_split_eligibility(tmp_path, flags, name):
    """The default eligibility source and 62's, each on the same store, as the decision at S sees it."""
    root = real_world(tmp_path, flags, name=name, extra=fresh_extra)
    _, evidence, result = real_step(tmp_path, root, eligibility=None, universe_policy=WORLD_POLICY)
    with PilotDataStore(root, workspace="india") as store:
        sixty_two = UniverseEligibility(store, lb.TARGETS, WORLD_POLICY, mode="pilot").snapshot(D5)
    return evidence, result, sixty_two


def test_real_default_eligibility_cannot_see_a_split_first_seen_after_s_minus_one(tmp_path):
    """codex-r1-1. 62's UniverseEligibility rebuilds lineage without the as-of trim, so a split first seen on S moved
    S-1's eligible set while the composed bars stayed identical. The default source here uses the same trimmed lineage."""
    _, base, base_62 = late_split_eligibility(tmp_path, {"no_future"}, "base")
    _, late, late_62 = late_split_eligibility(tmp_path, {"no_future", "relink"}, "late")
    assert base.status == late.status == signals.STATUS_EVALUATED
    assert base.composition_sha256 == late.composition_sha256 and base.adjusted_inputs_sha256 == late.adjusted_inputs_sha256
    assert base.eligible == base_62.eligible and base.eligibility_sha256 == base_62.result_sha256  # parity when no link is late
    assert late_62.eligible != base_62.eligible  # the precondition: the shock moves 62's own source
    assert late.eligible == base.eligible and len(base.eligible) >= 1
    assert late.eligibility_sha256 == base.eligibility_sha256
    assert late.decision_bytes() == base.decision_bytes() and late.evaluation_sha256 == base.evaluation_sha256


def test_real_default_eligibility_is_identical_under_every_shock_dated_s_or_later(tmp_path, monkeypatch):
    """codex-r1-1: the shock test with the default eligibility source, asserting eligibility identity as well."""
    reference = None
    for name, flags in {"base": set(), "scaled": {"scale"}, "future_deleted": {"no_future"},
                        "everything": {"scale", "new_breeze", "ca_structural", "bands_surv", "fetch_extra", "quarantine",
                                       "relink", "factor_event", "old_bonus"}}.items():
        root = real_world(tmp_path, flags, name=name)
        _, evidence, result = real_step(tmp_path, root, monkeypatch=monkeypatch, eligibility=None,
                                        universe_policy=WORLD_POLICY)
        assert result.status == signals.STATUS_EVALUATED, (name, result.pause_reason)
        view = (result.eligible, result.eligibility_sha256, result.decision_bytes(), result.evaluation_sha256,
                sorted(r["signal_id"] for r in kinds(evidence, "signal")))
        reference = reference or view
        assert view == reference, name
    assert reference[0]


# restart: the same session twice (codex-r1-2) ---------------------------------------------------------------------------


def reopen_evidence(evidence: EvidenceStore) -> EvidenceStore:
    return EvidenceStore.open(evidence.run_dir.parent, run_id="run1", session_id="s-1", clock=lambda: FIXED)


def test_a_restart_with_the_same_evaluation_dedupes_and_appends_nothing(tmp_path, stubs):
    inputs, evidence, first = run_step(tmp_path, run_start=S)
    before = evidence.records()
    reopened = reopen_evidence(evidence)
    again = evaluate_session(reopened, inputs, decision_session=S)
    assert again.status == signals.STATUS_EVALUATED and again.recorded == 0
    assert again.decision_bytes() == first.decision_bytes() and again.evaluation_sha256 == first.evaluation_sha256
    assert reopened.records() == before and not policy.run_state(reopened).open_pauses


def test_a_restart_that_derives_a_different_evaluation_pauses_instead_of_returning_either_value(tmp_path, stubs):
    inputs, evidence, first = run_step(tmp_path, run_start=S)
    before = evidence.records()
    stubs.rows = [row.model_copy(update={"adj_close": row.adj_close * Decimal("1.5")}) if row.trade_date == AS_OF else row
                  for row in stubs.rows]
    reopened = reopen_evidence(evidence)
    again = evaluate_session(reopened, inputs, decision_session=S)
    assert again.status == signals.STATUS_PAUSED and again.pause_event == "hash_mismatch"
    assert again.pause_reason == signals.CONFLICT_CODE and again.signals == () and again.evaluation_sha256 is None
    after = reopened.records()
    assert after[:len(before)] == before and [r["kind"] for r in after[len(before):]] == ["pause"]
    assert kinds(reopened, "pause")[0]["scope"] == "all" and len(kinds(reopened, "signal")) == 12
    assert policy.run_state(reopened).blocks(policy.STEP_BOOK)
    # still paused for the next call, which reads nothing more
    calls = len(stubs.live_calls)
    repeat = evaluate_session(reopened, inputs, decision_session=S)
    assert repeat.pause_reason == "already_paused" and len(stubs.live_calls) == calls


def test_real_a_restart_after_a_late_derived_dividend_pauses_and_the_log_keeps_the_first_decision(tmp_path, monkeypatch):
    monkeypatch.setitem(cc.STEADY, D4, ("99", "105", "95", "100"))
    root = real_world(tmp_path, {"no_future"}, name="restart_real", extra=fresh_extra)
    inputs, evidence, first = real_step(tmp_path, root, free=False)
    before = evidence.records()
    assert first.status == signals.STATUS_EVALUATED
    with PilotDataStore(root, workspace="india") as store:
        row = kit.bc_row("EQ", "STEADYCO", "STEADY CO", "INTERIM DIVIDEND", ex_date=D4)
        lb.ingest_pr_zip(store, cc.desc(kind="pr_zip"), lb.pr_zip_on(D2, [row]), trade_date=D2)
        lb.derive_corporate_actions(store, workspace="india")
        reopened = reopen_evidence(evidence)
        again = evaluate_session(reopened, replace(inputs, store=store), decision_session=WORLD_S)
    assert again.status == signals.STATUS_PAUSED and again.pause_reason == signals.CONFLICT_CODE
    assert reopened.records()[:len(before)] == before and len(reopened.records()) == len(before) + 1
    assert [p.scope for p in policy.run_state(reopened).open_pauses] == ["all"]


def test_a_signal_record_without_a_fingerprint_is_a_conflict_not_a_dedupe(tmp_path, stubs):
    inputs = make_inputs(tmp_path, eligibility=sup.StaticEligibility(ANCHORS), run_start=S)
    evidence = make_evidence(tmp_path, inputs)
    hashes = inputs.current_hashes()
    evidence.append("signal", {
        "signal_id": f"sig-{AS_OF.strftime('%Y%m%d')}-{ANCHORS[0]}", "symbol": "STK00", "isin": ANCHORS[0], "series": "EQ",
        "as_of_date": AS_OF.isoformat(), **{name: hashes[name] for name in HASH_NAMES}, "reason": signals.HOLDOUT_FLAG})
    result = evaluate_session(evidence, inputs, decision_session=S)
    assert result.status == signals.STATUS_PAUSED and result.pause_reason == signals.CONFLICT_CODE
    assert len(kinds(evidence, "signal")) == 1


def test_a_decide_stopped_between_two_records_finishes_under_the_same_fingerprint(tmp_path, stubs, monkeypatch):
    inputs = make_inputs(tmp_path, eligibility=sup.StaticEligibility(ANCHORS), run_start=S)
    evidence = make_evidence(tmp_path, inputs)
    original = signals._Recorder.append
    written = []

    def stop_after_five(self, kind, payload):
        if len(written) == 5:
            raise KeyboardInterrupt("stopped between records")
        written.append(kind)
        original(self, kind, payload)

    monkeypatch.setattr(signals._Recorder, "append", stop_after_five)
    with pytest.raises(KeyboardInterrupt):
        evaluate_session(evidence, inputs, decision_session=S)
    monkeypatch.setattr(signals._Recorder, "append", original)
    reopened = reopen_evidence(evidence)
    assert len(kinds(reopened, "signal")) == 5
    again = evaluate_session(reopened, inputs, decision_session=S)
    assert again.status == signals.STATUS_EVALUATED and again.recorded == 7 and len(kinds(reopened, "signal")) == 12
    assert {r["reason"].split(f"{signals.EVALUATION_KEY}=")[1][:64] for r in kinds(reopened, "signal")} == {again.evaluation_sha256}


# the fingerprint covers what the nine pins miss (codex-r1-3) ---------------------------------------------------------------


def test_real_a_derived_dividend_moves_the_fingerprint_though_every_pin_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setitem(cc.STEADY, D4, ("99", "105", "95", "100"))
    results = {}
    for label, with_event in (("base", False), ("dividend", True)):
        def extra(store, with_event=with_event):
            fresh_extra(store)
            if with_event:
                row = kit.bc_row("EQ", "STEADYCO", "STEADY CO", "INTERIM DIVIDEND", ex_date=D4)
                lb.ingest_pr_zip(store, cc.desc(kind="pr_zip"), lb.pr_zip_on(D2, [row]), trade_date=D2)
                lb.derive_corporate_actions(store, workspace="india")

        root = real_world(tmp_path, {"no_future"}, name=label, extra=extra)
        _, evidence, results[label] = real_step(tmp_path, root, free=False)
    base, dividend = results["base"], results["dividend"]
    assert base.status == dividend.status == signals.STATUS_EVALUATED
    assert base.hashes == dividend.hashes and base.raw_inputs_sha256 == dividend.raw_inputs_sha256
    assert base.decision_bytes() != dividend.decision_bytes() and base.composition_sha256 != dividend.composition_sha256
    assert base.evaluation_sha256 != dividend.evaluation_sha256


def test_real_a_different_band_file_moves_the_fingerprint_though_pins_and_composition_are_unchanged(tmp_path):
    results = {}
    for value in ("20", "5"):
        def extra(store, value=value):
            surveillance_lists()(store)
            lb.pb.put_list(store, D5, rows=[dict(kit.SEC_LIST_CANBK, Band=value),
                                            dict(kit.SEC_LIST_RELIANCE, Symbol="STEADYCO", Band=value)])
            lb.pb.with_convention(store)
            rrt.record_success(store, D5)

        root = real_world(tmp_path, {"no_future"}, name=f"band{value}", extra=extra)
        _, evidence, results[value] = real_step(tmp_path, root, free=False)
    wide, narrow = results["20"], results["5"]
    assert wide.status == narrow.status == signals.STATUS_EVALUATED
    assert {b.percent for b in wide.bands.values()} == {Decimal("20")} and {b.percent for b in narrow.bands.values()} == {Decimal("5")}
    assert wide.hashes == narrow.hashes and wide.composition_sha256 == narrow.composition_sha256
    assert wide.evaluation_sha256 != narrow.evaluation_sha256


def _band_override(stubs, monkeypatch, **update):
    band = stubs.band
    monkeypatch.setattr(signals, "band_for_upcoming_session",
                        lambda *a, **k: band(*a, **k).model_copy(update=update))
    return {}


def _convention(stubs, monkeypatch):
    stubs.convention = SimpleNamespace(model_dump=lambda mode="json": {"list_rule": "same_date", "basis": "other"})
    return {}


def _overlay_sources(stubs, monkeypatch):
    monkeypatch.setattr(signals, "snapshot_for", lambda store, kind, effective: SimpleNamespace(
        source_sha256=sha(f"other-{kind}"), list_name=kind))
    return {}


def _rows(stubs, monkeypatch, column):
    stubs.rows = [r.model_copy(update={column: getattr(r, column) + 1}) if r.trade_date == AS_OF else r
                  for r in stubs.rows]
    return {}


def _set(stubs, monkeypatch, **fields):
    for name, value in fields.items():
        setattr(stubs, name, value)
    return {}


MUTATIONS = {
    # each moves exactly one component of the fingerprint (the decision hash too where the input feeds a score)
    "band_percent": lambda stubs, mp: _band_override(stubs, mp, percent=Decimal("5")),
    "band_file": lambda stubs, mp: _band_override(stubs, mp, source_sha256s=(sha("corrected-band"),)),
    "band_convention": _convention,
    "overlay_list": lambda stubs, mp: _set(stubs, mp, lists={"asm": [(None, "STK07")], "gsm": []}),
    "overlay_foreign_symbol": lambda stubs, mp: _set(stubs, mp, lists={"asm": [(None, "NOTHELD")], "gsm": []}),
    "overlay_source": _overlay_sources,
    "adjusted_price": lambda stubs, mp: _rows(stubs, mp, "adj_close"),
    "raw_price": lambda stubs, mp: _rows(stubs, mp, "raw_close"),
    "composition": lambda stubs, mp: _set(stubs, mp, composition="d" * 64),
    "lineage": lambda stubs, mp: _set(stubs, mp, lineages={ANCHORS[0]: sha("lineage")}),
    "factor_set": lambda stubs, mp: _set(stubs, mp, factor_sets={ANCHORS[0]: sha("factors")}),
    "unknown_dividend": lambda stubs, mp: _set(stubs, mp, unknown={ANCHORS[0]: (DividendAmountUnknownEvent(
        event_id="e" * 64, ex_date=HIST[-3]),)}),
    "sources": lambda stubs, mp: _set(stubs, mp, sources=(sha("bhavcopy"),)),
    "smallcap_class": lambda stubs, mp: {"eligibility": sup.StaticEligibility(ANCHORS, smallcap={ANCHORS[0]: "small"})},
    "pins": lambda stubs, mp: {"cost_sha256": sha("another-cost-table")},
    "params_without_a_pin_change": lambda stubs, mp: {"config": strategy(lookback_sessions=10)},
    # same inputs, same ranks, other scores: only the decision itself moves (a scoring code change)
    "scoring_code": lambda stubs, mp: mp.setattr(signals, "zscores", lambda raw: {
        anchor: value * 2 for anchor, value in zscores(raw).items()}) or {},
}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_each_decision_input_moves_the_evaluation_fingerprint(mutation, tmp_path, stubs, monkeypatch):
    _, _, base = run_step(tmp_path, run_start=S)
    kwargs = MUTATIONS[mutation](stubs, monkeypatch)
    _, _, changed = run_step(tmp_path, run_start=S, **kwargs)
    assert changed.status == base.status == signals.STATUS_EVALUATED
    assert changed.evaluation_sha256 != base.evaluation_sha256
    if mutation not in ("pins", "params_without_a_pin_change"):
        assert changed.hashes == base.hashes  # the nine pins alone would not have seen it


def test_the_evaluation_fingerprint_is_deterministic(tmp_path, stubs):
    _, _, one = run_step(tmp_path, run_start=S)
    _, _, two = run_step(tmp_path, run_start=S)
    assert one.evaluation_sha256 == two.evaluation_sha256 and len(one.evaluation_sha256) == 64


def test_a_corrected_band_file_on_restart_is_a_conflict_not_a_silent_dedupe(tmp_path, stubs, monkeypatch):
    inputs, evidence, first = run_step(tmp_path, run_start=S)
    band = stubs.band
    monkeypatch.setattr(signals, "band_for_upcoming_session", lambda *a, **k: band(*a, **k).model_copy(
        update={"percent": Decimal("5"), "source_sha256s": (sha("corrected-band"),)}))
    again = evaluate_session(reopen_evidence(evidence), inputs, decision_session=S)
    assert again.status == signals.STATUS_PAUSED and again.pause_reason == signals.CONFLICT_CODE


# freshness after a weekday holiday (codex-r1-4) ----------------------------------------------------------------------------


def holiday_run(tmp_path, monkeypatch, stubs, *, calendar_holidays):
    decision = date(2026, 10, 20)  # Tuesday; Monday 19 October is a holiday on the operator's calendar
    monkeypatch.setattr(signals, "require_reference_freshness", refresh.require_reference_freshness)
    with PilotDataStore(tmp_path / "store", workspace="india") as store:
        rrt.mark_calendar(store, AS_OF, date(2026, 10, 19), {AS_OF})
        rrt.snapshots(store, AS_OF)
        rrt.record_success(store, AS_OF)
        inputs = make_inputs(tmp_path, store=store, eligibility=sup.StaticEligibility(ANCHORS), run_start=decision)
        inputs = replace(inputs, calendar=make_calendar(tmp_path, holidays=calendar_holidays))
        evidence = make_evidence(tmp_path, inputs)
        return evaluate_session(evidence, inputs, decision_session=decision)


def test_a_pinned_monday_holiday_does_not_make_fridays_refresh_stale_for_tuesday(tmp_path, stubs, monkeypatch):
    result = holiday_run(tmp_path, monkeypatch, stubs, calendar_holidays=[date(2026, 10, 19)])
    assert result.status == signals.STATUS_EVALUATED and result.as_of == AS_OF


def test_without_the_holiday_on_the_calendar_the_same_store_is_still_stale(tmp_path, stubs, monkeypatch):
    result = holiday_run(tmp_path, monkeypatch, stubs, calendar_holidays=[])
    assert result.status == signals.STATUS_PAUSED and result.pause_event == "freshness_check_failed"
    assert result.pause_reason == "reference_refresh_stale"


def test_the_pinned_calendar_is_what_freshness_is_given(tmp_path, stubs):
    inputs, _, _ = run_step(tmp_path, run_start=S)
    assert stubs.fresh_calendars == [inputs.calendar]


# the manifest flag (codex-r1-5, opus-r1-1) ----------------------------------------------------------------------------------


def test_the_flag_is_in_manifest_json_for_a_reader_that_never_opens_the_log(tmp_path, stubs):
    _, evidence, result = run_step(tmp_path, run_start=S)
    manifest = json.loads((evidence.run_dir / "manifest.json").read_text())
    assert manifest["holdout_dated_inputs"] is True and result.holdout_dated_inputs is True
    assert evidence.manifest.holdout_dated_inputs is True


def test_a_run_whose_manifest_lacks_the_flag_is_refused_before_anything_is_read(tmp_path, stubs):
    inputs = make_inputs(tmp_path, eligibility=sup.StaticEligibility(ANCHORS), run_start=S)
    evidence = EvidenceStore.create(tmp_path / "ev-unflagged", run_id="run1", run_class="plumbing", session_id="s-1",
                                    hashes=inputs.current_hashes(), clock=lambda: FIXED)  # the default is False
    assert json.loads((evidence.run_dir / "manifest.json").read_text())["holdout_dated_inputs"] is False
    with pytest.raises(SignalsError) as refused:
        evaluate_session(evidence, inputs, decision_session=S)
    assert refused.value.code == "manifest_not_holdout_dated"
    assert stubs.live_calls == [] and stubs.fresh_calls == [] and evidence.records() == []


# the seed pin (opus-r1-1) ----------------------------------------------------------------------------------------------------


def test_reseeding_with_other_rows_moves_only_the_seed_pin(tmp_path, stubs, monkeypatch):
    inputs = make_inputs(tmp_path, eligibility=sup.StaticEligibility(ANCHORS), run_start=S)
    evidence = make_evidence(tmp_path, inputs)
    monkeypatch.setattr(signals, "seed_rows", lambda store: [["a" * 64, HIST[0].isoformat(), AS_OF.isoformat()],
                                                            ["b" * 64, HIST[2].isoformat(), AS_OF.isoformat()]])
    assert inputs.current_hashes()["seed_sha256"] != evidence.manifest.hashes["seed_sha256"]
    result = evaluate_session(evidence, inputs, decision_session=S)
    assert result.status == signals.STATUS_PAUSED and result.pause_reason == "hash_mismatch:seed_sha256"
    assert stubs.live_calls == [] and kinds(evidence, "signal") == []


def append_seed_row(store, *, report_sha: str, window_start: date, window_end: date, dataset_sha: str = "c" * 64) -> None:
    store.ensure_table("session_store_seeds", SEEDS_DDL, key_columns=("seed_report_sha256",))
    store.append_rows("session_store_seeds", [{
        "seed_report_sha256": report_sha, "research_identity_sha256": "b" * 64, "dataset_sha256": dataset_sha,
        "window_start": window_start, "window_end": window_end, "seeded_at_utc": cc.LATER.replace(tzinfo=None),
        "row_sha256": sha("row-" + report_sha), "source_sha256": report_sha}], check="session_store_seed")


def test_real_a_reseed_with_other_blobs_and_window_but_the_same_dataset_pauses_on_the_seed_pin(tmp_path, monkeypatch):
    root = real_world(tmp_path, {"no_future"}, name="reseed", extra=fresh_extra)
    calendar = nc.pinned(_new_dir(tmp_path, "cal-reseed"), coverage=(date(2025, 3, 1), date(2025, 3, 31)))
    with PilotDataStore(root, workspace="india") as store:
        inputs = SignalInputs(
            store=store, strategy=world_params(), targets=lb.TARGETS, universe_policy=UniversePolicy(),
            calendar=calendar, tolerances=lb.TOLERANCES, window_start=cc.START, run_start_session=D2,
            dataset_sha256=sha("dataset"), tick_sha256=sha("tick"), cost_sha256=sha("cost"),
            scenario_sha256=sha("scenario"), eligibility=sup.StaticEligibility(REAL_ELIGIBLE))
        pins = inputs.current_hashes()
        evidence = EvidenceStore.create(_new_dir(tmp_path, "ev-reseed"), run_id="run1", run_class="plumbing",
                                        session_id="s-1", hashes=pins, holdout_dated_inputs=True, clock=lambda: FIXED)
        monkeypatch.setattr(signals, "require_reference_freshness", lambda *a, **k: None)
        assert evaluate_session(evidence, inputs, decision_session=WORLD_S).status == signals.STATUS_EVALUATED
        append_seed_row(store, report_sha="e" * 64, window_start=D2, window_end=D5)  # same dataset sha, other blobs and window
        changed = inputs.current_hashes()
        assert {name for name in pins if pins[name] != changed[name]} == {"seed_sha256"}
        result = evaluate_session(evidence, inputs, decision_session=WORLD_S)
    assert result.status == signals.STATUS_PAUSED and result.pause_event == "hash_mismatch"
    assert result.pause_reason == "hash_mismatch:seed_sha256"


def test_a_store_that_was_never_seeded_pauses_with_a_typed_code_and_reads_nothing(tmp_path, stubs, monkeypatch):
    inputs = make_inputs(tmp_path, eligibility=sup.StaticEligibility(ANCHORS), run_start=S)
    evidence = make_evidence(tmp_path, inputs)
    monkeypatch.setattr(signals, "seed_rows", REAL_SEED_ROWS)  # the real seed read, on a real and empty store
    with PilotDataStore(tmp_path / "empty-store", workspace="india") as store:
        with pytest.raises(PilotDataError) as unseeded:
            signals.seed_rows(store)
        assert unseeded.value.code == "not_a_session_store"
        result = evaluate_session(evidence, replace(inputs, store=store), decision_session=S)
    assert result.status == signals.STATUS_PAUSED and result.pause_event == "stale_data"
    assert result.pause_reason == "not_a_session_store" and kinds(evidence, "signal") == []


# a band error is typed (opus-r1-2) -------------------------------------------------------------------------------------------------


def test_a_pilot_error_from_the_band_call_pauses_all_with_its_code_and_records_no_signal(tmp_path, stubs):
    stubs.band_error = PilotDataError("band_convention_conflict", f"{CANARY_TOKEN} {CANARY_IP}")
    _, evidence, result = run_step(tmp_path, run_start=S, forbidden=FORBIDDEN)
    assert result.status == signals.STATUS_PAUSED and result.pause_event == "stale_data"
    assert result.pause_reason == "band_convention_conflict" and result.signals == ()
    assert kinds(evidence, "signal") == [] and kinds(evidence, "pause")[0]["scope"] == "all"
    raw = log_bytes(evidence)
    assert not any(canary.encode() in raw for canary in FORBIDDEN)


def test_a_pilot_error_from_the_band_convention_read_pauses_too(tmp_path, stubs, monkeypatch):
    def broken(store):
        raise PilotDataError("band_convention_conflict", "two conventions")

    monkeypatch.setattr(signals, "current_band_convention", broken)
    _, evidence, result = run_step(tmp_path, run_start=S)
    assert result.status == signals.STATUS_PAUSED and result.pause_reason == "band_convention_conflict"
    assert kinds(evidence, "signal") == []


# classification bounded to S-1 (codex-r2-1) -------------------------------------------------------------------------------------


def _real_inputs(tmp_path: Path, store, *, targets=None) -> SignalInputs:
    calendar = nc.pinned(_new_dir(tmp_path, "cal-r2"), coverage=(date(2025, 3, 1), date(2025, 3, 31)))
    return SignalInputs(
        store=store, strategy=world_params(), targets=targets or lb.TARGETS, universe_policy=WORLD_POLICY,
        calendar=calendar, tolerances=lb.TOLERANCES, window_start=cc.START, run_start_session=D2,
        dataset_sha256=sha("dataset"), tick_sha256=sha("tick"), cost_sha256=sha("cost"), scenario_sha256=sha("scenario"))


def _etf_targets():
    members = tuple(m.model_copy(update={"kind": "liquid_etf"}) if m.anchor_isin == cc.STEADY_ISIN else m
                    for m in lb.TARGETS.members)
    return lb.TARGETS.model_copy(update={"members": members})


def _ingest_etf_info(store, day: date, underlying: str) -> None:
    row = kit.etf_row("STEADYCO", "SYNTHETIC ETF", underlying)
    lb.ingest_pr_zip(store, cc.desc(kind="pr_zip"), kit.pr_zip(day, [kit.pd_index_row()], [], [row]), trade_date=day)


def _add_index_lists(store, fetched_at: datetime, small) -> None:
    from pilot_data.constituents import ingest_index_list
    from pilot_data.core import SourceDescriptor
    import test_pilot_data_universe as ut

    def rows(wanted, total, base):
        ids = list(wanted) + [ut.make_isin(base + n) for n in range(total - len(wanted))]
        return [{"Company Name": f"C{i}", "Industry": "I", "Symbol": f"X{i}", "Series": "EQ", "ISIN Code": isin}
                for i, isin in enumerate(ids)]

    for kind, total, wanted, base in (("nifty500", 500, REAL_ELIGIBLE, 50000), ("smallcap250", 250, small, 60000)):
        descriptor = SourceDescriptor(source="nse_archive", kind="index_list_" + kind,
                                      locator="https://example.invalid/" + kind, fetched_at=fetched_at)
        ingest_index_list(store, descriptor, kit.index_list_csv(rows(wanted, total, base)), list_name=kind)


@pytest.mark.parametrize("earlier_info", [False, True], ids=["new_etf_info_after_s_minus_one", "corrected_etf_info"])
def test_real_etf_metadata_filed_after_s_minus_one_cannot_rewrite_the_earlier_evaluation(tmp_path, earlier_info):
    """codex-r2-1. S-1 was evaluated on S-1's inputs; the next day's ingest adds (or changes) the ETF's underlying. The
    classification, the eligibility hash and the fingerprint of S-1 stay put and a retry does not conflict."""
    root = real_world(tmp_path, {"no_future"}, extra=fresh_extra)
    with PilotDataStore(root, workspace="india") as store:
        targets = _etf_targets()
        if earlier_info:
            _ingest_etf_info(store, D5, "NIFTY 50")
        inputs = _real_inputs(tmp_path, store, targets=targets)
        evidence = make_evidence(tmp_path, inputs)
        source = signals.AsOfEligibility(store, targets, WORLD_POLICY)
        before = source.snapshot(D5)
        first = evaluate_session(evidence, inputs, decision_session=WORLD_S)
        assert first.status == signals.STATUS_EVALUATED, first.pause_reason
        _ingest_etf_info(store, WORLD_S, "NIFTY SMALLCAP 250")  # the PR file of the decision day, ingested later
        after = source.snapshot(D5)
        again = evaluate_session(reopen_evidence(evidence), inputs, decision_session=WORLD_S)
        from pilot_data.universe import classify_smallcap
        assert classify_smallcap(store, targets)[cc.STEADY_ISIN] == "small"  # the unbounded read does see it
    assert after.smallcap == before.smallcap and after.result_sha256 == before.result_sha256
    assert before.smallcap[cc.STEADY_ISIN] == ("not_small" if earlier_info else "unclassified")
    assert again.status == signals.STATUS_EVALUATED and again.recorded == 0, again.pause_reason
    assert again.evaluation_sha256 == first.evaluation_sha256 and again.eligibility_sha256 == first.eligibility_sha256


def test_real_an_index_list_fetched_after_s_minus_one_cannot_rewrite_the_earlier_evaluation(tmp_path):
    root = real_world(tmp_path, {"no_future"}, extra=fresh_extra)
    with PilotDataStore(root, workspace="india") as store:
        _add_index_lists(store, datetime(2025, 3, 7, 8, tzinfo=timezone.utc), [])
        inputs = _real_inputs(tmp_path, store)
        evidence = make_evidence(tmp_path, inputs)
        source = signals.AsOfEligibility(store, inputs.targets, WORLD_POLICY)
        before = source.snapshot(D5)
        first = evaluate_session(evidence, inputs, decision_session=WORLD_S)
        assert first.status == signals.STATUS_EVALUATED, first.pause_reason
        _add_index_lists(store, datetime(2025, 3, 10, 8, tzinfo=timezone.utc), [cc.STEADY_ISIN])  # the STEADY name turns small
        after = source.snapshot(D5)
        again = evaluate_session(reopen_evidence(evidence), inputs, decision_session=WORLD_S)
    assert before.smallcap[cc.STEADY_ISIN] == "not_small"
    assert after.smallcap == before.smallcap and after.result_sha256 == before.result_sha256
    assert again.status == signals.STATUS_EVALUATED and again.recorded == 0, again.pause_reason
    assert again.evaluation_sha256 == first.evaluation_sha256


def test_real_a_list_fetched_on_s_minus_one_is_read_and_one_fetched_the_day_after_is_not(tmp_path):
    root = real_world(tmp_path, {"no_future"}, extra=fresh_extra)
    with PilotDataStore(root, workspace="india") as store:
        source = signals.AsOfEligibility(store, lb.TARGETS, WORLD_POLICY)
        assert source.snapshot(D5).smallcap[cc.STEADY_ISIN] == "unclassified"  # no list yet
        _add_index_lists(store, datetime(2025, 3, 7, 23, 59, tzinfo=timezone.utc), [])  # the last minute of S-1 (UTC)
        assert source.snapshot(D5).smallcap[cc.STEADY_ISIN] == "not_small"
        _add_index_lists(store, datetime(2025, 3, 8, 0, 0, tzinfo=timezone.utc), [cc.STEADY_ISIN])
        assert source.snapshot(D5).smallcap[cc.STEADY_ISIN] == "not_small"


def test_real_the_unbounded_default_callers_are_unchanged(tmp_path):
    """codex-r2-1 parity: no ``classification_as_of`` keeps 59's and 62's exact behaviour, and with nothing filed after
    S-1 the bounded source gives the same hash as the unbounded one."""
    from pilot_data.universe import NoTradingStatusSource, classify_smallcap, evaluate_universe
    root = real_world(tmp_path, {"no_future"}, extra=fresh_extra)
    with PilotDataStore(root, workspace="india") as store:
        _add_index_lists(store, datetime(2025, 3, 7, 8, tzinfo=timezone.utc), [])
        kwargs = dict(as_of=D5, targets=lb.TARGETS, policy=WORLD_POLICY, mode="pilot", allow_missing_surveillance_before=None,
                      workspace="india", status_source=NoTradingStatusSource())
        default = evaluate_universe(store, **kwargs)
        explicit = evaluate_universe(store, **kwargs, classification_as_of=None)
        bounded = evaluate_universe(store, **kwargs, classification_as_of=D5)
        sixty_two = UniverseEligibility(store, lb.TARGETS, WORLD_POLICY, mode="pilot").snapshot(D5)
        adapter = signals.AsOfEligibility(store, lb.TARGETS, WORLD_POLICY).snapshot(D5)
        assert default.result_sha256 == explicit.result_sha256 == bounded.result_sha256 == sixty_two.result_sha256
        assert adapter.result_sha256 == default.result_sha256
        _add_index_lists(store, datetime(2025, 3, 10, 8, tzinfo=timezone.utc), [cc.STEADY_ISIN])
        assert classify_smallcap(store, lb.TARGETS)[cc.STEADY_ISIN] == "small"  # default still reads the latest
        assert classify_smallcap(store, lb.TARGETS, as_of=D5)[cc.STEADY_ISIN] == "not_small"
        assert evaluate_universe(store, **kwargs).result_sha256 != default.result_sha256  # unbounded callers move, as before


# repeat calls on an unseeded store (opus-r2 residual 1) and band scale (codex-r2) ----------------------------------------------------


def test_repeat_calls_on_an_unseeded_store_record_one_pause_not_one_per_call(tmp_path, stubs, monkeypatch):
    inputs = make_inputs(tmp_path, eligibility=sup.StaticEligibility(ANCHORS), run_start=S)
    evidence = make_evidence(tmp_path, inputs)
    monkeypatch.setattr(signals, "seed_rows", REAL_SEED_ROWS)
    with PilotDataStore(tmp_path / "empty-store-2", workspace="india") as store:
        unseeded = replace(inputs, store=store)
        first = evaluate_session(evidence, unseeded, decision_session=S)
        second = evaluate_session(evidence, unseeded, decision_session=S)
        third = evaluate_session(reopen_evidence(evidence), unseeded, decision_session=S)
    assert first.pause_reason == "not_a_session_store" and first.recorded == 1
    assert second.status == third.status == signals.STATUS_PAUSED and second.pause_reason == "already_paused"
    assert second.recorded == third.recorded == 0
    assert len(kinds(evidence, "pause")) == 1 and kinds(evidence, "signal") == []

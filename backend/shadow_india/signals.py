"""Signal inputs for one shadow decide (64-03 Task 2B, D-17, D-22, D-31, D-32, Q-2, Q-7).

A decide for session S is the 62 decision dated S-1 (62 builds ``DecisionContext(session, ...)`` at the close of
``session`` and fills on the next one). Everything here reads data dated S-1 or earlier. The lists that matter for S
are the ASM and GSM snapshots effective S-1 (Q-7: NSE's publication time for S's lists is unverified), and the band
for S, which 64-08's ``band_for_upcoming_session`` resolves from S-1's own bhavcopy.

Inputs, all through public APIs and no edit to a 59, 60, 62 or 63 module:

- strategy parameters and limits: the fail-closed private loader (``load_strategy_config``). The D-17 plumbing
  parameter set lives in ``private/india/strategy.json``; the tracked placeholder is never a default;
- bars: ``pilot_data.live_bars.bars_as_of`` on the session store, with the real decision session. Adjusted prices
  feed signals, raw prices are for marks (62 D-05);
- freshness: ``reference_refresh.require_reference_freshness`` for S, with the pinned calendar, so the refresh it asks
  for is the previous trading session's and a weekday holiday does not make the session before it stale;
- eligibility: 59's ``evaluate_universe`` in ``mode="pilot"`` at S-1 (62 parity, Q-2) on the same as-of trimmed lineage
  ``bars_as_of`` composes with, so an ISIN link first seen after S-1 cannot change who is eligible, plus a buy-block
  overlay from the ASM and GSM lists effective S-1;
- signals: 62's own ``SignalTable`` and ``MomentumProvider`` on a ``DatasetView`` sealed with ``HoldoutRange(S, S)``
  (D-32), so a read of S or later raises ``HoldoutViolation`` whatever the composed rows hold.

Outcomes. A cadence day that is not an evaluation day writes ``non_evaluation_day`` and nothing else (marks, stops
and exits are the runner's job). A hash that differs from the run manifest, a failing freshness check, a bar or
eligibility input that cannot be composed, or an empty eligible universe records the pause the policy table assigns
and returns ``status="paused"`` without a score. A quarantined bar, a missing S-1 bar or too little history
suppresses that one name's signal and records why; no pause is raised for it, because a name with no signal cannot
be entered (an ``unresolved_corporate_action`` pause is the held-name path in ``portfolio.py``).

D-31: every result carries ``holdout_dated_inputs=True``. The composed rows may be dated inside the sealed 62
holdout, so this module never imports the 62 engine, gate, report or holdout opener. A run whose manifest does not
carry ``holdout_dated_inputs: true`` (hash-bound in ``manifest.json``) is refused before any input is read, and the
flag is also stamped in every record's text.

Pins. The nine record pins plus ``seed_sha256``, which binds the 64-08 seed rows of the session store (their report
sha256 and window) and this run's ``window_start``. Re-seeding the store with other blobs or another window moves it
even when ``dataset_sha256`` is unchanged. Every signal record carries ``evaluation_sha256``, a fingerprint of the
whole evaluation (pins, composition and factor sets, eligibility, overlay, bands and band convention, the decision).
A restart that derives a different evaluation for a session already on the record pauses everything with
``evaluation_conflict`` instead of returning one value while the log keeps the other.

No clock, no network, no randomness. Evidence writes go through the store handed in.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from pilot_data.core import PilotDataError, canonical_sha256
from pilot_data.live_bars import LiveBarsResult, _as_of_lineage, bars_as_of
from pilot_data.models import CrossCheckTolerances
from pilot_data.nse_calendar import PinnedCalendar
from pilot_data.price_bands import BandObservation, band_for_upcoming_session, current_band_convention
from pilot_data.reference_refresh import require_reference_freshness
from pilot_data.store import PilotDataStore
from pilot_data.surveillance import snapshot_for, surveillance_entries
from pilot_data.targets import TargetUniverseResult
from pilot_data.universe import UniversePolicy, evaluate_universe
from private_config.errors import PrivateConfigError
from private_config.loader import load_workspace_config
from private_config.schemas import IndiaLimits
from strategy_india.data import (
    DatasetView, DecisionView, EligibilitySnapshot, EligibilitySource, NoTradingStatusSource, events_from_manifest,
)
from strategy_india.errors import ParamsError
from strategy_india.holdout import HoldoutRange
from strategy_india.params import StrategyParams, parse_params
from strategy_india.signals import MODE_BASE, DecisionContext, MomentumProvider, SignalTable, zscores

from . import policy
from .evidence import EvidenceStore

HASH_NAMES = (
    "strategy_sha256", "params_sha256", "dataset_sha256", "universe_sha256", "band_sha256", "tick_sha256",
    "limits_sha256", "cost_sha256", "scenario_sha256",
)
SEED_PIN = "seed_sha256"  # manifest-only: the signal record schema carries the nine HASH_NAMES
PIN_NAMES = (*HASH_NAMES, SEED_PIN)
BAND_CONTRACT = "band_for_upcoming_session/1"
SEED_CONTRACT = "session-store-seeds/1"
EVALUATION_CONTRACT = "shadow-evaluation/1"
SIGNAL_CONTRACT = "shadow-signals/1"
STATUS_EVALUATED = "evaluated"
STATUS_NON_EVALUATION_DAY = "non_evaluation_day"
STATUS_PAUSED = "paused"
HOLDOUT_FLAG = "holdout_dated_inputs=true"
EVALUATION_KEY = "evaluation_sha256"
_EVALUATION_RE = re.compile(rf"(?:^|;){EVALUATION_KEY}=([0-9a-f]{{64}})(?:;|$)")
CONFLICT_CODE = "evaluation_conflict"
SUPPRESS_QUARANTINED = "adjusted_quarantined"
SUPPRESS_MISSING_BAR = "missing_bar_s1"
SUPPRESS_HISTORY = "insufficient_history_or_gap"
SURVEILLANCE_LISTS = ("asm", "gsm")


class SignalsError(Exception):
    """A refusal by this module. ``code`` is stable and carries no value."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- strategy config (fail-closed private loader) ---------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class StrategyConfig:
    params: StrategyParams
    params_sha256: str
    params_version: str
    limits: IndiaLimits
    limits_sha256: str

    def __repr__(self) -> str:  # no value of a private file reaches a log through a repr
        return f"StrategyConfig(version={self.params_version!r}, params_sha256={self.params_sha256!r})"

    def strategy_sha256(self) -> str:
        return canonical_sha256({
            "contract": SIGNAL_CONTRACT, "provider": "MomentumProvider", "mode": MODE_BASE,
            "params_version": self.params_version, "params_sha256": self.params_sha256,
        })


def load_strategy_config(private_dir: str | Path | None) -> StrategyConfig:
    """Parameters and limits through the fail-closed loader. Any refusal is a stable code, never a value.

    ``include_execution=False``: the signal step does not need the execution file and must not be affected by it.
    """

    try:
        config = load_workspace_config(private_dir, "india", include_execution=False)
    except PrivateConfigError as error:
        raise SignalsError(f"strategy_config_refused:{error.code}") from None
    if config.strategy is None or config.limits is None:
        raise SignalsError("strategy_config_incomplete")
    try:
        params = parse_params(config.strategy.params)
    except ParamsError:
        raise SignalsError("strategy_params_invalid") from None
    limits_sha = canonical_sha256(config.limits.model_dump(mode="json"))
    return StrategyConfig(
        params=params, params_sha256=config.strategy.params_sha256,
        params_version=config.strategy.strategy_params_version, limits=config.limits, limits_sha256=limits_sha,
    )


# --- eligibility on the as-of lineage, and the seed pin ---------------------------------------------------------


class AsOfEligibility:
    """59's ``evaluate_universe`` on the lineage ``bars_as_of`` uses (``UniverseEligibility`` with the as-of trim).

    62's ``UniverseEligibility`` rebuilds each lineage with ``build_lineage``, which links an ISIN change to a split
    event without asking when the event was first seen. Here every lineage is cut with ``_as_of_lineage`` first, so a
    link first seen after ``as_of`` is invisible to eligibility exactly as it is to the bars. The small-cap
    classification is bounded the same way (``classification_as_of``): index lists fetched and ETF info filed after
    ``as_of`` do not exist yet. Same snapshot type, same ``result_sha256`` as ``UniverseEligibility`` whenever no such
    link or later metadata exists.
    """

    def __init__(self, store: PilotDataStore, targets: TargetUniverseResult, policy: UniversePolicy, *,
                 mode: str = "pilot") -> None:
        self._store = store
        self._targets = targets
        self.policy = policy
        self._mode = mode

    def snapshot(self, as_of: date) -> EligibilitySnapshot:
        result = evaluate_universe(
            self._store, as_of=as_of, targets=self._targets, policy=self.policy, mode=self._mode,  # type: ignore[arg-type]
            allow_missing_surveillance_before=None, workspace="india", status_source=NoTradingStatusSource(),
            lineage_override=lambda member, built: _as_of_lineage(self._store, built, as_of),
            classification_as_of=as_of)
        if result.as_of != as_of:
            raise SignalsError("eligibility_not_for_as_of")
        return EligibilitySnapshot(
            as_of=result.as_of, eligible=frozenset(result.eligible_isins),
            smallcap={decision.anchor_isin: decision.smallcap_class for decision in result.decisions},
            result_sha256=result.result_sha256)


def seed_rows(store: PilotDataStore) -> list[list[str]]:
    """Every 64-08 seed row of the session store (report sha256, window), sorted. A store with none is not a session store."""

    if not store.table_exists("session_store_seeds"):
        raise PilotDataError("not_a_session_store", "the store has no session_store_seeds table")
    rows = store.query("SELECT seed_report_sha256, window_start, window_end FROM session_store_seeds "
                       "ORDER BY seed_report_sha256, window_start, window_end")
    if not rows:
        raise PilotDataError("not_a_session_store", "the store has no session_store_seeds row")
    return [[str(row[0]), row[1].isoformat(), row[2].isoformat()] for row in rows]


def seed_pin(store: PilotDataStore, window_start: date) -> str:
    return canonical_sha256({"contract": SEED_CONTRACT, "seeds": seed_rows(store),
                             "window_start": window_start.isoformat()})


# --- inputs and results -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalInputs:
    """What the runner hands in. ``store`` is the writable session store, never the research store."""

    store: PilotDataStore
    strategy: StrategyConfig
    targets: TargetUniverseResult
    universe_policy: UniversePolicy
    calendar: PinnedCalendar
    tolerances: CrossCheckTolerances
    window_start: date
    run_start_session: date
    dataset_sha256: str  # the pinned frozen dataset the session store was seeded from
    tick_sha256: str
    cost_sha256: str
    scenario_sha256: str
    eligibility: EligibilitySource | None = None  # default: UniverseEligibility in pilot mode

    def current_hashes(self) -> dict[str, str]:
        """The pins a manifest carries (the nine record pins and ``seed_sha256``), recomputed from the live objects.

        Raises ``PilotDataError`` when the store is not a seeded session store.
        """

        return {
            SEED_PIN: seed_pin(self.store, self.window_start),
            "strategy_sha256": self.strategy.strategy_sha256(),
            "params_sha256": self.strategy.params_sha256,
            "dataset_sha256": self.dataset_sha256,
            "universe_sha256": canonical_sha256({
                "targets": self.targets.target_sha256, "policy": self.universe_policy.policy_sha256(),
                "mode": "pilot",
            }),
            "band_sha256": canonical_sha256({"contract": BAND_CONTRACT, "calendar_sha256": self.calendar.sha256}),
            "tick_sha256": self.tick_sha256,
            "limits_sha256": self.strategy.limits_sha256,
            "cost_sha256": self.cost_sha256,
            "scenario_sha256": self.scenario_sha256,
        }


@dataclass(frozen=True)
class NameSignal:
    anchor_isin: str
    isin: str
    symbol: str
    series: str
    raw_score: Decimal | None
    zscore: Decimal | None
    rank: int | None
    suppressed: str | None
    buy_blocked: bool


@dataclass(frozen=True)
class SignalResult:
    status: str
    session: date
    as_of: date | None
    holdout_dated_inputs: bool = True  # D-31
    cadence_index: int | None = None
    evaluation_due: bool = False
    hashes: Mapping[str, str] = field(default_factory=dict)
    pause_event: str | None = None
    pause_reason: str | None = None
    signals: tuple[NameSignal, ...] = ()
    eligible: frozenset[str] = frozenset()
    eligibility_sha256: str | None = None
    surveillance_as_of: date | None = None  # Q-7: the lists the overlay read
    overlay_blocked: frozenset[str] = frozenset()
    overlay_sha256: str | None = None
    adjusted_inputs_sha256: str | None = None
    raw_inputs_sha256: str | None = None
    composition_sha256: str | None = None
    bands: Mapping[str, BandObservation] = field(default_factory=dict)
    entries_possible: bool = False  # the cross-section is large enough to z-score (62: min_universe_for_entry)
    recorded: int = 0  # evidence records this call appended
    evaluation_sha256: str | None = None  # the fingerprint every signal record of this evaluation carries

    def decision_payload(self) -> dict[str, Any]:
        """Everything the decision for S consists of, as strings, for the byte-identity tests."""

        return {
            "status": self.status, "session": self.session.isoformat(),
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "eligible": sorted(self.eligible), "eligibility_sha256": self.eligibility_sha256,
            "overlay_blocked": sorted(self.overlay_blocked), "entries_possible": self.entries_possible,
            "signals": [
                [s.anchor_isin, None if s.raw_score is None else str(s.raw_score),
                 None if s.zscore is None else str(s.zscore), s.rank, s.suppressed, s.buy_blocked]
                for s in self.signals
            ],
        }

    def decision_sha256(self) -> str:
        return canonical_sha256(self.decision_payload())

    def decision_bytes(self) -> bytes:
        return json.dumps(self.decision_payload(), sort_keys=True, separators=(",", ":")).encode("ascii")


# --- calendar and cadence ---------------------------------------------------------------------------------------


def _is_session(calendar: PinnedCalendar, day: date) -> bool:
    if calendar.is_special(day):
        return True
    return not (calendar.is_weekend(day) or calendar.is_holiday(day))


def _require_covered(calendar: PinnedCalendar, *days: date) -> None:
    if not all(calendar.covers(day) for day in days):
        raise SignalsError("session_calendar_unknown")


def previous_session(calendar: PinnedCalendar, session: date) -> date:
    """The last session before ``session`` on the pinned calendar. Raises when the table does not cover the walk."""

    _require_covered(calendar, session)
    day = session - timedelta(days=1)
    while True:
        _require_covered(calendar, day)
        if _is_session(calendar, day):
            return day
        day -= timedelta(days=1)


def sessions_before(calendar: PinnedCalendar, start: date, session: date) -> int:
    """Sessions in ``[start, session)`` on the pinned calendar: the 0-based index of ``session`` in the run."""

    if start > session:
        raise SignalsError("run_start_after_session")
    _require_covered(calendar, start, session)
    count, day = 0, start
    while day < session:
        if _is_session(calendar, day):
            count += 1
        day += timedelta(days=1)
    return count


def evaluation_due(cadence_index: int, rebalance_every: int) -> bool:
    """62's cadence (``i % rebalance_every == 0``) on the run's session index; the first decide is an evaluation."""

    return cadence_index % rebalance_every == 0


# --- the D-32 view ----------------------------------------------------------------------------------------------


def decision_dataset_view(rows: Sequence[Any], decision_session: date) -> DatasetView:
    """A 62 view sealed with ``HoldoutRange(S, S)``: any read of S or later raises ``HoldoutViolation`` (D-32, G-2a)."""

    return DatasetView.from_rows(rows, holdout=HoldoutRange(decision_session, decision_session))


# --- evaluation -------------------------------------------------------------------------------------------------


def _inputs_hash(rows: Sequence[Any], *, adjusted: bool) -> str:
    items = []
    for row in sorted(rows, key=lambda r: (r.anchor_isin, r.trade_date)):
        if adjusted:
            values = [None if v is None else str(v) for v in (row.adj_open, row.adj_high, row.adj_low, row.adj_close)]
            values.append(bool(row.adjusted_quarantined))
        else:
            values = [str(v) for v in (row.raw_open, row.raw_high, row.raw_low, row.raw_close)]
            values.append(int(row.raw_volume))
        items.append([row.anchor_isin, row.trade_date.isoformat(), *values])
    return canonical_sha256(items)


def _suppression(view: DatasetView, anchor: str, as_of: date, params: StrategyParams) -> str:
    series = view.series(anchor)
    if not series or series[-1].session != as_of:
        return SUPPRESS_MISSING_BAR
    need = max(params.lookback_sessions + params.skip_sessions, params.vol_window) + 1
    if any(bar.quarantined for bar in series[-need:]):
        return SUPPRESS_QUARANTINED
    return SUPPRESS_HISTORY


def _overlay(store: PilotDataStore, as_of: date) -> tuple[frozenset[str], frozenset[str], str]:
    """ISINs and symbols on an ASM or GSM list effective ``as_of`` (Q-7), and the hash of the lists read."""

    isins: set[str] = set()
    symbols: set[str] = set()
    sources: list[str] = []
    for kind in SURVEILLANCE_LISTS:
        ref = snapshot_for(store, kind, as_of)  # type: ignore[arg-type]
        if ref is None:
            raise PilotDataError("surveillance_snapshot_missing", f"no {kind} list effective on the as-of session")
        sources.append(ref.source_sha256)
        for entry in surveillance_entries(store, ref):
            if entry.isin is not None:
                isins.add(entry.isin)
            symbols.add(entry.nse_symbol)
    return frozenset(isins), frozenset(symbols), canonical_sha256({"as_of": as_of.isoformat(), "sources": sorted(sources)})


def _signal_id(as_of: date, isin: str) -> str:
    return f"sig-{as_of.strftime('%Y%m%d')}-{isin}"


def _text(*parts: str) -> str:
    return ";".join(parts)


class _Recorder:
    """Appends evidence once per deterministic id, so a restarted decide never writes the same signal twice."""

    def __init__(self, evidence: EvidenceStore) -> None:
        self._evidence = evidence
        self._existing = evidence.records()
        self.count = 0

    def has_signal(self, signal_id: str) -> bool:
        return any(r.get("kind") == "signal" and r.get("signal_id") == signal_id for r in self._existing)

    def signal_fingerprints(self, as_of: date) -> set[str | None]:
        """The ``evaluation_sha256`` of every signal already on the record for ``as_of``; None for one without."""

        found: set[str | None] = set()
        for record in self._existing:
            if record.get("kind") == "signal" and record.get("as_of_date") == as_of.isoformat():
                match = _EVALUATION_RE.search(str(record.get("reason", "")))
                found.add(match.group(1) if match else None)
        return found

    def has_non_evaluation(self, as_of: date) -> bool:
        return any(r.get("kind") == "non_evaluation_day" and r.get("as_of_date") == as_of.isoformat()
                   for r in self._existing)

    def append(self, kind: str, payload: Mapping[str, Any]) -> None:
        self._evidence.append(kind, payload)
        self.count += 1


def _pause(evidence: EvidenceStore, event: str, code: str, *, session: date, as_of: date | None,
           hashes: Mapping[str, str], cadence_index: int | None = None, evaluation: bool = False,
           recorded: int = 0) -> SignalResult:
    reason = _text(f"signals:{code}", HOLDOUT_FLAG)
    policy.raise_event(evidence, event, reason)
    return SignalResult(
        status=STATUS_PAUSED, session=session, as_of=as_of, cadence_index=cadence_index, evaluation_due=evaluation,
        hashes=dict(hashes), pause_event=event, pause_reason=code, recorded=recorded + 1,
    )


def _evaluation_fingerprint(result: SignalResult, live: LiveBarsResult, snapshot: EligibilitySnapshot,
                            blocked_isins: frozenset[str], blocked_symbols: frozenset[str], convention: Any) -> str:
    """Everything a decision for the session depends on, so two evaluations that differ anywhere differ here.

    The pins alone miss decision inputs (a derived dividend, a band file); composition, factor sets and unknown-dividend
    events come from 64-08, eligibility, overlay, bands and the band convention from this module, and the decision
    itself closes it.
    """

    return canonical_sha256({
        "contract": EVALUATION_CONTRACT,
        "pins": dict(sorted(result.hashes.items())),
        "composition_sha256": live.composition_sha256,
        "lineages": dict(sorted(live.lineage_hashes.items())),
        "factor_sets": dict(sorted(live.factor_set_hashes.items())),
        "dividend_amount_unknown_events": {
            anchor: sorted([event.ex_date.isoformat(), event.event_id] for event in found)
            for anchor, found in sorted(live.dividend_amount_unknown_events.items())},
        "sources": sorted(live.source_sha256s),
        "eligibility": {"result_sha256": snapshot.result_sha256, "eligible": sorted(snapshot.eligible),
                        "smallcap": dict(sorted(snapshot.smallcap.items()))},
        "overlay": {"sha256": result.overlay_sha256, "isins": sorted(blocked_isins), "symbols": sorted(blocked_symbols)},
        "adjusted_inputs_sha256": result.adjusted_inputs_sha256, "raw_inputs_sha256": result.raw_inputs_sha256,
        "bands": {isin: [band.status, None if band.percent is None else str(band.percent), band.nse_symbol,
                         band.series, band.source_kind, sorted(band.source_sha256s), band.reason]
                  for isin, band in sorted(result.bands.items())},
        "band_convention": None if convention is None else convention.model_dump(mode="json"),
        "decision_sha256": result.decision_sha256(),
    })


def evaluate_session(evidence: EvidenceStore, inputs: SignalInputs, *, decision_session: date) -> SignalResult:
    """The signal step of a decide for ``decision_session``. Records evidence, returns the typed result.

    Raises ``RunAborted`` when the run holds an abort (nothing may run) and ``SignalsError`` for a call that is not
    well formed (a session the pinned calendar does not call a session, a run start after the session, an
    uncovered calendar). Operational failures are recorded as the pause the policy table assigns and returned.
    """

    state = policy.run_state(evidence)
    state.require(policy.STEP_READ_ONLY)  # an abort blocks even this
    if not evidence.manifest.holdout_dated_inputs:  # D-31: the composed rows may be dated inside the sealed holdout
        raise SignalsError("manifest_not_holdout_dated")
    _require_covered(inputs.calendar, decision_session, inputs.run_start_session)
    if not _is_session(inputs.calendar, decision_session):
        raise SignalsError("decision_session_not_a_session")
    as_of = previous_session(inputs.calendar, decision_session)
    open_all_pause = next((p for p in state.open_pauses if p.scope == policy.SCOPE_ALL), None)
    try:
        hashes = inputs.current_hashes()
    except PilotDataError as error:  # not a seeded session store: nothing can be pinned, nothing is read
        if open_all_pause is None:
            return _pause(evidence, "stale_data", error.code, session=decision_session, as_of=as_of, hashes={})
        hashes = {}  # already paused: a repeat call must not append another pause record
    if open_all_pause is not None:  # a pause from the P-06 or D-07 list stops all processing: no read, no record
        return SignalResult(status=STATUS_PAUSED, session=decision_session, as_of=as_of, hashes=hashes,
                            pause_event=open_all_pause.event, pause_reason="already_paused")
    # 1. the pins: a hash that differs from the manifest pauses everything before any input is read
    pinned = evidence.manifest.hashes
    differing = tuple(name for name in PIN_NAMES if pinned.get(name) != hashes[name])
    if differing:
        return _pause(evidence, "hash_mismatch", "hash_mismatch:" + ",".join(differing), session=decision_session,
                      as_of=as_of, hashes=hashes)
    # 2. the cadence: a non-evaluation day records itself and reads no price
    params = inputs.strategy.params
    index = sessions_before(inputs.calendar, inputs.run_start_session, decision_session)
    due = evaluation_due(index, params.rebalance_every)
    recorder = _Recorder(evidence)
    if not due:
        if not recorder.has_non_evaluation(as_of):
            recorder.append("non_evaluation_day", {
                "as_of_date": as_of.isoformat(), "reason": "cadence",
                "cadence_state": _text(f"index={index}", f"every={params.rebalance_every}", HOLDOUT_FLAG),
            })
        return SignalResult(status=STATUS_NON_EVALUATION_DAY, session=decision_session, as_of=as_of,
                            cadence_index=index, evaluation_due=False, hashes=hashes, recorded=recorder.count)
    common = dict(session=decision_session, as_of=as_of, hashes=hashes, cadence_index=index, evaluation=True)
    # 3. reference freshness for S
    try:
        require_reference_freshness(inputs.store, today_ist=decision_session, calendar=inputs.calendar)
    except PilotDataError as error:
        return _pause(evidence, "freshness_check_failed", error.code, **common)
    # 4. bars as of S-1, composed with the real decision session
    try:
        live: LiveBarsResult = bars_as_of(
            inputs.store, targets=inputs.targets, decision_session=decision_session, as_of=as_of,
            window_start=inputs.window_start, tolerances=inputs.tolerances, workspace="india",
            calendar=inputs.calendar)
    except PilotDataError as error:
        return _pause(evidence, "stale_data", error.code, **common)
    # 5. eligibility at S-1 (pilot mode) and the ASM/GSM overlay effective S-1
    eligibility = inputs.eligibility or AsOfEligibility(inputs.store, inputs.targets, inputs.universe_policy, mode="pilot")
    try:
        snapshot = eligibility.snapshot(as_of)
        blocked_isins, blocked_symbols, overlay_sha = _overlay(inputs.store, as_of)
    except PilotDataError as error:
        return _pause(evidence, "stale_data", error.code, **common)
    if snapshot.as_of != as_of:
        raise SignalsError("eligibility_not_for_as_of")
    if not snapshot.eligible:
        return _pause(evidence, "stale_data", "eligible_universe_empty", **common)
    # 6. 62's signal code on a view sealed at S (D-32)
    view = decision_dataset_view(live.rows, decision_session)
    table = SignalTable(view, params, events_from_manifest(live), mode=MODE_BASE)
    ctx = DecisionContext(as_of, snapshot.eligible, DecisionView(view, as_of), table)
    raw = MomentumProvider().scores(ctx)
    entries_possible = len(raw) >= params.min_universe_for_entry
    z = zscores(raw) if entries_possible else {}
    ranked = sorted(z.items(), key=lambda kv: (-kv[1], kv[0]))
    ranks = {anchor: position + 1 for position, (anchor, _) in enumerate(ranked)}
    # only what the sealed view can read: rows dated before S. The hashes and the name lookup use the same rule, so a
    # row dated S or later could not move a hash even if 64-08 handed one back.
    visible = [row for row in live.rows if row.trade_date < decision_session]
    latest: dict[str, Any] = {}
    for row in sorted(visible, key=lambda r: r.trade_date):
        latest[row.anchor_isin] = row
    members = {m.anchor_isin: m for m in inputs.targets.members}
    names: list[NameSignal] = []
    for anchor in sorted(snapshot.eligible):
        row = latest.get(anchor)
        member = members.get(anchor)
        symbol = row.nse_symbol if row is not None else (member.nse_symbol if member is not None else None)
        if symbol is None:
            raise SignalsError("eligible_name_without_symbol")
        isin = row.isin if row is not None else anchor
        series = row.series if row is not None else "EQ"
        score = raw.get(anchor)
        blocked = anchor in blocked_isins or isin in blocked_isins or symbol in blocked_symbols
        names.append(NameSignal(
            anchor_isin=anchor, isin=isin, symbol=symbol, series=series, raw_score=score, zscore=z.get(anchor),
            rank=ranks.get(anchor), buy_blocked=blocked,
            suppressed=None if score is not None else _suppression(view, anchor, as_of, params)))
    # 7. the band for S of the names that could be bought or held (rank within the hold cutoff)
    bands: dict[str, BandObservation] = {}
    try:
        for name in names:
            if name.rank is not None and name.rank <= params.hold_rank_cutoff:
                bands[name.isin] = band_for_upcoming_session(
                    inputs.store, isin=name.isin, prior_session=as_of, session=decision_session,
                    calendar=inputs.calendar, workspace="india")
        convention = current_band_convention(inputs.store)
    except PilotDataError as error:  # e.g. band_convention_conflict: no band can be trusted, nothing is recorded
        return _pause(evidence, "stale_data", error.code, **common)
    adjusted_sha = _inputs_hash(visible, adjusted=True)
    raw_sha = _inputs_hash(visible, adjusted=False)
    result = SignalResult(
        status=STATUS_EVALUATED, session=decision_session, as_of=as_of, cadence_index=index, evaluation_due=True,
        hashes=hashes, signals=tuple(names), eligible=snapshot.eligible, eligibility_sha256=snapshot.result_sha256,
        surveillance_as_of=as_of, overlay_blocked=frozenset(
            n.anchor_isin for n in names if n.buy_blocked), overlay_sha256=overlay_sha,
        adjusted_inputs_sha256=adjusted_sha, raw_inputs_sha256=raw_sha, composition_sha256=live.composition_sha256,
        bands=bands, entries_possible=entries_possible)
    fingerprint = _evaluation_fingerprint(result, live, snapshot, blocked_isins, blocked_symbols, convention)
    # a session already on the record under another evaluation (or under none) is a conflict, never a dedupe
    if recorder.signal_fingerprints(as_of) - {fingerprint}:
        return _pause(evidence, "hash_mismatch", CONFLICT_CODE, **common)
    # 8. one signal record per eligible name, ranked or suppressed, with the nine pins and the evaluation fingerprint
    for name in names:
        signal_id = _signal_id(as_of, name.isin)
        if recorder.has_signal(signal_id):
            continue
        detail = [f"surveillance_as_of={as_of.isoformat()}", HOLDOUT_FLAG, f"{EVALUATION_KEY}={fingerprint}"]
        if name.suppressed is not None:
            detail.insert(0, f"suppressed={name.suppressed}")
        else:
            detail.insert(0, f"rank={name.rank}" if name.rank is not None else "rank=none")
            detail.insert(1, f"raw={name.raw_score}")
        if name.buy_blocked:
            detail.insert(0, "buy_blocked=surveillance")
        recorder.append("signal", {
            "signal_id": signal_id, "symbol": name.symbol, "isin": name.isin, "series": name.series,
            "as_of_date": as_of.isoformat(), **{pin: hashes[pin] for pin in HASH_NAMES}, "reason": _text(*detail),
            "adjusted_inputs_sha256": adjusted_sha, "raw_inputs_sha256": raw_sha,
        })
    return replace(result, evaluation_sha256=fingerprint, recorded=recorder.count)


__all__ = [
    "AsOfEligibility", "CONFLICT_CODE", "EVALUATION_KEY", "HASH_NAMES", "HOLDOUT_FLAG", "PIN_NAMES", "SEED_PIN", "NameSignal", "SignalInputs", "SignalResult", "SignalsError", "StrategyConfig",
    "STATUS_EVALUATED", "STATUS_NON_EVALUATION_DAY", "STATUS_PAUSED", "decision_dataset_view", "evaluate_session",
    "evaluation_due", "load_strategy_config", "previous_session", "seed_pin", "seed_rows", "sessions_before",
]

"""GMM regime severity at the classifier and admission boundary (GMM-REGIME-DEFECT fix).

No test here patches model inference. The real classifier reads the real (or a
deliberately renumbered or broken) artifact, the severity map orders it, and the real
risk gate sizes the order. India goes through ``AppState``; the UK quote-to-admission
fixture lives in ``test_uk_practice_admission.py``.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

import india_limits_support as ils
from app_context import AppState
from execution import AdmissionDecision, ExecutionLedger, ExecutionService, OrderIntent
from market_data import (
    IndiaInstrument,
    MarketDataError,
    MarketDataSession,
    RegimeClassifier,
    RegimeEvidence,
    ReplayMarketDataProvider,
    TopOfBook,
    build_market_preflight_context,
)
from regime_testkit import ARTIFACT, permuted_params, shipped_map, shipped_params
from simulation import RiskSwarmGate
from simulation.regime_severity import build_scaling_policy_connection, build_severity_map, policy_table_matches

INSTRUMENT = IndiaInstrument(symbol="RELIANCE")
PERMUTATIONS = list(itertools.permutations(range(4)))


def _quotes(mids, *, half_fraction="0.0005", start=1):
    now = datetime.now(timezone.utc)
    events = []
    for offset, mid in enumerate(mids):
        mid = Decimal(str(mid))
        half = (mid * Decimal(half_fraction)).quantize(Decimal("0.0001"))
        events.append(
            TopOfBook(
                instrument=INSTRUMENT, source="local-replay", bid=mid - half, ask=mid + half,
                observed_at=now, received_at=now, sequence=start + offset,
            )
        )
    return tuple(events)


def _swing(volatility, base="100"):
    """Three mids whose log returns are +v and -v: the window's volatility feature is v."""

    middle = (Decimal(base) * Decimal(str(float(np.exp(volatility))))).quantize(Decimal("0.0001"))
    return (base, str(middle), base)


async def _session(events, *, now=None):
    now = now or events[-1].observed_at
    session = MarketDataSession(ReplayMarketDataProvider([]), clock=lambda: now)
    await session.start((INSTRUMENT,))
    for event in events:
        session.ingest(event)
    return session, now


async def _classify(classifier, mids, **kwargs):
    events = _quotes(mids, **kwargs)
    session, now = await _session(events)
    return classifier.evidence(session, INSTRUMENT, now=now)


# --- the classifier carries raw id, severity and the version evidence -------------------------


@pytest.mark.asyncio
async def test_a_tight_flat_window_classifies_as_the_calm_component_id_3_not_a_5_percent_regime():
    evidence = await _classify(RegimeClassifier(), ("100", "100.01", "100.02"))
    assert evidence.regime_id == 3, "the raw id stays as the model emitted it"
    assert (evidence.severity_rank, evidence.severity_label) == (0, "calm")
    severity_map = shipped_map()
    assert evidence.policy_hash == severity_map.policy_hash
    assert evidence.policy_version == severity_map.policy_version
    assert evidence.mapping_version == severity_map.mapping_version
    assert evidence.model_version.startswith("gmm-p256:")
    assert evidence.audit()["policy_hash"] == severity_map.policy_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "volatility,raw_id,label,rank",
    [(0.0, 3, "calm", 0), (0.08, 0, "normal", 1), (0.25, 2, "stressed", 2)],
)
async def test_real_features_reach_the_normal_and_stressed_components_with_their_severity(
    volatility, raw_id, label, rank
):
    evidence = await _classify(RegimeClassifier(), _swing(volatility))
    assert (evidence.regime_id, evidence.severity_label, evidence.severity_rank) == (raw_id, label, rank)


@pytest.mark.asyncio
async def test_the_crisis_centroid_classifies_as_the_most_severe_component():
    # Crisis features are volatility 2.4 and spread 0.76 (the artifact's raw means).
    evidence = await _classify(RegimeClassifier(), _swing(2.4, base="10"), half_fraction="0.38")
    assert (evidence.regime_id, evidence.severity_label, evidence.severity_rank) == (1, "crisis", 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", PERMUTATIONS)
async def test_a_renumbered_artifact_classifies_the_same_windows_with_the_same_meaning(order, tmp_path):
    path = tmp_path / "renumbered.npz"
    np.savez(path, **permuted_params(order))
    base, renumbered = RegimeClassifier(), RegimeClassifier(path)
    for volatility in (0.0, 0.08, 0.25):
        expected = await _classify(base, _swing(volatility))
        actual = await _classify(renumbered, _swing(volatility))
        assert actual.severity_label == expected.severity_label
        assert actual.severity_rank == expected.severity_rank
        assert actual.regime_id == order.index(expected.regime_id), "raw ids move; meaning does not"
        # The model hash differs (different bytes) and so does the policy hash, but the
        # size policy that results is the same.
        severity_map = renumbered.severity_map
        assert severity_map.size_multiplier(actual.regime_id) == shipped_map().size_multiplier(expected.regime_id)


def _write_model(tmp_path, params, name="model.npz"):
    path = tmp_path / name
    np.savez(path, **params)
    return path


def _tied(params):
    params["means"] = params["means"].copy()
    params["means"][0] = params["means"][3]
    return params


def _five(params):
    count = 5
    return {
        "weights": np.full(count, 1.0 / count),
        "means": np.array([[i, i] for i in range(count)], dtype=float),
        "precisions_cholesky": np.tile(np.eye(2), (count, 1, 1)),
        "scaler_mean": params["scaler_mean"],
        "scaler_var": params["scaler_var"],
    }


def _singular(params):
    params["precisions_cholesky"] = np.zeros_like(params["precisions_cholesky"])
    return params


def _bad_scaler(params):
    params["scaler_var"] = np.array([0.0, 0.01])
    return params


@pytest.mark.parametrize(
    "breaker,codes",
    [
        (_tied, {"REGIME_SEVERITY_AMBIGUOUS"}),
        (_five, {"REGIME_K_UNSUPPORTED"}),
        (_singular, {"REGIME_COVARIANCE_INVALID"}),
        (_bad_scaler, {"REGIME_MODEL_INVALID", "REGIME_SCALER_INVALID"}),
    ],
)
def test_the_classifier_refuses_to_load_a_model_that_cannot_be_ordered(breaker, codes, tmp_path):
    path = _write_model(tmp_path, breaker(shipped_params()))
    with pytest.raises(MarketDataError) as error:
        RegimeClassifier(path)
    assert error.value.code in codes


def test_the_classifier_loads_a_k_3_model_and_orders_it():
    params = shipped_params()
    keep = [3, 0, 1]
    weights = params["weights"][keep]
    reduced = {
        "weights": weights / weights.sum(),
        "means": params["means"][keep],
        "precisions_cholesky": params["precisions_cholesky"][keep],
        "scaler_mean": params["scaler_mean"],
        "scaler_var": params["scaler_var"],
    }
    severity_map = build_severity_map(reduced)
    assert severity_map.id_by_rank == (0, 1, 2)
    assert [severity_map.label(i) for i in range(3)] == ["calm", "stressed", "crisis"]
    assert [severity_map.size_multiplier(i) for i in range(3)] == [1.0, 0.1, 0.05]


@pytest.mark.asyncio
async def test_a_regime_id_the_model_does_not_have_is_refused_at_classification(monkeypatch):
    import market_data.regime as regime_module

    monkeypatch.setattr(regime_module, "fast_gmm_predict_proba", lambda feature, **p: np.array([0.0, 0.0, 0.0, 0.0, 1.0]))
    with pytest.raises(MarketDataError) as error:
        await _classify(RegimeClassifier(), ("100", "100.01", "100.02"))
    assert error.value.code == "REGIME_INFERENCE_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "posterior",
    [
        np.array([0.5, 0.5, 0.5, 0.5]),  # finite but not a probability vector
        np.array([1.2, -0.2, 0.0, 0.0]),
        np.array([0.0, 0.0, 0.0, 0.0]),
        np.array([np.nan, np.nan, np.nan, np.nan]),
    ],
    ids=["sums-to-two", "negative", "all-zero", "nan"],
)
async def test_an_invalid_posterior_denies_instead_of_picking_component_zero(posterior, monkeypatch):
    import market_data.regime as regime_module

    monkeypatch.setattr(regime_module, "fast_gmm_predict_proba", lambda feature, **p: posterior)
    with pytest.raises(MarketDataError) as error:
        await _classify(RegimeClassifier(), ("100", "100.01", "100.02"))
    assert error.value.code == "REGIME_INFERENCE_FAILED"


def test_the_classifier_refuses_a_model_with_negated_precision_factors(tmp_path):
    params = shipped_params()
    params["precisions_cholesky"] = -params["precisions_cholesky"]
    with pytest.raises(MarketDataError) as error:
        RegimeClassifier(_write_model(tmp_path, params))
    assert error.value.code == "REGIME_COVARIANCE_INVALID"


# --- India: the fixture that was being sized at 5% ---------------------------------------------


async def _india_admit(tmp_path, private_config_dir, mids, *, quantity="1", half_fraction="0.0005"):
    state = AppState()
    assert state.start_execution(
        tmp_path / "execution.sqlite3", workspace="india", private_dir=private_config_dir,
        india_clock=lambda: ils.NOW,
    ), state.execution_startup_error
    try:
        await state.start_market_data_replay((INSTRUMENT,), _quotes(mids, half_fraction=half_fraction))
        state._execution_ledger.configure_paper_budget("paper", "INR", "1000", workspace="india")
        proposal = OrderIntent(
            proposal_id="india-severity", workspace="india", account="paper", broker="paper", mode="PAPER",
            ticker=INSTRUMENT.execution_ticker, side="BUY", quantity=Decimal(quantity),
            order_type="LIMIT", limit_price=Decimal("100.00"),
        ).model_dump(mode="json")
        admission = state.admit_india_paper_proposal(
            proposal, instrument=INSTRUMENT, portfolio_state={"equity": 1000.0, "peak_equity": 1000.0},
            quote=ils.make_evidence(),
        )
        reservation = state._execution_ledger.get_reservation("india-severity")
        return admission, reservation
    finally:
        await state.close_market_data()
        state.close_execution()


@pytest.mark.asyncio
async def test_the_calm_india_fixture_gets_its_intended_full_size(tmp_path, private_config_dir):
    """Calm used to admit 0.05 of a share. It now keeps the whole request."""

    admission, reservation = await _india_admit(tmp_path, private_config_dir, ("100", "100.01", "100.02"), half_fraction="0.01")
    assert admission.decision is AdmissionDecision.ADMITTED
    assert admission.risk_quantity == Decimal("1")
    assert admission.final_quantity == Decimal("1")
    assert reservation is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "volatility,expected",
    [(0.08, Decimal("0.5")), (0.25, Decimal("0.1"))],
)
async def test_normal_and_stressed_india_fixtures_reduce_the_size_by_their_schedule(
    volatility, expected, tmp_path, private_config_dir
):
    admission, _ = await _india_admit(tmp_path, private_config_dir, _swing(volatility))
    assert admission.decision is AdmissionDecision.ADMITTED
    assert admission.final_quantity == expected
    assert admission.final_quantity < Decimal("1")


@pytest.mark.asyncio
async def test_a_crisis_window_never_reaches_a_fill_the_spread_block_denies_it(tmp_path, private_config_dir):
    admission, reservation = await _india_admit(
        tmp_path, private_config_dir, _swing(2.4, base="100"), half_fraction="0.38"
    )
    assert admission.decision is AdmissionDecision.DENIED
    assert admission.final_quantity == Decimal("0")
    assert reservation is None


def test_the_crisis_component_is_sized_at_five_percent_by_the_policy_table():
    assert shipped_map().size_multiplier(1) == 0.05


@pytest.mark.asyncio
async def test_an_india_start_with_a_model_that_cannot_be_ordered_leaves_execution_disabled(
    tmp_path, private_config_dir, monkeypatch
):
    import app_context

    def broken_classifier(*args, **kwargs):
        raise MarketDataError("REGIME_SEVERITY_AMBIGUOUS", "two regime components have tied severity scores")

    monkeypatch.setattr(app_context, "RegimeClassifier", broken_classifier)
    state = AppState()
    started = state.start_execution(
        tmp_path / "execution.sqlite3", workspace="india", private_dir=private_config_dir,
        india_clock=lambda: ils.NOW,
    )
    assert started is False
    assert state.execution_authority is False
    assert "REGIME_SEVERITY_AMBIGUOUS" in state.execution_startup_error
    assert not (tmp_path / "execution.sqlite3").exists(), "no ledger is opened for an unorderable model"


@pytest.mark.asyncio
async def test_the_app_owned_size_policy_is_bound_to_the_classifiers_severity_map(tmp_path, private_config_dir):
    state = AppState()
    assert state.start_execution(
        tmp_path / "execution.sqlite3", workspace="india", private_dir=private_config_dir,
        india_clock=lambda: ils.NOW,
    )
    try:
        severity_map = state._regime_severity_map()
        assert policy_table_matches(state._preflight_policy_connection, severity_map)
        rows = dict(state._preflight_policy_connection.execute("SELECT regime_id, scale_multiplier FROM scaling_policies"))
        assert rows == {3: 1.0, 0: 0.5, 2: 0.1, 1: 0.05}
        local = state._local_paper_preflight()
        assert local["regime_id"] == 3 and local["regime_policy_hash"] == severity_map.policy_hash
        assert local["regime_audit"]["policy_hash"] == severity_map.policy_hash
        assert local["regime_audit"]["regime_id"] == 3 and local["regime_audit"]["severity_label"] == "calm"
        assert local["regime_audit"]["model_version"]
        assert state.execution_service._regime_severity_map is severity_map
    finally:
        state.close_execution()


# --- the service binds policy and model versions into the admission evidence -------------------


class _FixedSimulator:
    """Deterministic simulator evidence so two admissions can be compared byte for byte."""

    def simulate_execution(self, side, quantity, tick_window, portfolio_state):
        return {"simulated_fill_price": 100.0, "simulator_drawdown_pct": 0.0}


class _CountingGate(RiskSwarmGate):
    def __init__(self):
        self.calls = 0

    def evaluate(self, *args, **kwargs):
        self.calls += 1
        return super().evaluate(*args, **kwargs)


def _intent(proposal_id, quantity="1"):
    return OrderIntent(
        proposal_id=proposal_id, workspace="india", account="paper", broker="paper", mode="PAPER",
        ticker=INSTRUMENT.execution_ticker, side="BUY", quantity=Decimal(quantity),
        order_type="LIMIT", limit_price=Decimal("100.00"),
    )


_TRUSTED = object()


def _admit(
    tmp_path, name, *, regime_id, policy_hash, audit, connection, gate=None, runtime_preflight=True, at=None,
    trusted=_TRUSTED, risk_evidence=None, spread=0.002,
):
    (tmp_path / name).mkdir()
    private = ils.india_private_dir(tmp_path / name)
    with ExecutionLedger(tmp_path / f"{name}.sqlite3", workspace="india") as ledger:
        service = ExecutionService(
            MagicMock(dispatch=AsyncMock()), ledger, simulator=_FixedSimulator(), risk_gate=gate or RiskSwarmGate(),
            require_runtime_preflight=runtime_preflight, india_guard=ils.make_guard(ledger, private),
            regime_severity_map=shipped_map() if trusted is _TRUSTED else trusted,
        )
        kwargs = {
            "price": "100", "tick_window": {"bid": [99.9], "ask": [100.1], "spread": [0.002]},
            "portfolio_state": {"equity": 1000.0, "peak_equity": 1000.0},
            "current_spread_pct": spread, "risk_db_connection": connection,
            "evidence_at": at or datetime.now(timezone.utc), "india_quote": ils.make_evidence(),
        }
        if regime_id is not None:
            kwargs["regime_id"] = regime_id
        if risk_evidence is not None:
            kwargs["risk_evidence"] = risk_evidence
        if policy_hash is not None:
            kwargs["regime_policy_hash"] = policy_hash
        if audit is not None:
            kwargs["regime_audit"] = audit
        return service.admit(_intent(f"p-{name}"), currency="INR", **kwargs)


def _audit(**overrides):
    severity_map = shipped_map()
    audit = {
        "regime_id": 3, "severity_rank": 0, "severity_label": "calm", "model_version": "gmm-p256:abc",
        "mapping_version": severity_map.mapping_version, "policy_version": severity_map.policy_version,
        "policy_hash": severity_map.policy_hash,
    }
    audit.update(overrides)
    return audit


def test_the_admission_evidence_hash_binds_the_regime_model_ordering_and_policy(tmp_path):
    severity_map = shipped_map()
    connection = build_scaling_policy_connection(severity_map)
    at = datetime.now(timezone.utc)  # one instant, so only the regime evidence can differ
    try:
        def evidence_hash(name, **audit):
            return _admit(
                tmp_path, name, regime_id=3, policy_hash=severity_map.policy_hash,
                audit=_audit(**audit) if audit != {"none": True} else None, connection=connection, at=at,
            ).evidence_hash

        reference = evidence_hash("a")
        assert evidence_hash("b") == reference, "identical evidence hashes identically"
        for index, change in enumerate(
            (
                {"policy_hash": "f" * 64}, {"model_version": "gmm-p256:other"}, {"policy_version": "v-next"},
                {"mapping_version": "score-v2"}, {"regime_id": 0}, {"severity_rank": 1}, {"severity_label": "normal"},
            )
        ):
            assert evidence_hash(f"c{index}", **change) != reference, change
        assert evidence_hash("none", none=True) != reference, "an admission without regime evidence differs"
    finally:
        connection.close()


def test_a_calm_admission_through_the_service_keeps_its_quantity(tmp_path):
    severity_map = shipped_map()
    connection = build_scaling_policy_connection(severity_map)
    try:
        admission = _admit(
            tmp_path, "calm", regime_id=severity_map.calm_id, policy_hash=severity_map.policy_hash,
            audit=_audit(), connection=connection,
        )
        assert admission.decision is AdmissionDecision.ADMITTED and admission.final_quantity == Decimal("1")
    finally:
        connection.close()


def test_a_policy_built_for_another_ordering_denies_before_the_gate_runs(tmp_path):
    severity_map = shipped_map()
    other = build_severity_map(permuted_params((1, 0, 2, 3)))
    foreign = build_scaling_policy_connection(other)
    gate = _CountingGate()
    try:
        admission = _admit(
            tmp_path, "mismatch", regime_id=3, policy_hash=severity_map.policy_hash, audit=_audit(),
            connection=foreign, gate=gate,
        )
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "REGIME_POLICY_MISMATCH"
        assert admission.final_quantity == Decimal("0")
        assert gate.calls == 0
    finally:
        foreign.close()


def test_a_hand_built_raw_keyed_table_cannot_stand_in_for_the_policy(tmp_path):
    import sqlite3

    handmade = sqlite3.connect(":memory:")
    handmade.execute("CREATE TABLE scaling_policies (regime_id INTEGER PRIMARY KEY, scale_multiplier REAL NOT NULL)")
    handmade.executemany("INSERT INTO scaling_policies VALUES (?, ?)", ((0, 1.0), (1, 0.5), (2, 0.1), (3, 0.05)))
    try:
        admission = _admit(
            tmp_path, "handmade", regime_id=3, policy_hash=shipped_map().policy_hash, audit=_audit(), connection=handmade,
        )
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "REGIME_POLICY_MISMATCH"
    finally:
        handmade.close()


def test_an_unclassified_order_is_denied_instead_of_defaulting_to_raw_id_0(tmp_path):
    connection = build_scaling_policy_connection(shipped_map())
    gate = _CountingGate()
    try:
        admission = _admit(
            tmp_path, "unclassified", regime_id=None, policy_hash=None, audit=None, connection=connection,
            gate=gate, runtime_preflight=False,
        )
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "GMM_REGIME_ID_IS_REQUIRED"
        assert gate.calls == 0
    finally:
        connection.close()


@pytest.mark.parametrize("unknown", [4, 9, -1])
def test_a_regime_id_outside_the_model_is_denied_with_no_quantity(unknown, tmp_path):
    severity_map = shipped_map()
    connection = build_scaling_policy_connection(severity_map)
    try:
        admission = _admit(
            tmp_path, f"unknown{abs(unknown)}", regime_id=unknown, policy_hash=severity_map.policy_hash,
            audit=_audit(regime_id=unknown), connection=connection,
        )
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.final_quantity == Decimal("0")
    finally:
        connection.close()


def _handmade_raw_keyed_table():
    """The pre-fix table: raw ids as if they were severities (0 full size, 3 five percent)."""

    import sqlite3

    handmade = sqlite3.connect(":memory:")
    handmade.execute("CREATE TABLE scaling_policies (regime_id INTEGER PRIMARY KEY, scale_multiplier REAL NOT NULL)")
    handmade.executemany("INSERT INTO scaling_policies VALUES (?, ?)", ((0, 1.0), (1, 0.5), (2, 0.1), (3, 0.05)))
    return handmade


@pytest.mark.parametrize("runtime_preflight", [True, False])
def test_the_old_raw_id_table_with_no_binding_is_denied_not_admitted_at_full_size(tmp_path, runtime_preflight):
    """C3: omitting the hash and audit used to skip the binding, so the old table admitted raw
    id 0 (the NORMAL component) at full size with no regime audit."""

    handmade = _handmade_raw_keyed_table()
    gate = _CountingGate()
    try:
        admission = _admit(
            tmp_path, f"oldtable{runtime_preflight}", regime_id=0, policy_hash=None, audit=None,
            connection=handmade, gate=gate, runtime_preflight=runtime_preflight,
        )
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "REGIME_BINDING_REQUIRED"
        assert admission.final_quantity == Decimal("0")
        assert gate.calls == 0
    finally:
        handmade.close()


def test_the_old_raw_id_table_with_a_copied_hash_and_audit_is_still_denied(tmp_path):
    handmade = _handmade_raw_keyed_table()
    severity_map = shipped_map()
    gate = _CountingGate()
    try:
        admission = _admit(
            tmp_path, "oldcopied", regime_id=0, policy_hash=severity_map.policy_hash,
            audit=_audit(regime_id=0, severity_rank=1, severity_label="normal"), connection=handmade, gate=gate,
        )
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "REGIME_POLICY_MISMATCH"
        assert gate.calls == 0
    finally:
        handmade.close()


@pytest.mark.parametrize(
    "policy_hash,audit,code",
    [
        (None, "audit", "REGIME_BINDING_REQUIRED"),
        ("hash", None, "REGIME_BINDING_REQUIRED"),
        ("hash", "audit-empty", "REGIME_BINDING_REQUIRED"),
        ("hash", "audit-no-model", "REGIME_BINDING_REQUIRED"),
        ("f" * 64, "audit", "REGIME_POLICY_MISMATCH"),
        ("hash", "audit-wrong-rank", "REGIME_POLICY_MISMATCH"),
        ("hash", "audit-wrong-label", "REGIME_POLICY_MISMATCH"),
        ("hash", "audit-wrong-id", "REGIME_POLICY_MISMATCH"),
        ("hash", "audit-wrong-policy", "REGIME_POLICY_MISMATCH"),
    ],
)
def test_a_missing_or_inconsistent_regime_binding_denies_even_with_the_right_table(
    policy_hash, audit, code, tmp_path
):
    severity_map = shipped_map()
    connection = build_scaling_policy_connection(severity_map)
    audits = {
        "audit": _audit(),
        "audit-empty": {},
        "audit-no-model": {k: v for k, v in _audit().items() if k != "model_version"},
        "audit-wrong-rank": _audit(severity_rank=2),
        "audit-wrong-label": _audit(severity_label="crisis"),
        "audit-wrong-id": _audit(regime_id=0),
        "audit-wrong-policy": _audit(policy_version="other"),
    }
    gate = _CountingGate()
    try:
        admission = _admit(
            tmp_path, f"binding-{abs(hash((str(policy_hash), str(audit))))}", regime_id=3,
            policy_hash=severity_map.policy_hash if policy_hash == "hash" else policy_hash,
            audit=audits.get(audit, audit), connection=connection, gate=gate,
        )
        assert admission.decision is AdmissionDecision.DENIED and admission.reason_code == code
        assert gate.calls == 0
    finally:
        connection.close()


def test_a_service_with_no_trusted_severity_map_denies_every_gated_admission(tmp_path):
    severity_map = shipped_map()
    connection = build_scaling_policy_connection(severity_map)
    gate = _CountingGate()
    try:
        admission = _admit(
            tmp_path, "nomap", regime_id=3, policy_hash=severity_map.policy_hash, audit=_audit(),
            connection=connection, gate=gate, trusted=None,
        )
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "REGIME_SEVERITY_MAP_UNAVAILABLE"
        assert gate.calls == 0
    finally:
        connection.close()


def test_a_tampered_crisis_row_with_a_matching_hash_row_denies_at_the_service(tmp_path):
    """C2: crisis (raw id 1) raised from 0.05 to 1.0, hash row and audit untouched."""

    severity_map = shipped_map()
    connection = build_scaling_policy_connection(severity_map)
    connection.execute("UPDATE scaling_policies SET scale_multiplier = 1.0 WHERE regime_id = 1")
    gate = _CountingGate()
    try:
        admission = _admit(
            tmp_path, "tamper", regime_id=1, policy_hash=severity_map.policy_hash,
            audit=_audit(regime_id=1, severity_rank=3, severity_label="crisis"), connection=connection, gate=gate,
        )
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "REGIME_POLICY_MISMATCH"
        assert admission.final_quantity == Decimal("0")
        assert gate.calls == 0
    finally:
        connection.close()


# --- fix round 2: every executable admission is gated and bound; the gate alone sizes it ------


def _uk_paper_service(ledger, **kwargs):
    return ExecutionService(MagicMock(dispatch=AsyncMock()), ledger, **kwargs)


def _uk_intent(pid, qty="2"):
    return OrderIntent(
        proposal_id=pid, workspace="uk", account="invest", broker="paper", mode="PAPER",
        ticker="VUSA", side="BUY", quantity=Decimal(qty),
    )


@pytest.mark.parametrize("with_binding", [False, True])
def test_an_admission_with_no_risk_gate_is_denied_even_with_complete_caller_evidence(tmp_path, with_binding):
    """P1-a: no gate used to skip the binding, so caller evidence admitted and reserved 2 of 2."""

    from regime_testkit import bound_regime_fields

    with ExecutionLedger(tmp_path / "l.sqlite3", workspace="uk") as ledger:
        ledger.configure_paper_budget("invest", "GBP", "1000", workspace="uk")
        service = _uk_paper_service(ledger, regime_severity_map=shipped_map())
        extra = bound_regime_fields() if with_binding else {}
        admission = service.admit(
            _uk_intent("nogate"), currency="GBP", price="100",
            simulator_evidence={"simulated_fill_price": "100"}, risk_evidence={"scaled_size": "2"}, **extra,
        )
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "REGIME_BINDING_REQUIRED"
        assert admission.final_quantity == Decimal("0")
        assert ledger.get_reservation("nogate") is None
        with pytest.raises(Exception, match="admission"):
            service.reserve("nogate")


def _india_sell_service(tmp_path):
    private = ils.india_private_dir(tmp_path)
    ledger = ils.open_ledger(tmp_path)
    guard = ils.make_guard(ledger, private)
    service = ExecutionService(
        MagicMock(dispatch=AsyncMock()), ledger, simulator=_FixedSimulator(), risk_gate=RiskSwarmGate(),
        require_runtime_preflight=True, india_guard=guard, regime_severity_map=shipped_map(),
    )
    ils.seed_position(ledger, ils.TICKER, 7, "700.00", guard=guard)
    return ledger, service


def _sell(service, connection, pid, **fields):
    return service.admit(
        ils.make_intent(pid, side="SELL", quantity=7), currency="INR", price="100.00",
        tick_window={"bid": [99.9], "ask": [100.1], "spread": [0.002]},
        portfolio_state={"equity": 1000.0, "peak_equity": 1000.0}, current_spread_pct=0.002,
        risk_db_connection=connection, evidence_at=datetime.now(timezone.utc),
        india_quote=ils.make_evidence(), **fields,
    )


def test_a_guarded_india_sell_needs_a_verified_binding_and_still_keeps_its_exact_quantity(tmp_path):
    from regime_testkit import bound_regime_fields

    ledger, service = _india_sell_service(tmp_path)
    connection = build_scaling_policy_connection(shipped_map())
    tampered = build_scaling_policy_connection(shipped_map())
    tampered.execute("UPDATE scaling_policies SET scale_multiplier = 1.0 WHERE regime_id = 1")
    try:
        # Denials first: a denied SELL holds no quantity, so the valid one below still fits.
        unbound = _sell(service, connection, "sell-unbound", regime_id=3)
        assert unbound.decision is AdmissionDecision.DENIED and unbound.reason_code == "REGIME_BINDING_REQUIRED"
        crisis = _sell(service, tampered, "sell-tampered", **bound_regime_fields(1))
        assert crisis.decision is AdmissionDecision.DENIED and crisis.reason_code == "REGIME_POLICY_MISMATCH"
        # Normal regime (raw id 0) scales a BUY to half. A SELL deploys no capital, so the
        # D-06 waiver keeps the exact 7, but only after the binding has been verified.
        valid = _sell(service, connection, "sell-ok", **bound_regime_fields(0))
        assert valid.decision is AdmissionDecision.ADMITTED and valid.final_quantity == Decimal("7")
    finally:
        connection.close()
        tampered.close()
        ledger.close()


def test_a_guarded_india_sell_with_no_gate_is_denied(tmp_path):
    from regime_testkit import bound_regime_fields

    private = ils.india_private_dir(tmp_path)
    ledger = ils.open_ledger(tmp_path)
    guard = ils.make_guard(ledger, private)
    service = ExecutionService(
        MagicMock(dispatch=AsyncMock()), ledger, india_guard=guard, regime_severity_map=shipped_map()
    )
    ils.seed_position(ledger, ils.TICKER, 7, "700.00", guard=guard)
    try:
        admission = ils.admit(service, ils.make_intent("sell-nogate", side="SELL", quantity=7))
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "REGIME_BINDING_REQUIRED"
        assert ledger.get_reservation("sell-nogate") is None
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "regime_id,spread,caller,expected_reason",
    [
        (1, 0.002, "1", "RISK_EVIDENCE_CONFLICT"),  # crisis scales 1 to 0.05
        (3, 0.06, "1", "RISK_EVIDENCE_CONFLICT"),  # spread veto: the gate returns 0
        (0, 0.002, "1", "RISK_EVIDENCE_CONFLICT"),  # normal scales 1 to 0.5
    ],
)
def test_a_caller_admitted_quantity_cannot_override_the_gates_output(
    regime_id, spread, caller, expected_reason, tmp_path
):
    """P1-b: ``risk_evidence["admitted_quantity"]`` used to win over the gate's own result."""

    from regime_testkit import bound_regime_fields

    connection = build_scaling_policy_connection(shipped_map())
    gate = _CountingGate()
    fields = bound_regime_fields(regime_id)
    try:
        admission = _admit(
            tmp_path, f"override{regime_id}", regime_id=regime_id, policy_hash=fields["regime_policy_hash"],
            audit=fields["regime_audit"], connection=connection, gate=gate, spread=spread,
            risk_evidence={"admitted_quantity": caller},
        )
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == expected_reason
        assert admission.final_quantity == Decimal("0")
        assert gate.calls == 1
    finally:
        connection.close()


def test_a_caller_admitted_quantity_that_agrees_with_the_gate_changes_nothing(tmp_path):
    from regime_testkit import bound_regime_fields

    connection = build_scaling_policy_connection(shipped_map())
    fields = bound_regime_fields()
    try:
        admission = _admit(
            tmp_path, "agree", regime_id=fields["regime_id"], policy_hash=fields["regime_policy_hash"],
            audit=fields["regime_audit"], connection=connection, risk_evidence={"admitted_quantity": "1"},
        )
        assert admission.decision is AdmissionDecision.ADMITTED and admission.final_quantity == Decimal("1")
    finally:
        connection.close()


def test_the_admitted_quantity_is_the_gate_output_and_nothing_a_caller_passes(tmp_path):
    from regime_testkit import bound_regime_fields

    connection = build_scaling_policy_connection(shipped_map())
    fields = bound_regime_fields(0)  # normal: the gate returns 0.5 of the request
    try:
        admission = _admit(
            tmp_path, "gateonly", regime_id=0, policy_hash=fields["regime_policy_hash"],
            audit=fields["regime_audit"], connection=connection, risk_evidence={"scaled_size": "1"},
        )
        assert admission.decision is AdmissionDecision.ADMITTED and admission.final_quantity == Decimal("0.5")
    finally:
        connection.close()


def test_the_market_context_carries_the_policy_hash_and_audit_into_the_admission_kwargs():
    severity_map = shipped_map()

    async def build():
        events = _quotes(("100", "100.01", "100.02"))
        session, now = await _session(events)
        evidence = RegimeClassifier().evidence(session, INSTRUMENT, now=now)
        intent = _intent("ctx")
        return build_market_preflight_context(
            session, intent=intent, instrument=INSTRUMENT, regime=evidence, now=now
        ).execution_kwargs()

    import asyncio

    kwargs = asyncio.run(build())
    assert kwargs["regime_policy_hash"] == severity_map.policy_hash
    assert kwargs["regime_audit"]["policy_hash"] == severity_map.policy_hash
    assert kwargs["regime_audit"]["regime_id"] == kwargs["regime_id"] == 3


def test_regime_evidence_cannot_be_built_without_its_severity_and_version_fields():
    with pytest.raises(Exception):
        RegimeEvidence(
            instrument=INSTRUMENT, regime_id=3, observed_at=datetime.now(timezone.utc),
            model_version="m", source_snapshot_id="0" * 64,
        )


# --- feature calibration is NOT validated by this fix (characterisation) -----------------------


@pytest.mark.asyncio
async def test_runtime_features_are_intratick_log_return_volatility_and_bid_ask_spread(monkeypatch):
    """What the classifier feeds the model today.

    The model was trained on DAILY features: the rolling standard deviation of daily simple
    close-to-close returns, and the rolling mean of (high - low) / close
    (``backend/analytics_db.py`` ``calculate_and_ingest_features``). The runtime computes the
    population standard deviation of the window's log mid returns and the top-of-book
    (ask - bid) / mid. Both are ratios, but they are different quantities on different
    time scales. This test pins the runtime definition and the artifact scaler it uses; it
    does not claim the corrected labels are calibrated for it. Calibration needs separate
    evidence and is out of scope for the severity relabel.
    """

    import market_data.regime as regime_module

    seen = {}

    def spy(feature, **params):
        seen["feature"], seen["params"] = np.array(feature), params
        return np.array([0.0, 0.0, 0.0, 1.0])

    monkeypatch.setattr(regime_module, "fast_gmm_predict_proba", spy)
    events = _quotes(_swing(0.08), half_fraction="0.001")
    session, now = await _session(events)
    RegimeClassifier().evidence(session, INSTRUMENT, now=now)

    mids = np.array([(float(e.bid) + float(e.ask)) / 2.0 for e in events])
    expected_volatility = float(np.std(np.diff(np.log(mids))))
    snapshot = session.snapshot(INSTRUMENT, now=now)
    assert seen["feature"][0] == pytest.approx(expected_volatility)
    assert seen["feature"][1] == pytest.approx(float(snapshot.spread_pct))
    artifact = shipped_params()
    assert np.array_equal(seen["params"]["scaler_mean"], artifact["scaler_mean"])
    assert np.array_equal(seen["params"]["scaler_var"], artifact["scaler_var"])


def test_the_training_feature_definition_is_the_daily_one_in_the_analytics_sql():
    import inspect

    import analytics_db

    source = inspect.getsource(analytics_db.AnalyticsDB.calculate_and_ingest_features)
    assert "STDDEV(daily_return)" in source
    assert "(close - LAG(close) OVER" in source
    assert "((high - low) / NULLIF(close, 0))" in source
    assert "AVG(spread)" in source


@pytest.mark.asyncio
async def test_the_legacy_loop_substitutes_online_scaler_statistics_for_the_fitted_scaler(tmp_path):
    """Documented, not fixed: the legacy loop warm-starts Welford from the artifact scaler and then
    standardizes with its own running statistics, so its features drift from the fitted scale."""

    from backend.trading_loop import LiveTradingLoop

    class Manager:
        preloaded_weights = {0: 1, 1: 1, 2: 1}

        def swap_adapter(self, adapter_id):
            return True

    params = shipped_params()
    loop = LiveTradingLoop(Manager(), params, telemetry_db_path=str(tmp_path / "t.db"))
    price = 100.0
    for index in range(60):
        price *= 1.0005 if index % 2 else 0.9995
        loop.process_tick(price, price + 0.01, price - 0.01)
    assert loop.vol_standardizer.mean != pytest.approx(float(params["scaler_mean"][0]))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The artifact stores no record of the feature definition it was trained on, so runtime "
        "feature parity cannot be checked. Separate calibration evidence is required before the "
        "corrected regime labels are treated as validated risk policy."
    ),
)
def test_the_artifact_records_the_feature_definition_it_was_trained_on():
    with np.load(ARTIFACT) as artifact:
        assert "feature_definition" in artifact.files

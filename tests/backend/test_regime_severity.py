"""GMM regime severity ordering (Phase 66 GMM-REGIME-DEFECT fix).

A GMM component id is arbitrary. The shipped model numbers its components 0 normal,
1 crisis, 2 stressed, 3 calm, and the old hard-coded size table sized the calm
component at 5% and the crisis component at 50%. These tests pin the fix at the
level of the model and its policy: the ordering comes from the fitted means, every
behavioural consumer reads it, and anything that cannot be ordered refuses.
"""

from __future__ import annotations

import hashlib
import itertools
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pytest

from coreml.fast_gmm import fast_gmm_predict_proba
from execution import LocalPaperVenue, OrderSide, QuoteEvidence, RequotePolicy, evaluate_requote
from execution.requote import RequoteValidationError
from regime_testkit import ARTIFACT, ARTIFACT_SHA256, permuted_params, shipped_map, shipped_params
from simulation import RiskSwarmGate
from simulation import regime_severity as rs
from simulation.regime_severity import (
    REGIME_POLICY_TABLE,
    RegimeSeverityError,
    build_scaling_policy_connection,
    build_severity_map,
    params_sha256,
    policy_matches,
)
from simulation.requoter import AdaptiveReQuoter

PERMUTATIONS = list(itertools.permutations(range(4)))
SEMANTIC_BY_OLD_ID = {3: "calm", 0: "normal", 2: "stressed", 1: "crisis"}
SIZE_BY_OLD_ID = {3: 1.0, 0: 0.5, 2: 0.1, 1: 0.05}


# --- the shipped artifact ----------------------------------------------------------------------


def test_the_shipped_artifact_is_the_one_the_defect_note_describes_and_is_untouched():
    assert hashlib.sha256(ARTIFACT.read_bytes()).hexdigest() == ARTIFACT_SHA256


def test_the_shipped_artifact_order_is_calm_normal_stressed_crisis_ids_3_0_2_1():
    severity_map = shipped_map()
    assert severity_map.id_by_rank == (3, 0, 2, 1)
    assert severity_map.rank_by_id == (1, 3, 2, 0)
    assert severity_map.calm_id == 3
    assert {raw: severity_map.label(raw) for raw in range(4)} == SEMANTIC_BY_OLD_ID
    # The scores are the defect note's: means[k, 0] + means[k, 1], standardized coordinates.
    expected = {3: -0.567219, 0: 0.343374, 2: 5.913311, 1: 17.148014}
    for raw, score in expected.items():
        assert severity_map.scores[raw] == pytest.approx(score, abs=1e-6)


def test_the_shipped_size_table_by_raw_id_is_3_to_1_0_to_half_2_to_a_tenth_1_to_5_percent():
    assert shipped_map().size_multipliers_by_id() == SIZE_BY_OLD_ID


def test_the_policy_table_keeps_the_existing_schedule_per_severity_rank():
    sizes = [float(row.size_multiplier) for row in REGIME_POLICY_TABLE[4]]
    assert sizes == [1.0, 0.5, 0.1, 0.05]
    assert [row.label for row in REGIME_POLICY_TABLE[4]] == ["calm", "normal", "stressed", "crisis"]


@pytest.mark.parametrize("count", sorted(REGIME_POLICY_TABLE))
def test_every_configured_k_is_calm_at_full_size_and_never_sizes_a_worse_regime_larger(count):
    rows = REGIME_POLICY_TABLE[count]
    assert len(rows) == count
    assert rows[0].size_multiplier == Decimal("1.0")
    sizes = [row.size_multiplier for row in rows]
    collars = [row.collar_multiplier for row in rows]
    adapters = [row.adapter_id for row in rows]
    assert all(later < earlier for earlier, later in zip(sizes, sizes[1:])), "size falls with severity"
    assert all(later >= earlier for earlier, later in zip(collars, collars[1:])), "collar never narrows"
    assert adapters == sorted(adapters), "adapter slots follow severity"


# --- every component permutation keeps its meaning ---------------------------------------------


@pytest.mark.parametrize("order", PERMUTATIONS)
def test_a_renumbered_model_keeps_every_components_label_size_collar_and_adapter(order):
    """New id i is old component order[i]. Nothing about a component may follow its id."""

    base = shipped_map()
    renumbered = build_severity_map(permuted_params(order))
    for new_id, old_id in enumerate(order):
        assert renumbered.rank(new_id) == base.rank(old_id)
        assert renumbered.label(new_id) == base.label(old_id)
        assert renumbered.size_multiplier(new_id) == base.size_multiplier(old_id)
        assert renumbered.collar_multiplier(new_id) == base.collar_multiplier(old_id)
        assert renumbered.adapter_id(new_id, [0, 1, 2]) == base.adapter_id(old_id, [0, 1, 2])
    assert renumbered.calm_id == order.index(base.calm_id)


@pytest.mark.parametrize("order", PERMUTATIONS)
def test_a_renumbered_model_gives_the_same_posterior_leverage(order):
    """Posterior-weighted leverage is a property of the model, not of how it is numbered."""

    base_map, base_params = shipped_map(), shipped_params()
    renumbered_params = permuted_params(order)
    renumbered = build_severity_map(renumbered_params)
    for feature in ([0.0, 0.002], [0.03, 0.01], [0.09, 0.05], [0.25, 0.01], [0.5, 0.5], [2.4, 0.76]):
        x = np.array(feature)
        base_p = fast_gmm_predict_proba(x, **base_params)
        new_p = fast_gmm_predict_proba(x, **renumbered_params)
        base_leverage = sum(p * base_map.size_multiplier(k) for k, p in enumerate(base_p))
        new_leverage = sum(p * renumbered.size_multiplier(k) for k, p in enumerate(new_p))
        assert new_leverage == pytest.approx(base_leverage, rel=1e-9, abs=1e-12)


@pytest.mark.parametrize("order", PERMUTATIONS)
def test_a_renumbered_model_scales_the_same_through_the_sql_policy_and_the_risk_gate(order):
    gate = RiskSwarmGate()
    base = shipped_map()
    renumbered = build_severity_map(permuted_params(order))
    connection = build_scaling_policy_connection(renumbered)
    try:
        for new_id, old_id in enumerate(order):
            scaled = gate.evaluate(10.0, 100.0, new_id, 0.001, connection, policy_hash=renumbered.policy_hash)
            assert scaled == pytest.approx(100.0 * SIZE_BY_OLD_ID[old_id])
            assert scaled == pytest.approx(100.0 * base.size_multiplier(old_id))
    finally:
        connection.close()


@pytest.mark.parametrize("order", PERMUTATIONS)
def test_a_renumbered_model_gets_the_same_requote_collar_per_component(order):
    now = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)

    def limit_for(severity_map, raw_id):
        evidence = QuoteEvidence(
            bid=Decimal("10.00"), ask=Decimal("10.50"), volatility=Decimal("0.02"), cost=Decimal("0"),
            tick_size=Decimal("0.01"), regime_id=raw_id, observed_at=now, source="test",
        )
        policy = RequotePolicy.for_severity_map(severity_map)
        return evaluate_requote(
            side=OrderSide.BUY, evidence=evidence, policy=policy, venue=LocalPaperVenue(), now=now
        ).limit_price

    base = shipped_map()
    renumbered = build_severity_map(permuted_params(order))
    for new_id, old_id in enumerate(order):
        assert limit_for(renumbered, new_id) == limit_for(base, old_id)
    # The four collars are genuinely different, so the comparison above is not vacuous.
    assert len({limit_for(base, raw) for raw in range(4)}) == 4


class _StubManager:
    """Adapter manager with three preloaded slots that records every swap."""

    def __init__(self):
        self.preloaded_weights = {0: object(), 1: object(), 2: object()}
        self.swapped: list[int] = []

    def swap_adapter(self, adapter_id):
        self.swapped.append(adapter_id)
        return True


def _tick_stream():
    rng = np.random.default_rng(7)
    price = 100.0
    ticks = []
    for index in range(240):
        scale = 0.0005 if index < 80 else (0.01 if index < 160 else 0.002)
        price *= float(np.exp(rng.normal(0.0, scale)))
        half = price * (0.0004 if index < 120 else 0.002)
        ticks.append((price, price + half, price - half))
    return ticks


@pytest.mark.parametrize("order", PERMUTATIONS)
def test_a_renumbered_model_drives_the_legacy_loop_to_the_same_leverage_and_adapters(order, tmp_path):
    from backend.trading_loop import LiveTradingLoop

    def run(params, name):
        manager = _StubManager()
        loop = LiveTradingLoop(manager, params, telemetry_db_path=str(tmp_path / f"{name}.db"))
        leverage = [loop.process_tick(*tick)["risk_leverage_coefficient"] for tick in _tick_stream()]
        return leverage, manager.swapped, loop

    base_leverage, base_swaps, _ = run(shipped_params(), "base")
    new_leverage, new_swaps, loop = run(permuted_params(order), "perm")
    assert new_leverage == pytest.approx(base_leverage, rel=1e-9, abs=1e-12)
    assert new_swaps == base_swaps
    assert loop.severity_map.id_by_rank == tuple(order.index(old) for old in (3, 0, 2, 1))


# --- fail closed -------------------------------------------------------------------------------


def _params(scores, *, weights=None):
    """Synthetic model: K components whose mean volatility and spread sum to the given scores."""

    count = len(scores)
    means = np.array([[score / 2.0, score / 2.0] for score in scores])
    return {
        "weights": np.full(count, 1.0 / count) if weights is None else np.asarray(weights, dtype=float),
        "means": means,
        "precisions_cholesky": np.tile(np.eye(2), (count, 1, 1)),
        "scaler_mean": np.array([0.1, 0.1]),
        "scaler_var": np.array([0.05, 0.01]),
    }


def _refusal(params):
    with pytest.raises(RegimeSeverityError) as error:
        build_severity_map(params)
    return error.value.code


@pytest.mark.parametrize("count", [1, 5, 6])
def test_an_unsupported_component_count_refuses(count):
    assert _refusal(_params(list(range(count)))) == "REGIME_K_UNSUPPORTED"


@pytest.mark.parametrize("scores,expected_ids", [([5.0, -1.0], (1, 0)), ([2.0, 9.0, 0.5], (2, 0, 1))])
def test_k_2_and_k_3_are_configured_explicitly_and_ordered_by_score(scores, expected_ids):
    severity_map = build_severity_map(_params(scores))
    assert severity_map.id_by_rank == expected_ids
    sizes = [severity_map.size_multiplier(raw) for raw in expected_ids]
    assert sizes[0] == 1.0 and sizes == sorted(sizes, reverse=True)
    assert severity_map.label(expected_ids[0]) == "calm"
    assert severity_map.label(expected_ids[-1]) in {"crisis", "stressed"}
    assert {len(REGIME_POLICY_TABLE[len(scores)])} == {len(scores)}


def test_k_2_and_k_3_values_are_the_documented_conservative_policy():
    assert [(r.label, str(r.size_multiplier)) for r in REGIME_POLICY_TABLE[3]] == [
        ("calm", "1.0"), ("stressed", "0.1"), ("crisis", "0.05"),
    ]
    assert [(r.label, str(r.size_multiplier)) for r in REGIME_POLICY_TABLE[2]] == [
        ("calm", "1.0"), ("stressed", "0.05"),
    ]


@pytest.mark.parametrize(
    "scores",
    [
        [1.0, 1.0, 5.0, 9.0],
        [1.0, 1.0 + 1e-12, 5.0, 9.0],
        [9.0, 5.0, 2.0, 2.0],
        [0.0, 0.0, 0.0, 0.0],
    ],
)
def test_tied_severity_scores_refuse_and_are_never_broken_by_raw_id(scores):
    assert _refusal(_params(scores)) == "REGIME_SEVERITY_AMBIGUOUS"


def test_a_tie_in_the_sum_of_two_different_features_still_refuses():
    params = _params([0.0, 4.0, 8.0, 12.0])
    params["means"] = np.array([[0.0, 4.0], [4.0, 0.0], [1.0, 7.0], [6.0, 6.0]])
    # Scores are 4, 4, 8, 12: the first two differ in both features and still tie.
    assert _refusal(params) == "REGIME_SEVERITY_AMBIGUOUS"


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda p: p.update(precisions_cholesky=np.zeros((4, 2, 2))), "REGIME_COVARIANCE_INVALID"),
        (lambda p: p.update(precisions_cholesky=np.full((4, 2, 2), np.nan)), "REGIME_COVARIANCE_INVALID"),
        (lambda p: p.update(precisions_cholesky=np.tile(np.eye(2)[:, :1], (4, 1, 1))), "REGIME_COVARIANCE_INVALID"),
        (lambda p: p.update(precisions_cholesky=np.tile(np.diag([1e-8, 1.0]), (4, 1, 1))), "REGIME_COVARIANCE_INVALID"),
        (lambda p: p.update(precisions_cholesky=np.tile(np.array([[1.0, 2.0], [2.0, 4.0]]), (4, 1, 1))), "REGIME_COVARIANCE_INVALID"),
        (lambda p: p.update(scaler_var=np.array([0.0, 0.01])), "REGIME_SCALER_INVALID"),
        (lambda p: p.update(scaler_var=np.array([-0.05, 0.01])), "REGIME_SCALER_INVALID"),
        (lambda p: p.update(scaler_var=np.array([np.nan, 0.01])), "REGIME_SCALER_INVALID"),
        (lambda p: p.update(scaler_var=np.array([0.05, 0.01, 0.2])), "REGIME_SCALER_INVALID"),
        (lambda p: p.update(scaler_mean=np.array([np.inf, 0.1])), "REGIME_SCALER_INVALID"),
        (lambda p: p.update(scaler_mean=np.array([0.1])), "REGIME_SCALER_INVALID"),
        (lambda p: p.update(weights=np.array([0.5, 0.5, 0.5, -0.5])), "REGIME_MODEL_INVALID"),
        (lambda p: p.update(weights=np.array([0.1, 0.1, 0.1, 0.1])), "REGIME_MODEL_INVALID"),
        (lambda p: p.update(weights=np.array([])), "REGIME_MODEL_INVALID"),
        (lambda p: p.update(means=np.zeros((4, 3))), "REGIME_MODEL_INVALID"),
        (lambda p: p.update(means=np.full((4, 2), np.inf)), "REGIME_MODEL_INVALID"),
        (lambda p: p.pop("scaler_mean"), "REGIME_SCALER_INVALID"),
        (lambda p: p.pop("means"), "REGIME_MODEL_INVALID"),
    ],
)
def test_an_invalid_covariance_scaler_or_model_refuses(mutate, code):
    params = shipped_params()
    mutate(params)
    assert _refusal(params) == code


@pytest.mark.parametrize("bad", [-1, 4, 99, True, False, None, "1", 1.0, 2.5])
def test_an_unknown_or_malformed_regime_id_raises_on_every_lookup(bad):
    severity_map = shipped_map()
    for lookup in (
        severity_map.rank, severity_map.label, severity_map.size_multiplier,
        severity_map.collar_multiplier, severity_map.adapter_id,
    ):
        with pytest.raises(RegimeSeverityError) as error:
            lookup(bad)
        assert error.value.code == "REGIME_ID_UNKNOWN"


def test_numpy_integer_ids_from_argmax_are_known_ids():
    assert shipped_map().rank(np.int64(3)) == 0


def test_an_unknown_id_finds_no_row_in_the_sql_policy_and_the_gate_blocks_it():
    connection = build_scaling_policy_connection(shipped_map())
    try:
        for unknown in (4, -1, 99):
            assert RiskSwarmGate().evaluate(10.0, 100.0, unknown, 0.001, connection) == 0.0
    finally:
        connection.close()


# --- the map and policy are versioned and hashed -----------------------------------------------


def test_the_audit_keeps_the_raw_id_and_carries_the_versions_and_hash():
    severity_map = shipped_map()
    audit = severity_map.audit(3)
    assert audit["regime_id"] == 3 and audit["severity_rank"] == 0 and audit["severity_label"] == "calm"
    assert audit["policy_hash"] == severity_map.policy_hash and len(severity_map.policy_hash) == 64
    assert audit["mapping_version"] == rs.SEVERITY_MAPPING_VERSION
    assert audit["policy_version"] == rs.REGIME_POLICY_VERSION
    assert audit["params_sha256"] == params_sha256(shipped_params())


def test_the_policy_hash_is_deterministic_and_changes_with_the_model_the_order_and_the_table(monkeypatch):
    base = shipped_map().policy_hash
    assert build_severity_map(shipped_params()).policy_hash == base
    assert build_severity_map(permuted_params((1, 0, 2, 3))).policy_hash != base
    changed = shipped_params()
    changed["scaler_mean"] = changed["scaler_mean"] + 1e-6
    assert build_severity_map(changed).policy_hash != base

    softer = dict(REGIME_POLICY_TABLE)
    rows = list(REGIME_POLICY_TABLE[4])
    rows[3] = rs.RankPolicy("crisis", Decimal("0.5"), Decimal("3.0"), 2)
    softer[4] = tuple(rows)
    monkeypatch.setattr(rs, "REGIME_POLICY_TABLE", softer)
    assert build_severity_map(shipped_params()).policy_hash != base


def test_a_policy_table_is_only_accepted_when_its_hash_matches():
    severity_map = shipped_map()
    other = build_severity_map(permuted_params((1, 0, 2, 3)))
    connection = build_scaling_policy_connection(severity_map)
    handmade = sqlite3.connect(":memory:")
    handmade.execute("CREATE TABLE scaling_policies (regime_id INTEGER PRIMARY KEY, scale_multiplier REAL NOT NULL)")
    handmade.executemany("INSERT INTO scaling_policies VALUES (?, ?)", ((0, 1.0), (1, 0.5), (2, 0.1), (3, 0.05)))
    try:
        assert policy_matches(connection, severity_map.policy_hash)
        assert not policy_matches(connection, other.policy_hash)
        assert not policy_matches(connection, "")
        assert not policy_matches(connection, None)
        assert not policy_matches(handmade, severity_map.policy_hash), "a raw-keyed table has no policy hash"
        assert not policy_matches(None, severity_map.policy_hash)
    finally:
        connection.close()
        handmade.close()


def test_the_risk_gate_refuses_a_table_built_from_another_models_ordering():
    gate = RiskSwarmGate()
    severity_map = shipped_map()
    other = build_severity_map(permuted_params((1, 0, 2, 3)))
    wrong_table = build_scaling_policy_connection(other)
    right_table = build_scaling_policy_connection(severity_map)
    try:
        assert gate.evaluate(10.0, 100.0, 3, 0.001, right_table, policy_hash=severity_map.policy_hash) == 100.0
        assert gate.evaluate(10.0, 100.0, 3, 0.001, wrong_table, policy_hash=severity_map.policy_hash) == 0.0
        # Without a hash the gate behaves as before: it trusts the table it is handed.
        assert gate.evaluate(10.0, 100.0, 3, 0.001, wrong_table) > 0.0
    finally:
        wrong_table.close()
        right_table.close()


def test_the_five_percent_spread_block_survives_in_the_gate():
    gate = RiskSwarmGate()
    severity_map = shipped_map()
    connection = build_scaling_policy_connection(severity_map)
    try:
        calm = severity_map.calm_id
        assert gate.evaluate(10.0, 100.0, calm, 0.05, connection, policy_hash=severity_map.policy_hash) == 100.0
        assert gate.evaluate(10.0, 100.0, calm, 0.0501, connection, policy_hash=severity_map.policy_hash) == 0.0
        assert gate.evaluate(10.0, 100.0, calm, 0.001, None, policy_hash=severity_map.policy_hash) == 0.0
    finally:
        connection.close()


# --- the legacy loop and the adapter re-quoter -------------------------------------------------


def test_the_legacy_loop_refuses_a_model_it_cannot_order(tmp_path):
    from backend.trading_loop import LiveTradingLoop

    tied = shipped_params()
    tied["means"] = tied["means"].copy()
    tied["means"][0] = tied["means"][3]
    # trading_loop imports the package-style module, so the class is not this module's
    # flat-imported twin (the repo supports both import styles). The code is the contract.
    with pytest.raises(Exception) as error:
        LiveTradingLoop(_StubManager(), tied, telemetry_db_path=str(tmp_path / "t.db"))
    assert type(error.value).__name__ == "RegimeSeverityError"
    assert error.value.code == "REGIME_SEVERITY_AMBIGUOUS"


def test_the_legacy_loop_leverage_override_is_by_severity_rank_and_must_be_complete(tmp_path):
    from backend.trading_loop import LiveTradingLoop

    loop = LiveTradingLoop(
        _StubManager(), shipped_params(), telemetry_db_path=str(tmp_path / "t.db"),
        leverage_by_severity_rank={0: 0.8, 1: 0.4, 2: 0.2, 3: 0.01},
    )
    assert loop.leverage_coefficients == {3: 0.8, 0: 0.4, 2: 0.2, 1: 0.01}
    with pytest.raises(ValueError):
        LiveTradingLoop(
            _StubManager(), shipped_params(), telemetry_db_path=str(tmp_path / "u.db"),
            leverage_by_severity_rank={0: 1.0, 1: 0.5},
        )
    with pytest.raises(TypeError):
        LiveTradingLoop(  # the old raw-keyed parameter is gone, not silently reinterpreted
            _StubManager(), shipped_params(), telemetry_db_path=str(tmp_path / "v.db"),
            leverage_coefficients={0: 1.0, 1: 0.5, 2: 0.1, 3: 0.05},
        )


@pytest.mark.asyncio
async def test_the_legacy_loop_blocks_an_order_before_any_regime_is_classified(tmp_path):
    from backend.trading_loop import LiveTradingLoop

    loop = LiveTradingLoop(_StubManager(), shipped_params(), telemetry_db_path=str(tmp_path / "t.db"))
    connection = build_scaling_policy_connection(loop.severity_map)
    dispatched = []

    async def dispatch(size, price):
        dispatched.append(size)
        return {"actual_fill_price": price}

    window = {"bid": [99.9] * 5, "ask": [100.1] * 5, "spread": [0.002] * 5}
    try:
        assert loop.current_regime == -1
        decision = await loop.execute_order_pre_flight(
            "BUY", 10.0, window, {"equity": 1000.0, "peak_equity": 1000.0}, connection, dispatch
        )
        assert decision.approved is False and decision.scaled_size == 0.0
        assert dispatched == []
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_the_legacy_loop_sizes_the_calm_component_at_full_size_and_the_crisis_one_at_five_percent(tmp_path):
    from backend.trading_loop import LiveTradingLoop

    loop = LiveTradingLoop(_StubManager(), shipped_params(), telemetry_db_path=str(tmp_path / "t.db"))
    connection = build_scaling_policy_connection(loop.severity_map)
    window = {"bid": [99.9] * 5, "ask": [100.1] * 5, "spread": [0.002] * 5}
    sizes = {}
    try:
        for raw_id in (3, 0, 2, 1):
            loop.current_regime = raw_id

            async def dispatch(size, price):
                return {"actual_fill_price": price}

            decision = await loop.execute_order_pre_flight(
                "BUY", 100.0, window, {"equity": 1000.0, "peak_equity": 1000.0}, connection, dispatch
            )
            sizes[raw_id] = decision.scaled_size
    finally:
        connection.close()
    assert sizes == pytest.approx({3: 100.0, 0: 50.0, 2: 10.0, 1: 5.0})


def test_the_requoter_reads_raw_ids_only_through_the_severity_map():
    severity_map = shipped_map()
    quoter = AdaptiveReQuoter(None, None, 3, severity_map=severity_map)
    assert {raw: quoter.get_regime_multiplier(raw) for raw in range(4)} == {3: 1.0, 0: 1.5, 2: 2.0, 1: 3.0}
    # Legacy string labels are untouched.
    assert quoter.get_regime_multiplier("extreme") == 3.0
    assert quoter.get_regime_multiplier("something-else") == 1.5
    for bad in (-1, 4):
        with pytest.raises(Exception) as unknown:
            quoter.get_regime_multiplier(bad)
        assert unknown.value.code == "REGIME_ID_UNKNOWN"
    with pytest.raises(Exception) as error:
        AdaptiveReQuoter(None, None, 3).get_regime_multiplier(3)
    assert error.value.code == "REGIME_SEVERITY_MAP_REQUIRED"


def test_the_requote_policy_has_no_default_ladder_so_an_unmapped_regime_denies():
    now = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
    evidence = QuoteEvidence(
        bid=Decimal("10.00"), ask=Decimal("10.50"), volatility=Decimal("0.02"), cost=Decimal("0"),
        tick_size=Decimal("0.01"), regime_id=3, observed_at=now, source="test",
    )
    for raw_id in range(4):  # the old ladder {0, 1, 2} treated raw ids as severities
        with pytest.raises(RequoteValidationError, match="regime"):
            evaluate_requote(
                side=OrderSide.BUY, evidence=QuoteEvidence(**{**evidence.__dict__, "regime_id": raw_id}),
                policy=RequotePolicy(), venue=LocalPaperVenue(), now=now,
            )
    mapped = RequotePolicy.for_severity_map(shipped_map())
    assert evaluate_requote(
        side=OrderSide.BUY, evidence=evidence, policy=mapped, venue=LocalPaperVenue(), now=now
    ).policy_version.endswith(shipped_map().policy_hash[:16])
    unknown = QuoteEvidence(**{**evidence.__dict__, "regime_id": 9})
    with pytest.raises(RequoteValidationError, match="regime"):
        evaluate_requote(side=OrderSide.BUY, evidence=unknown, policy=mapped, venue=LocalPaperVenue(), now=now)

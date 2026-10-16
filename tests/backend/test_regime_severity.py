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
import math
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
    policy_table_matches,
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
    # A wider BUY collar in the worst regime is the wrong direction: the most severe rank
    # of every K shares the widest collar, 2.0, and never goes above it.
    assert rows[-1].collar_multiplier == Decimal("2.0")
    assert max(row.collar_multiplier for row in rows) == Decimal("2.0")
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
            scaled = gate.evaluate(10.0, 100.0, new_id, 0.001, connection, severity_map=renumbered)
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
    # Calm, normal and the two most severe components give three different collars (crisis
    # shares the widest value, 2.0), so the comparison above is not vacuous.
    assert len({limit_for(base, raw) for raw in range(4)}) == 3
    assert limit_for(base, 2) == limit_for(base, 1) > limit_for(base, 0) > limit_for(base, 3)


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
    rows[3] = rs.RankPolicy("crisis", Decimal("0.5"), Decimal("2.0"), 2)
    softer[4] = tuple(rows)
    monkeypatch.setattr(rs, "REGIME_POLICY_TABLE", softer)
    assert build_severity_map(shipped_params()).policy_hash != base


def test_a_policy_table_is_only_accepted_when_its_actual_rows_match_the_trusted_map():
    severity_map = shipped_map()
    other = build_severity_map(permuted_params((1, 0, 2, 3)))
    connection = build_scaling_policy_connection(severity_map)
    handmade = sqlite3.connect(":memory:")
    handmade.execute("CREATE TABLE scaling_policies (regime_id INTEGER PRIMARY KEY, scale_multiplier REAL NOT NULL)")
    handmade.executemany("INSERT INTO scaling_policies VALUES (?, ?)", ((0, 1.0), (1, 0.5), (2, 0.1), (3, 0.05)))
    try:
        assert policy_table_matches(connection, severity_map)
        assert not policy_table_matches(connection, other)
        assert not policy_table_matches(handmade, severity_map), "a raw-keyed table has no policy meta"
        assert not policy_table_matches(None, severity_map)
    finally:
        connection.close()
        handmade.close()


def _tampered(severity_map, sql, args=()):
    connection = build_scaling_policy_connection(severity_map)
    connection.execute(sql, args)
    return connection


@pytest.mark.parametrize(
    "sql,args",
    [
        # The crisis row (raw id 1) raised from 0.05 to full size, hash row untouched.
        ("UPDATE scaling_policies SET scale_multiplier = 1.0 WHERE regime_id = 1", ()),
        ("UPDATE scaling_policies SET scale_multiplier = 0.51 WHERE regime_id = 0", ()),
        ("UPDATE scaling_policies SET scale_multiplier = 0.0 WHERE regime_id = 3", ()),
        ("DELETE FROM scaling_policies WHERE regime_id = 2", ()),
        ("INSERT INTO scaling_policies VALUES (4, 1.0)", ()),
        ("UPDATE scaling_policies SET regime_id = 7 WHERE regime_id = 2", ()),
        ("INSERT INTO scaling_policy_meta VALUES ('extra')", ()),
        ("UPDATE scaling_policy_meta SET policy_hash = 'f'", ()),
        ("DELETE FROM scaling_policy_meta", ()),
    ],
)
def test_a_tampered_policy_table_is_refused_by_content_even_when_the_hash_row_matches(sql, args):
    severity_map = shipped_map()
    connection = _tampered(severity_map, sql, args)
    try:
        assert not policy_table_matches(connection, severity_map)
        # The risk gate refuses it too, so a full-size crisis fill is not possible.
        assert RiskSwarmGate().evaluate(10.0, 100.0, 1, 0.001, connection, severity_map=severity_map) == 0.0
    finally:
        connection.close()


def test_the_tampered_crisis_row_would_otherwise_admit_full_size():
    """The risk gate without the severity map trusts the rows: this is the hole C2 closes."""

    severity_map = shipped_map()
    connection = _tampered(severity_map, "UPDATE scaling_policies SET scale_multiplier = 1.0 WHERE regime_id = 1")
    try:
        assert RiskSwarmGate().evaluate(10.0, 100.0, 1, 0.001, connection) == 100.0
    finally:
        connection.close()


def test_the_risk_gate_refuses_a_table_built_from_another_models_ordering():
    gate = RiskSwarmGate()
    severity_map = shipped_map()
    other = build_severity_map(permuted_params((1, 0, 2, 3)))
    wrong_table = build_scaling_policy_connection(other)
    right_table = build_scaling_policy_connection(severity_map)
    try:
        assert gate.evaluate(10.0, 100.0, 3, 0.001, right_table, severity_map=severity_map) == 100.0
        assert gate.evaluate(10.0, 100.0, 3, 0.001, wrong_table, severity_map=severity_map) == 0.0
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
        assert gate.evaluate(10.0, 100.0, calm, 0.05, connection, severity_map=severity_map) == 100.0
        assert gate.evaluate(10.0, 100.0, calm, 0.0501, connection, severity_map=severity_map) == 0.0
        assert gate.evaluate(10.0, 100.0, calm, 0.001, None, severity_map=severity_map) == 0.0
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
    assert {raw: quoter.get_regime_multiplier(raw) for raw in range(4)} == {3: 1.0, 0: 1.5, 2: 2.0, 1: 2.0}
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


# --- C1: factor structure and posterior validity -----------------------------------------------


def _with_factors(transform):
    params = shipped_params()
    params["precisions_cholesky"] = transform(params["precisions_cholesky"].copy())
    return params


def _negate_one(factors):
    factors[0] = -factors[0]
    return factors


def _zero_one_diagonal(factors):
    factors[2, 1, 1] = 0.0
    return factors


def _negative_one_diagonal(factors):
    factors[1, 0, 0] = -abs(factors[1, 0, 0])
    return factors


@pytest.mark.parametrize(
    "transform",
    [
        lambda f: -f,  # every factor negated: L @ L.T is unchanged, the posterior is NaN
        _negate_one,
        lambda f: np.swapaxes(f, 1, 2).copy(),  # lower triangular instead of upper
        _zero_one_diagonal,
        _negative_one_diagonal,
        lambda f: f + np.tril(np.full((2, 2), 1e-3), -1),  # a stray entry below the diagonal
    ],
    ids=["all-negated", "one-negated", "lower-triangular", "zero-diagonal", "negative-diagonal", "below-diagonal-entry"],
)
def test_a_precision_factor_that_is_not_upper_triangular_with_a_positive_diagonal_refuses(transform):
    assert _refusal(_with_factors(transform)) == "REGIME_COVARIANCE_INVALID"


@pytest.mark.parametrize("order", [(3, 0, 2, 1), (0, 1, 2, 3), (2, 3, 1, 0)])
def test_a_renumbered_model_with_negated_factors_cannot_be_loaded_by_the_legacy_loop(order, tmp_path):
    """Codex's repro: negated factors gave a NaN posterior whose argmax picked component 0."""

    from backend.trading_loop import LiveTradingLoop

    params = permuted_params(order)
    params["precisions_cholesky"] = -params["precisions_cholesky"]
    with pytest.raises(Exception) as error:
        LiveTradingLoop(_StubManager(), params, telemetry_db_path=str(tmp_path / "t.db"))
    assert error.value.code == "REGIME_COVARIANCE_INVALID"


BAD_POSTERIORS = [
    pytest.param(np.array([np.nan, np.nan, np.nan, np.nan]), id="nan"),
    pytest.param(np.array([0.25, 0.25, np.inf, 0.25]), id="inf"),
    pytest.param(np.array([0.5, 0.5, 0.5, 0.5]), id="sums-to-two"),
    pytest.param(np.array([0.0, 0.0, 0.0, 0.0]), id="all-zero"),
    pytest.param(np.array([1.2, -0.2, 0.0, 0.0]), id="negative-entry"),
    pytest.param(np.array([1.0, 0.0, 0.0]), id="too-short"),
    pytest.param(np.array([[1.0, 0.0, 0.0, 0.0]]), id="two-dimensional"),
]


@pytest.mark.parametrize("posterior", BAD_POSTERIORS)
def test_an_invalid_posterior_is_refused_before_any_argmax(posterior):
    with pytest.raises(RegimeSeverityError) as error:
        rs.validate_posterior(posterior, 4)
    assert error.value.code == "REGIME_POSTERIOR_INVALID"


def test_a_valid_posterior_is_accepted_unchanged():
    vector = np.array([0.1, 0.2, 0.3, 0.4])
    assert np.array_equal(rs.validate_posterior(vector, 4), vector)


@pytest.mark.parametrize("posterior", BAD_POSTERIORS)
@pytest.mark.asyncio
async def test_the_legacy_loop_invalidates_the_regime_on_a_bad_posterior_and_blocks_orders(
    posterior, tmp_path, monkeypatch
):
    import backend.trading_loop as loop_module
    from backend.trading_loop import LiveTradingLoop

    manager = _StubManager()
    loop = LiveTradingLoop(manager, shipped_params(), telemetry_db_path=str(tmp_path / "t.db"))
    for tick in _tick_stream()[:40]:
        loop.process_tick(*tick)
    assert loop.current_regime != -1 and loop.risk_leverage_coefficient > 0.0
    swaps_before = list(manager.swapped)

    monkeypatch.setattr(loop_module, "fast_gmm_predict_proba", lambda *args, **kwargs: posterior)
    result = loop.process_tick(*_tick_stream()[41])
    assert result["regime_valid"] is False and result["dominant_regime"] == -1
    assert loop.current_regime == -1 and loop.requoter.current_regime == -1
    assert loop.risk_leverage_coefficient == 0.0 and result["risk_leverage_coefficient"] == 0.0
    assert manager.swapped == swaps_before, "no adapter swap on an invalid posterior"

    connection = build_scaling_policy_connection(loop.severity_map)
    dispatched = []

    async def dispatch(size, price):
        dispatched.append(size)
        return {"actual_fill_price": price}

    try:
        decision = await loop.execute_order_pre_flight(
            "BUY", 10.0, {"bid": [99.9] * 5, "ask": [100.1] * 5, "spread": [0.002] * 5},
            {"equity": 1000.0, "peak_equity": 1000.0}, connection, dispatch,
        )
    finally:
        connection.close()
    assert decision.approved is False and dispatched == []


def test_the_requoter_accepts_a_numpy_integer_id_and_refuses_other_non_labels():
    quoter = AdaptiveReQuoter(None, None, 3, severity_map=shipped_map())
    assert quoter.get_regime_multiplier(np.int64(3)) == 1.0
    assert quoter.get_regime_multiplier(np.int32(1)) == 2.0
    for bad in (None, 1.0, True, 2.5):
        with pytest.raises(Exception) as error:
            quoter.get_regime_multiplier(bad)
        assert error.value.code == "REGIME_ID_UNKNOWN"


class _SwitchableManager(_StubManager):
    """Adapter manager whose swap and slot lookup can be made to fail, return None or raise."""

    def __init__(self):
        super().__init__()
        self.mode = "ok"

    @property
    def preloaded_weights(self):
        if self.mode == "slots_unreadable":
            raise RuntimeError("adapter slots are unreadable")
        if self.mode == "slots_unorderable":
            return {"not-a-slot-number": object(), 1: object()}
        return self._slots

    @preloaded_weights.setter
    def preloaded_weights(self, value):
        self._slots = value

    def swap_adapter(self, adapter_id):
        if self.mode == "raise":
            raise RuntimeError("adapter swap blew up")
        if self.mode == "none":
            return None  # a swap that reports nothing is not a swap that worked
        self.swapped.append(adapter_id)
        return self.mode == "ok"


_LOOKUP_FAILURES = ["slots_unreadable", "slots_unorderable"]


@pytest.mark.parametrize(
    "mode",
    ["failed_swap", "swap_raises", "swap_returns_none", "inference_raises", *_LOOKUP_FAILURES],
)
@pytest.mark.asyncio
async def test_a_failed_swap_or_inference_error_invalidates_the_regime_until_the_next_good_classification(
    mode, tmp_path, monkeypatch
):
    """P1-c: a crisis posterior with a failed swap used to leave the previous calm regime in
    place and dispatch 10 of 10. Any inference, adapter-lookup or adapter failure now
    invalidates the regime, including a swap that returns None and a slot lookup that raises
    after a good posterior (66 follow-up: those lookups sat outside the try)."""

    import backend.trading_loop as loop_module
    from backend.trading_loop import LiveTradingLoop

    real_predict = loop_module.fast_gmm_predict_proba
    manager = _SwitchableManager()
    loop = LiveTradingLoop(manager, shipped_params(), telemetry_db_path=str(tmp_path / "t.db"))
    ticks = _tick_stream()
    for tick in ticks[:40]:
        loop.process_tick(*tick)
    previous = loop.current_regime
    assert previous != -1 and loop.risk_leverage_coefficient > 0.0

    crisis_id = shipped_map().id_by_rank[3]
    assert previous != crisis_id
    swap_modes = {"failed_swap": "fail", "swap_raises": "raise", "swap_returns_none": "none"}
    if mode in swap_modes:
        manager.mode = swap_modes[mode]
        monkeypatch.setattr(loop_module, "fast_gmm_predict_proba", lambda *a, **k: np.eye(4)[crisis_id])
    elif mode in _LOOKUP_FAILURES:
        # The posterior stays valid and the regime unchanged: only the slot lookup fails.
        manager.mode = mode
    else:
        def boom(*args, **kwargs):
            raise RuntimeError("inference failed")

        monkeypatch.setattr(loop_module, "fast_gmm_predict_proba", boom)

    result = loop.process_tick(*ticks[41])  # must not raise
    assert result["regime_valid"] is False and result["dominant_regime"] == -1
    assert loop.current_regime == -1 and loop.requoter.current_regime == -1
    assert loop.risk_leverage_coefficient == 0.0 and result["risk_leverage_coefficient"] == 0.0

    connection = build_scaling_policy_connection(loop.severity_map)
    dispatched = []

    async def dispatch(size, price):
        dispatched.append(size)
        return {"actual_fill_price": price}

    window = {"bid": [99.9] * 5, "ask": [100.1] * 5, "spread": [0.002] * 5}
    try:
        decision = await loop.execute_order_pre_flight(
            "BUY", 10.0, window, {"equity": 1000.0, "peak_equity": 1000.0}, connection, dispatch
        )
        assert decision.approved is False and dispatched == []

        # The next successful classification restores it.
        manager.mode = "ok"
        monkeypatch.setattr(loop_module, "fast_gmm_predict_proba", real_predict)
        for tick in ticks[42:46]:
            loop.process_tick(*tick)
        assert loop.current_regime != -1 and loop.risk_leverage_coefficient > 0.0
        decision = await loop.execute_order_pre_flight(
            "BUY", 10.0, window, {"equity": 1000.0, "peak_equity": 1000.0}, connection, dispatch
        )
        assert decision.approved is True and dispatched
    finally:
        connection.close()


async def _preflight_buy(loop):
    """Run one BUY pre-flight against the loop's current regime; returns (decision, dispatched)."""

    connection = build_scaling_policy_connection(loop.severity_map)
    dispatched = []

    async def dispatch(size, price):
        dispatched.append(size)
        return {"actual_fill_price": price}

    try:
        decision = await loop.execute_order_pre_flight(
            "BUY", 10.0, {"bid": [99.9] * 5, "ask": [100.1] * 5, "spread": [0.002] * 5},
            {"equity": 1000.0, "peak_equity": 1000.0}, connection, dispatch,
        )
    finally:
        connection.close()
    return decision, dispatched


@pytest.mark.parametrize(
    "bad_tick",
    [(None, 100.1, 99.9), (100.0, None, 99.9), (100.0, 100.1, None), (100.0, "n/a", 99.9)],
    ids=["no_mid", "no_ask", "no_bid", "unparseable_ask"],
)
@pytest.mark.asyncio
async def test_a_tick_that_breaks_the_feature_update_invalidates_a_warm_regime(bad_tick, tmp_path):
    """Codex r1 P1: the feature updates sat outside the try. After a calm regime had warmed up,
    ``process_tick(None, 100.1, 99.9)`` raised and the regime stayed at 3 with leverage 0.98,
    so the next pre-flight approved and dispatched 10 of 10. Any failure now invalidates it."""

    from backend.trading_loop import LiveTradingLoop

    manager = _StubManager()
    loop = LiveTradingLoop(manager, shipped_params(), telemetry_db_path=str(tmp_path / "t.db"))
    ticks = _tick_stream()
    for tick in ticks[:40]:
        loop.process_tick(*tick)
    assert loop.current_regime != -1 and loop.risk_leverage_coefficient > 0.0
    decision, dispatched = await _preflight_buy(loop)
    assert decision.approved is True and dispatched, "control: the warm regime sizes and dispatches"

    result = loop.process_tick(*bad_tick)  # must not raise
    assert result["regime_valid"] is False and result["dominant_regime"] == -1
    assert loop.current_regime == -1 and loop.requoter.current_regime == -1
    assert loop.risk_leverage_coefficient == 0.0 and result["risk_leverage_coefficient"] == 0.0

    decision, dispatched = await _preflight_buy(loop)
    assert decision.approved is False and decision.regime_id == -1 and decision.scaled_size == 0.0
    assert dispatched == [], "a stale regime must not size or dispatch the order"

    # The next good tick classifies again.
    for tick in ticks[41:46]:
        loop.process_tick(*tick)
    assert loop.current_regime != -1 and loop.risk_leverage_coefficient > 0.0


class _HostileCodeError(Exception):
    """An exception whose ``code`` property itself raises when read."""

    @property
    def code(self):
        raise RuntimeError("code is unreadable")


@pytest.mark.asyncio
async def test_an_exception_whose_code_property_raises_still_invalidates_the_regime(tmp_path, monkeypatch):
    """Codex r1 P2: the handler read ``exc.code`` before invalidating, so a hostile ``code``
    escaped it and the stale regime stayed usable. Invalidation now comes first."""

    import backend.trading_loop as loop_module
    from backend.trading_loop import LiveTradingLoop

    manager = _StubManager()
    loop = LiveTradingLoop(manager, shipped_params(), telemetry_db_path=str(tmp_path / "t.db"))
    ticks = _tick_stream()
    for tick in ticks[:40]:
        loop.process_tick(*tick)
    assert loop.current_regime != -1 and loop.risk_leverage_coefficient > 0.0

    def hostile(*args, **kwargs):
        raise _HostileCodeError("inference failed")

    monkeypatch.setattr(loop_module, "fast_gmm_predict_proba", hostile)
    result = loop.process_tick(*ticks[41])  # must not raise
    assert result["regime_valid"] is False and result["dominant_regime"] == -1
    assert result["invalid_reason"] == "_HostileCodeError"
    assert loop.current_regime == -1 and loop.requoter.current_regime == -1
    assert loop.risk_leverage_coefficient == 0.0

    decision, dispatched = await _preflight_buy(loop)
    assert decision.approved is False and dispatched == []


def _bad_value_tick(field, value):
    """A tick as (mid, ask, bid) with one price replaced."""

    tick = {"mid": 100.0, "ask": 100.1, "bid": 99.9}
    tick[field] = value
    return tick["mid"], tick["ask"], tick["bid"]


_BAD_TICKS = [
    pytest.param(_bad_value_tick(field, value), id=f"{field}={label}")
    for field in ("mid", "ask", "bid")
    for label, value in (
        ("nan", float("nan")), ("inf", float("inf")), ("-inf", float("-inf")),
        ("zero", 0.0), ("negative", -1.0), ("bool", True), ("text", "100.0"),
        ("1e308", 1e308), ("1e12", 1e12),
    )
] + [
    pytest.param((100.0, 99.9, 100.1), id="crossed_book"),
    # Opus verify of 79c4e47: a crossed tick sized 10 of 10 in regime 3.
    pytest.param((100.0, 99.0, 101.0), id="crossed_100_99_101"),
    # Opus verify: ask=1, bid=-1 gives mid=0 and spread=0, which read as calm and sized 10 of 10.
    pytest.param((0.0, 1.0, -1.0), id="mid0_from_ask1_bid-1"),
    pytest.param((-0.5, 1.0, -2.0), id="negative_book"),
]


def _comparable(result):
    """A tick result without the one field that legitimately differs after an invalidation."""

    return {key: value for key, value in result.items() if key != "regime_changed"}


@pytest.mark.parametrize("warm", [False, True], ids=["cold_start", "warm_start"])
@pytest.mark.parametrize("bad_tick", _BAD_TICKS)
@pytest.mark.asyncio
async def test_a_non_finite_or_malformed_tick_is_refused_before_any_feature_state_moves(bad_tick, warm, tmp_path):
    """Codex r2 P1: on a fresh loop ``process_tick(nan, 100.1, 99.9)`` returned regime 3 valid with
    leverage 0.98, because the volatility tracker returns 0.0 on its first update and hides the
    bad mid. The tick is validated before any update, the regime is invalidated with
    TICK_INVALID, and the next good tick classifies as if the bad one never arrived."""

    from backend.trading_loop import LiveTradingLoop

    ticks = _tick_stream()
    warmup = 40 if warm else 0
    reference = LiveTradingLoop(_StubManager(), shipped_params(), telemetry_db_path=str(tmp_path / "ref.db"))
    expected = [reference.process_tick(*tick) for tick in ticks[:46]]

    loop = LiveTradingLoop(_StubManager(), shipped_params(), telemetry_db_path=str(tmp_path / "t.db"))
    for tick in ticks[:warmup]:
        loop.process_tick(*tick)
    if warm:
        assert loop.current_regime != -1 and loop.risk_leverage_coefficient > 0.0

    result = loop.process_tick(*bad_tick)  # must not raise
    assert result["regime_valid"] is False and result["dominant_regime"] == -1
    assert result["invalid_reason"] == "TICK_INVALID"
    assert loop.current_regime == -1 and loop.requoter.current_regime == -1
    assert loop.risk_leverage_coefficient == 0.0 and result["risk_leverage_coefficient"] == 0.0

    decision, dispatched = await _preflight_buy(loop)
    assert decision.approved is False and decision.scaled_size == 0.0 and dispatched == []

    # Nothing was updated by the refused tick: the good ticks that follow match a loop that
    # never saw it, and the regime comes back.
    recovered = [loop.process_tick(*tick) for tick in ticks[warmup:46]]
    assert [_comparable(r) for r in recovered] == [_comparable(r) for r in expected[warmup:46]]
    assert loop.current_regime != -1 and loop.risk_leverage_coefficient > 0.0
    decision, dispatched = await _preflight_buy(loop)
    assert decision.approved is True and dispatched


def test_a_numpy_float_tick_and_a_zero_spread_tick_are_still_classified(tmp_path):
    """The validation must not refuse what the success path accepts: numpy floats and bid == ask."""

    from backend.trading_loop import LiveTradingLoop

    loop = LiveTradingLoop(_StubManager(), shipped_params(), telemetry_db_path=str(tmp_path / "t.db"))
    for mid, ask, bid in _tick_stream()[:10]:
        assert loop.process_tick(np.float64(mid), np.float32(ask), np.float64(bid))["regime_valid"] is True
    assert loop.process_tick(100.0, 100.0, 100.0)["regime_valid"] is True
    # A large but sane price (just under the 1e12 bound) is not refused.
    big = LiveTradingLoop(_StubManager(), shipped_params(), telemetry_db_path=str(tmp_path / "big.db"))
    assert big.process_tick(9.9e11, 9.9e11 + 1.0, 9.9e11 - 1.0)["regime_valid"] is True


def _feature_state(loop):
    """Every number the feature pipeline keeps between ticks."""

    vol, spread = loop.vol_tracker, loop.spread_tracker
    vol_std, spread_std = loop.vol_standardizer, loop.spread_standardizer
    return (
        (vol.last_price, vol.var, vol.vol, vol.initialized),
        spread.spread,
        (vol_std.count, vol_std.mean, vol_std.M2),
        (spread_std.count, spread_std.mean, spread_std.M2),
    )


_MIN_PRICE = 1e-9  # mirrors trading_loop._MIN_TICK_PRICE; a test below pins the constant itself
_BELOW_FLOOR = float(np.nextafter(_MIN_PRICE, 0.0))

_FLOOR_AND_BOOK_TICKS = [
    pytest.param(_bad_value_tick(field, value), id=f"{field}={label}")
    for field in ("mid", "ask", "bid")
    for label, value in (
        ("1e-200", 1e-200), ("denormal", 5e-324), ("half_floor", _MIN_PRICE / 2), ("just_below_floor", _BELOW_FLOOR),
    )
] + [
    # A consistent, uncrossed book entirely below the floor: only the floor refuses it.
    pytest.param((1e-200, 1e-200, 1e-200), id="locked_book_1e-200"),
    pytest.param((2e-200, 3e-200, 1e-200), id="consistent_book_1e-200"),
    pytest.param((_BELOW_FLOOR, _BELOW_FLOOR, _BELOW_FLOOR), id="locked_book_just_below_floor"),
] + [
    # Inside the price range and not crossed, but the mid is not in the book.
    pytest.param((99.8, 100.1, 99.9), id="mid_below_bid"),
    pytest.param((100.2, 100.1, 99.9), id="mid_above_ask"),
    pytest.param((float(np.nextafter(99.9, 0.0)), 100.1, 99.9), id="mid_one_ulp_below_bid"),
    pytest.param((float(np.nextafter(100.1, 200.0)), 100.1, 99.9), id="mid_one_ulp_above_ask"),
    pytest.param((100.000001, 100.0, 99.999999), id="narrow_book_mid_just_above_ask"),
    pytest.param((100.5, 100.0, 100.0), id="zero_spread_mid_off_the_quote"),
]


@pytest.mark.parametrize("warm", [False, True], ids=["cold_start", "warm_start"])
@pytest.mark.parametrize("bad_tick", _FLOOR_AND_BOOK_TICKS)
@pytest.mark.asyncio
async def test_a_sub_floor_price_or_a_mid_outside_the_book_is_refused_and_leaves_feature_state_untouched(
    bad_tick, warm, tmp_path
):
    """Follow-ups to #568. A positive price like 1e-200 passed validation, and the next normal tick
    then squared a relative return near 1e200: infinite variance in the EMA and Welford state,
    so trading stayed blocked until restart. A mid outside [bid, ask] classified as if it were
    a real quote. Both are refused now, before any state moves, and the next good tick classifies
    as if the bad one never arrived."""

    from backend.trading_loop import LiveTradingLoop

    ticks = _tick_stream()
    warmup = 40 if warm else 0
    reference = LiveTradingLoop(_StubManager(), shipped_params(), telemetry_db_path=str(tmp_path / "ref.db"))
    expected = [reference.process_tick(*tick) for tick in ticks[:46]]

    loop = LiveTradingLoop(_StubManager(), shipped_params(), telemetry_db_path=str(tmp_path / "t.db"))
    for tick in ticks[:warmup]:
        loop.process_tick(*tick)
    if warm:
        assert loop.current_regime != -1 and loop.risk_leverage_coefficient > 0.0
    before = _feature_state(loop)

    result = loop.process_tick(*bad_tick)  # must not raise
    assert result["regime_valid"] is False and result["dominant_regime"] == -1
    assert result["invalid_reason"] == "TICK_INVALID"
    assert _feature_state(loop) == before, "a refused tick must not move any tracker or standardizer"
    assert loop.current_regime == -1 and loop.requoter.current_regime == -1
    assert loop.risk_leverage_coefficient == 0.0 and result["risk_leverage_coefficient"] == 0.0

    decision, dispatched = await _preflight_buy(loop)
    assert decision.approved is False and decision.scaled_size == 0.0 and dispatched == []

    recovered = [loop.process_tick(*tick) for tick in ticks[warmup:46]]
    assert [_comparable(r) for r in recovered] == [_comparable(r) for r in expected[warmup:46]]
    assert loop.current_regime != -1 and loop.risk_leverage_coefficient > 0.0
    decision, dispatched = await _preflight_buy(loop)
    assert decision.approved is True and dispatched


@pytest.mark.parametrize(
    "tick",
    [
        pytest.param((_MIN_PRICE, _MIN_PRICE, _MIN_PRICE), id="all_three_at_the_floor"),
        pytest.param((100.0, 100.0, 100.0), id="bid_eq_ask_eq_mid"),
        pytest.param((99.9, 100.1, 99.9), id="mid_on_the_bid"),
        pytest.param((100.1, 100.1, 99.9), id="mid_on_the_ask"),
        pytest.param((100.0, 100.1, 99.9), id="mid_inside"),
    ],
)
def test_the_floor_and_the_book_edges_are_inclusive(tick, tmp_path):
    """A price exactly at the floor is accepted, and so is a mid exactly on the bid or ask
    (a locked book has bid == mid == ask). Only strictly outside is refused."""

    from backend.trading_loop import LiveTradingLoop

    loop = LiveTradingLoop(_StubManager(), shipped_params(), telemetry_db_path=str(tmp_path / "t.db"))
    assert loop.process_tick(*tick)["regime_valid"] is True


def test_the_floor_constants_keep_the_worst_case_variance_finite_through_the_real_trackers(tmp_path):
    """Numerical proof, not a check on the constants. The worst relative return the loop can see is
    from a price at the floor to one just under the ceiling. Push it, and repeated bounces
    between the extremes, through the real volatility tracker and Welford standardizers (and
    the whole loop) and require every state number to stay finite with headroom."""

    import backend.trading_loop as loop_module
    from backend.features.online_vol import OnlineVolatility
    from backend.features.welford import WelfordStandardizer
    from backend.trading_loop import LiveTradingLoop

    assert loop_module._MIN_TICK_PRICE == _MIN_PRICE
    low, high = loop_module._MIN_TICK_PRICE, float(np.nextafter(loop_module._MAX_TICK_PRICE, 0.0))
    worst_return = (high - low) / low
    headroom = np.finfo(np.float64).max / 1e100  # the squared return must sit far below overflow
    assert worst_return**2 < headroom

    for alpha in (1.0, 0.5, 0.05):  # alpha 1.0 puts the whole squared return into the EMA at once
        tracker, standardizer = OnlineVolatility(alpha=alpha), WelfordStandardizer()
        for price in [low, high, low, high] * 250:
            standardizer.update(tracker.update(price))
        for name, number in (
            ("var", tracker.var), ("vol", tracker.vol), ("mean", standardizer.mean),
            ("M2", standardizer.M2), ("variance", standardizer.variance),
        ):
            assert math.isfinite(number), f"{name} overflowed at alpha={alpha}"
            assert number < headroom
        assert math.isfinite(standardizer.zscore(tracker.vol))

    # The same extremes through the loop itself, after a warm start with the shipped scalers.
    loop = LiveTradingLoop(_StubManager(), shipped_params(), telemetry_db_path=str(tmp_path / "t.db"))
    for tick in _tick_stream()[:40]:
        loop.process_tick(*tick)
    for _ in range(100):
        loop.process_tick(low, low, low)
        loop.process_tick(high, high, high)
    for number in (
        loop.vol_tracker.var, loop.vol_tracker.vol, loop.vol_standardizer.mean, loop.vol_standardizer.M2,
        loop.spread_standardizer.mean, loop.spread_standardizer.M2,
    ):
        assert math.isfinite(number) and number < headroom


def test_without_the_floor_a_tiny_price_would_poison_the_volatility_state():
    """The reason for the floor, shown on the real tracker: one 1e-200 price followed by a normal
    one leaves an infinite variance that no later tick unwinds."""

    from backend.features.online_vol import OnlineVolatility

    tracker = OnlineVolatility(alpha=0.05)
    for price in (100.0, 1e-200, 100.0):
        tracker.update(price)
    assert not math.isfinite(tracker.var)
    for _ in range(50):
        tracker.update(100.0)
    assert not math.isfinite(tracker.var), "the poison does not decay"

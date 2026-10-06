"""AC-12: GMM regime filter (D-09)."""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import date
from decimal import Decimal

import numpy as np
import pytest

from strategy_india.engine import fit_fold_components, run_walk_forward
from strategy_india.folds import FoldRules
from strategy_india.holdout import HoldoutRange
from strategy_india.params import RegimeSpec  # noqa: F401
from strategy_india.regime import fit_regime, regime_flags
from strategy_india.signals import MODE_BASE, FeatureRow

from test_strategy_india_support import (
    SESSION_START,
    default_names,
    etf_names,
    make_context,
    make_rows,
    params,
    weekday_sessions,
)

SPEC = params().regime
CALM = (0.006, 0.001, -0.03, 0.01, 0.65, 0.05)
MID = (0.014, 0.002, -0.11, 0.015, 0.40, 0.05)
STRESS = (0.026, 0.003, -0.22, 0.03, 0.18, 0.05)


def _row(day: date, regime, rng: random.Random) -> FeatureRow:
    vm, vs, dm, ds, bm, bs = regime
    return FeatureRow(day, Decimal(f"{abs(rng.gauss(vm, vs)):.6f}"), Decimal(f"{min(0.0, rng.gauss(dm, ds)):.6f}"),
                      Decimal(f"{min(1.0, max(0.0, rng.gauss(bm, bs))):.6f}"))


def features(plan: list[tuple[tuple, int]], seed: int = 1) -> list[FeatureRow]:
    rng = random.Random(seed)
    days = weekday_sessions(SESSION_START, sum(n for _, n in plan))
    out, i = [], 0
    for regime, n in plan:
        for _ in range(n):
            out.append(_row(days[i], regime, rng))
            i += 1
    return out


def shuffled(rows: list[FeatureRow], seed: int) -> list[FeatureRow]:
    """Same observations in a mixed order, re-dated, so train rows hold every regime."""
    rng = random.Random(seed)
    values = [(r.volatility, r.drawdown, r.breadth) for r in rows]
    rng.shuffle(values)
    return [FeatureRow(r.session, *v) for r, v in zip(rows, values)]


TWO = shuffled(features([(CALM, 160), (STRESS, 60)]), 3)
THREE = shuffled(features([(CALM, 150), (MID, 90), (STRESS, 70)]), 4)


def test_k_is_chosen_by_bic_between_two_and_three():
    two = fit_regime(TWO, spec=SPEC, seed=5)
    three = fit_regime(THREE, spec=SPEC, seed=5)
    assert (two.k, three.k) == (2, 3)
    assert set(two.bic_by_k) == {2, 3} and not two.degenerate
    assert Decimal(two.bic_by_k[2]) < Decimal(two.bic_by_k[3])
    assert Decimal(three.bic_by_k[3]) < Decimal(three.bic_by_k[2])


def test_a_component_under_the_minimum_weight_is_rejected():
    strict = SPEC.model_copy(update={"min_component_weight": Decimal("0.6")})  # no K can give every component 60%
    model = fit_regime(TWO, spec=strict, seed=5)
    assert model.degenerate and model.k == 1
    flags = regime_flags(model, TWO, strict)
    assert not any(flags.values()), "a degenerate model stays invested, and says so in its record"
    assert model.record()["degenerate"] is True
    ok = fit_regime(TWO, spec=SPEC, seed=5)
    assert float(ok.min_weight) >= 0.05


def test_components_are_ordered_by_a_fixed_risk_score_so_labels_do_not_depend_on_the_seed():
    base = TWO
    test_rows = features([(CALM, 15), (STRESS, 15), (CALM, 15)], seed=9)
    flag_sets = []
    for seed in (1, 2, 3, 4):
        model = fit_regime(base, spec=SPEC, seed=seed)
        scaled_means = model.gmm.means_
        risk = scaled_means[:, 0] - scaled_means[:, 1]
        assert model.cash_component == int(np.argmax(risk)), "the highest risk score is the cash regime"
        assert model.risk_order[-1] == model.cash_component
        flags = regime_flags(model, test_rows, replace_hysteresis(SPEC, 1))
        flag_sets.append(tuple(flags.values()))
    assert len(set(flag_sets)) == 1, "same cash flags for every seed"
    flags = flag_sets[0]
    assert all(flags[15:30]) and not any(flags[:15]) and not any(flags[31:])


def replace_hysteresis(spec: RegimeSpec, dwell: int) -> RegimeSpec:
    return spec.model_copy(update={"min_dwell": dwell})


class _Stub:
    def __init__(self, posterior):
        self._p = posterior

    def cash_posterior(self, rows):
        return list(self._p)


def _flags(posterior, enter="0.8", leave="0.4", dwell=1):
    spec = SPEC.model_copy(update={"enter_cash": Decimal(enter), "exit_cash": Decimal(leave), "min_dwell": dwell})
    days = weekday_sessions(SESSION_START, len(posterior))
    rows = [FeatureRow(d, Decimal(0), Decimal(0), Decimal(0)) for d in days]
    return [regime_flags(_Stub(posterior), rows, spec)[d] for d in days]


def test_hysteresis_holds_the_cash_flag_between_the_thresholds():
    # enters at 0.9, then 0.6 and 0.5 sit between exit 0.4 and enter 0.8: it must not flip back
    assert _flags([0.1, 0.9, 0.6, 0.5, 0.45, 0.3, 0.6, 0.7, 0.85]) == [False, True, True, True, True, False, False, False, True]


def test_minimum_dwell_blocks_a_one_session_flip():
    assert _flags([0.1, 0.9, 0.1, 0.1, 0.1], dwell=3) == [False, True, True, True, False]
    assert _flags([0.1, 0.9, 0.1, 0.1, 0.1], dwell=1) == [False, True, False, False, False]


def test_same_inputs_give_identical_model_hashes_and_records():
    a = fit_regime(THREE, spec=SPEC, seed=11)
    b = fit_regime(THREE, spec=SPEC, seed=11)
    assert a.model_sha256 == b.model_sha256 and a.record() == b.record()
    assert fit_regime(THREE, spec=SPEC, seed=12).seed == 12
    changed = [replace(r, volatility=r.volatility * Decimal("1.5")) if i == 3 else r for i, r in enumerate(THREE)]
    assert fit_regime(changed, spec=SPEC, seed=11).model_sha256 != a.model_sha256
    record = a.record()
    assert {"k", "seed", "model_sha256"} <= set(record) and record["seed"] == 11


def test_regime_flags_use_the_train_scaler_and_never_refit_on_the_rows_being_scored():
    model = fit_regime(TWO, spec=SPEC, seed=5)
    stress_only = features([(STRESS, 25)], seed=9)
    raw = np.array([[float(r.volatility), float(r.drawdown), float(r.breadth)] for r in stress_only])
    by_train = model.gmm.predict_proba((raw - model.scaler_mean) / model.scaler_scale)[:, model.cash_component]
    assert np.allclose(model.cash_posterior(stress_only), by_train)
    refit_on_test = model.gmm.predict_proba((raw - raw.mean(axis=0)) / np.where(raw.std(axis=0) > 0, raw.std(axis=0), 1.0))[:, model.cash_component]
    assert by_train.min() > 0.95 and refit_on_test.min() < 0.05 and refit_on_test.mean() < 0.8, "the two scalers disagree, so this test can tell them apart"
    flags = regime_flags(model, stress_only, replace_hysteresis(SPEC, 1))
    assert all(list(flags.values())[1:])
    shifted = [replace(r, volatility=r.volatility * 3) for r in stress_only]  # scoring rows never feed back into the scaler
    mean_before = model.scaler_mean.copy()
    regime_flags(model, shifted, SPEC)
    assert np.array_equal(model.scaler_mean, mean_before)


def _engine_setup():
    sessions = weekday_sessions(SESSION_START, 330)
    rows = make_rows(sessions, default_names(8) + etf_names())
    holdout = HoldoutRange(sessions[-20], sessions[-1])
    ctx = make_context(rows, holdout)
    return ctx, ctx.view.sessions()


def test_model_is_fit_on_training_rows_only_and_scaler_ignores_test_rows():
    ctx, dev = _engine_setup()
    feats = ctx.table(MODE_BASE).market_features()
    cutoff = dev[200]
    model, *_ = fit_fold_components(ctx, feats, cutoff=cutoff, observations=None)
    perturbed = [replace(f, volatility=f.volatility * 7, drawdown=f.drawdown * 3, breadth=Decimal("0.01"))
                 if f.session > cutoff else f for f in feats]
    again, *_ = fit_fold_components(ctx, perturbed, cutoff=cutoff, observations=None)
    assert again.model_sha256 == model.model_sha256
    assert np.array_equal(again.scaler_mean, model.scaler_mean) and np.array_equal(again.scaler_scale, model.scaler_scale)
    train = np.array([[float(f.volatility), float(f.drawdown), float(f.breadth)] for f in feats if f.session <= cutoff])
    assert np.allclose(model.scaler_mean, train.mean(axis=0))
    touched_train = [replace(f, volatility=f.volatility * 2) if f.session == dev[60] else f for f in feats]
    other, *_ = fit_fold_components(ctx, touched_train, cutoff=cutoff, observations=None)
    assert other.model_sha256 != model.model_sha256, "training rows do move the fit"


def test_walk_forward_records_k_seed_and_model_hash_per_fold_and_is_repeatable():
    ctx, dev = _engine_setup()
    rules = FoldRules(n_folds=2, test_sessions=40, min_train_sessions=100)
    first = run_walk_forward(ctx, rules)
    second = run_walk_forward(make_context(ctx_rows(ctx), ctx.view.holdout), rules)
    for fold in first.folds:
        assert fold.regime["k"] in (1, 2, 3) and fold.regime["seed"] == ctx.params.seed
        assert len(fold.regime["model_sha256"]) == 64
    assert [f.regime for f in first.folds] == [f.regime for f in second.folds]
    assert first.folds[0].regime["model_sha256"] != first.folds[1].regime["model_sha256"], "refit per fold"


def ctx_rows(ctx):
    rows = []
    for anchor in ctx.view.anchors():
        rows += list(ctx.view.series(anchor))
    return rows

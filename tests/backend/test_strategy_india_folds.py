"""AC-11: purged, forward-only folds (D-13)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from costs.core import LookaheadError

from strategy_india import engine
from strategy_india.engine import _label_observations, fit_fold_components, simulate_segment
from strategy_india.errors import StrategyIndiaError
from strategy_india.folds import (
    FoldRules,
    Observation,
    make_folds,
    purge_at_cutoff,
    purge_at_holdout,
)
from strategy_india.holdout import HoldoutRange
from strategy_india.signals import MODE_BASE

from test_strategy_india_support import (
    SESSION_START,
    default_names,
    etf_names,
    make_context,
    make_rows,
    params,
    weekday_sessions,
)

SESSIONS = weekday_sessions(SESSION_START, 400)
RULES = FoldRules(n_folds=4, test_sessions=40, min_train_sessions=120)
ZERO = Decimal(0)


def test_folds_are_chronological_and_forward_only():
    folds = make_folds(SESSIONS, RULES)
    assert [f.index for f in folds] == [1, 2, 3, 4]
    assert folds[-1].test_end == SESSIONS[-1]
    for fold in folds:
        assert fold.train_start == SESSIONS[0]
        assert fold.cutoff < fold.test_start  # training ends before the test starts
        assert fold.test_start <= fold.test_end
        inside = [s for s in SESSIONS if fold.in_test(s)]
        assert len(inside) == RULES.test_sessions
        assert SESSIONS[SESSIONS.index(fold.test_start) - 1] == fold.cutoff  # no gap and no overlap
    for earlier, later in zip(folds, folds[1:]):
        assert earlier.test_end < later.test_start < later.test_end
        assert later.cutoff >= earlier.test_end  # expanding: later folds only ever look back
    assert RULES.as_payload()["purge"] == "outcome_availability" and RULES.as_payload()["gap_sessions"] == 0


def test_too_short_a_window_is_refused():
    with pytest.raises(StrategyIndiaError):
        make_folds(SESSIONS[:150], RULES)
    with pytest.raises(StrategyIndiaError):
        FoldRules(n_folds=0, test_sessions=10, min_train_sessions=10)
    with pytest.raises(StrategyIndiaError):
        FoldRules(n_folds=2, test_sessions=10, min_train_sessions=10, scheme="rolling")


def obs(entry: int, outcome: int | None) -> Observation:
    return Observation(SESSIONS[entry], SESSIONS[outcome] if outcome is not None else None, Decimal(1), Decimal("0.01"))


def test_training_trade_delayed_past_the_cutoff_is_purged_and_resolved_one_is_kept():
    cutoff = SESSIONS[100]
    resolved_before = obs(90, 99)
    resolved_on_cutoff = obs(95, 100)
    delayed = obs(95, 104)  # exit intent came earlier, but NO_ASSUMED_FILL pushed the real fill past the cutoff
    unresolved = obs(98, None)
    later_entry = obs(101, 105)  # not a training observation of this fold at all
    kept, purged = purge_at_cutoff([resolved_before, resolved_on_cutoff, delayed, unresolved, later_entry], cutoff)
    assert kept == [resolved_before, resolved_on_cutoff]
    assert purged == [delayed, unresolved]


def test_observations_resolving_inside_the_holdout_are_purged():
    holdout_start = SESSIONS[300]
    inside = obs(295, 301)
    unresolved = obs(298, None)
    before = obs(280, 290)
    kept, purged = purge_at_holdout([before, inside, unresolved], holdout_start)
    assert kept == [before] and purged == [inside, unresolved]
    assert purge_at_holdout([obs(280, 299)], holdout_start)[0]  # resolved the session before: kept
    assert not purge_at_holdout([obs(280, 300)], holdout_start)[0]  # resolves on the first holdout session: purged


def _fit_params():
    return params(edge_map={"mode": "fit", "slope": "0.01", "min_obs": 3, "ridge": "1"})


def _setup(unavailable=None):
    sessions = weekday_sessions(SESSION_START, 200)
    rows = make_rows(sessions, default_names(8) + etf_names())
    holdout = HoldoutRange(sessions[-20], sessions[-1])
    ctx = make_context(rows, holdout, params_obj=_fit_params(), unavailable=unavailable)
    return ctx, ctx.view.sessions(), rows, holdout


def test_engine_purges_a_trade_whose_exit_was_delayed_by_no_assumed_fill():
    ctx, dev, rows, holdout = _setup()
    gate = ctx.scenarios.gate()
    control = simulate_segment(ctx, sessions=dev, scenario=gate, slope=Decimal("0.01"), regime_cash=None, mode=MODE_BASE, fold="label")
    target = control.closed[2]
    cutoff = dev[dev.index(target.exit_date) + 1]  # the exit fills one session before the cutoff in the control run
    blocked = {(isin, day): "band_crosscheck_row_conflict" for isin in {r.isin for r in rows} for day in (target.exit_date, cutoff)}
    bctx, _, _, _ = _setup(unavailable=blocked)
    c_obs = _label_observations(ctx, dev)
    b_obs = _label_observations(bctx, dev)
    key = lambda o: (o.entry_date, o.score)  # noqa: E731
    wanted = (target.entry_date, target.entry_score)
    kept_c, purged_c = purge_at_cutoff(c_obs, cutoff)
    kept_b, purged_b = purge_at_cutoff(b_obs, cutoff)
    assert wanted in {key(o) for o in kept_c}, "resolved before the cutoff in the unblocked run: kept"
    assert wanted in {key(o) for o in purged_b}, "the same trade, exit delayed past the cutoff: purged"
    assert all(o.outcome_date is not None and o.outcome_date <= cutoff for o in kept_b)
    # and the fit step sees exactly those sets
    features = bctx.table(MODE_BASE).market_features()
    _, _, _, n_kept, n_purged = fit_fold_components(bctx, features, cutoff=cutoff, observations=b_obs)
    assert (n_kept, n_purged) == (len(kept_b), len(purged_b))


def test_engine_purges_unresolved_development_trades_at_the_holdout_boundary():
    ctx, dev, rows, holdout = _setup()
    observations = _label_observations(ctx, dev)
    assert any(o.outcome_date is None for o in observations), "a book is still open at the end of development"
    features = ctx.table(MODE_BASE).market_features()
    _, slope, fitted, kept, purged = fit_fold_components(ctx, features, cutoff=dev[-1], observations=observations,
                                                         holdout_start=holdout.start)
    expected_purged = [o for o in observations if o.outcome_date is None or o.outcome_date >= holdout.start]
    assert purged == len(expected_purged) >= 1 and kept == len(observations) - purged
    assert slope >= ZERO


def test_features_may_warm_up_on_prices_before_the_segment():
    ctx, dev, _, _ = _setup()
    short = dev[100:112]  # shorter than the 20 session lookback
    assert len(short) < ctx.params.lookback_sessions
    res = simulate_segment(ctx, sessions=short, scenario=ctx.scenarios.gate(), slope=Decimal("0.01"), regime_cash=None,
                           mode=MODE_BASE, fold="w")
    assert res.entries > 0, "scores at the first decision came from prices before the segment"
    assert ctx.table(MODE_BASE).raw_score("INE000A01000", short[0]) is not None


def test_any_order_not_strictly_before_its_fill_session_raises_lookahead_error_through_the_engine(monkeypatch):
    ctx, dev, _, _ = _setup()
    monkeypatch.setattr(engine, "order_information_date", lambda decision, session: session)
    with pytest.raises(LookaheadError):
        simulate_segment(ctx, sessions=dev, scenario=ctx.scenarios.gate(), slope=Decimal("0.01"), regime_cash=None,
                         mode=MODE_BASE, fold="x")


def test_an_order_stamped_with_a_later_date_than_its_session_also_raises(monkeypatch):
    ctx, dev, _, _ = _setup()
    monkeypatch.setattr(engine, "order_information_date", lambda decision, session: session + timedelta(days=1))
    with pytest.raises(LookaheadError):
        simulate_segment(ctx, sessions=dev, scenario=ctx.scenarios.gate(), slope=Decimal("0.01"), regime_cash=None,
                         mode=MODE_BASE, fold="x")

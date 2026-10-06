"""D-12: no preflight refusal puts a holdout-session date, count or series into the operator-facing message.

Every refusal that can fire before the holdout opens is driven here, and each message is held to the rule the
#548 inference refusal already met: it names a category and carries no digit and no date.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from strategy_india import data, study
from strategy_india.data import DividendEvents
from strategy_india.errors import StrategyIndiaError
from strategy_india.ticks import load_default_tables

from test_strategy_india_support import (
    ETF_ISINS,
    SESSION_START,
    default_names,
    etf_names,
    make_rows,
    study_inputs,
    tag_rows,
    weekday_sessions,
)

HOLDOUT_DAYS = weekday_sessions(SESSION_START, 400)[-60:]
ETF = ETF_ISINS[0]
ANY_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _case(tmp_path, *, drop=(), updates=None, **kw):
    sessions = weekday_sessions(SESSION_START, 400)
    rows = make_rows(sessions, default_names(10) + etf_names(), drop=drop)
    if updates:
        rows = [r.model_copy(update=updates(r)) if updates(r) else r for r in rows]
    inputs = study_inputs(tmp_path, rows=tag_rows(rows, DividendEvents()), **kw)
    study.register(inputs, hypothesis="h")
    return inputs


def _in_gap(tmp_path, **kw):
    inputs = study_inputs(tmp_path, sessions_n=500, start=date(2024, 1, 1), **kw)
    return inputs


def _refusal(inputs) -> str:
    head = inputs.registry.head_hash()
    with pytest.raises(StrategyIndiaError) as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.code == "holdout_unrunnable" and "NOT spent" in str(err.value)
    assert inputs.registry.holdout_events() == () and inputs.registry.head_hash() == head
    return str(err.value)


def _gap_without_inference(tmp_path):
    inputs = _in_gap(tmp_path)  # the committed ETF table has no tick for these sessions and nothing infers one
    study.register(inputs, hypothesis="h")
    return inputs


def _gap_thin_sample(tmp_path):
    inputs = study_inputs(tmp_path, sessions_n=400, start=date(2024, 1, 1))
    inputs.ticks = load_default_tables(rows=inputs.rows, benchmark_isins=ETF_ISINS[:1])
    study.register(inputs, hypothesis="h")
    return inputs


def _equity_gap(tmp_path):
    inputs = study_inputs(tmp_path, start=date(2019, 1, 1))  # before the first committed equity version
    study.register(inputs, hypothesis="h")
    return inputs


def _eligibility_missing(tmp_path, monkeypatch):
    inputs = _case(tmp_path)
    monkeypatch.setattr(data._surveillance, "snapshot_for", lambda store, kind, day: None)
    inputs.eligibility = data.UniverseEligibility(object(), object(), object())
    return inputs


SCENARIOS = {
    "gap_no_inferred_source": _gap_without_inference,
    "gap_inference_refused": _gap_thin_sample,
    "equity_gap": _equity_gap,
    "etf_missing_30_bars": lambda p: _case(p, drop=[(ETF, d) for d in HOLDOUT_DAYS[-30:]]),
    "etf_missing_every_bar": lambda p: _case(p, drop=[(ETF, d) for d in HOLDOUT_DAYS]),
    "etf_missing_one_bar": lambda p: _case(p, drop=[(ETF, HOLDOUT_DAYS[20])]),
    "etf_series_be_first_day": lambda p: _case(
        p, updates=lambda r: {"series": "BE"} if r.anchor_isin == ETF and r.trade_date == HOLDOUT_DAYS[0] else None),
    "etf_series_bz_late_day": lambda p: _case(
        p, updates=lambda r: {"series": "BZ"} if r.anchor_isin == ETF and r.trade_date == HOLDOUT_DAYS[41] else None),
    "non_positive_first_close": lambda p: _case(
        p, updates=lambda r: {"raw_close": Decimal(0)} if r.anchor_isin == ETF and r.trade_date == HOLDOUT_DAYS[0] else None),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_no_preflight_refusal_carries_a_holdout_date_count_or_series(tmp_path, name):
    message = _refusal(SCENARIOS[name](tmp_path))
    assert not re.search(r"\d", message), message  # no count, no day, no year: window constants never reach a message
    assert not ANY_DATE.search(message), message
    assert not any(day.isoformat() in message for day in HOLDOUT_DAYS), message
    assert not re.search(r"\b(BE|BZ)\b", message), message  # the series a holdout row carries


def test_a_missing_surveillance_snapshot_refuses_with_a_category_and_no_date(tmp_path, monkeypatch):
    message = _refusal(_eligibility_missing(tmp_path, monkeypatch))
    assert "eligibility inputs are missing" in message and "ASM" in message  # the category survives
    assert not re.search(r"\d", message) and not ANY_DATE.search(message), message


def test_the_refusals_still_name_their_category(tmp_path):
    # Hygiene must not blank the message: each refusal still says what failed.
    assert "does not cover every holdout session" in _refusal(_gap_without_inference(tmp_path / "a"))
    assert "no bar on at least one holdout session" in _refusal(SCENARIOS["etf_missing_30_bars"](tmp_path / "b"))
    assert "does not cover the benchmark ETF's series" in _refusal(SCENARIOS["etf_series_be_first_day"](tmp_path / "c"))

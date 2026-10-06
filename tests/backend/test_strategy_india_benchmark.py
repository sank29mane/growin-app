"""AC-16: benchmarks (D-17). ETF by ADV and completeness; TRI only by path plus sha256."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from costs.core import TickSizeUnavailable

from strategy_india import benchmark, study
from strategy_india.benchmark import TRI_LABEL, TriSeries, TriUnavailable, load_tri
from strategy_india.data import DatasetView
from strategy_india.errors import DataError
from strategy_india.holdout import HoldoutRange
from strategy_india.report import write_report
from strategy_india.ticks import EQUITY, NON_GOLD_ETF, TickTables, load_default_tables, resolve_tick

from test_strategy_india_support import (
    ETF_ISINS,
    SESSION_START,
    NameSpec,
    default_names,
    etf_names,
    make_context,
    make_rows,
    params,
    study_inputs,
    tick_tables,
    weekday_sessions,
    write_tri,
)

FIXTURES = Path(__file__).parent / "fixtures" / "strategy_india"
SESSIONS = weekday_sessions(SESSION_START, 100)
FAR = HoldoutRange(date(2030, 1, 1), date(2030, 2, 1))
SPEC = params().benchmark


def _view(rows):
    return DatasetView.from_rows(rows, holdout=FAR)


def test_etf_is_chosen_by_median_adv_when_both_are_complete():
    view = _view(make_rows(SESSIONS, default_names(3) + etf_names()))
    choice = benchmark.choose_etf(view, SPEC, start=SESSIONS[0], end=SESSIONS[-1])
    assert choice.anchor_isin == ETF_ISINS[0]  # 250 x 9m shares is far deeper than 120 x 0.9m
    assert Decimal(choice.completeness) == 1 and choice.median_traded_value > 0
    assert {c[0] for c in choice.considered} == set(ETF_ISINS)


def test_etf_with_incomplete_bars_is_skipped_whatever_its_volume():
    dropped = [(ETF_ISINS[0], d) for d in SESSIONS[10:40]]  # 70 percent complete, floor is 95
    view = _view(make_rows(SESSIONS, default_names(3) + etf_names(), drop=dropped))
    choice = benchmark.choose_etf(view, SPEC, start=SESSIONS[0], end=SESSIONS[-1])
    assert choice.anchor_isin == ETF_ISINS[1]
    only = SPEC.model_copy(update={"candidate_isins": (ETF_ISINS[0],)})
    with pytest.raises(DataError):
        benchmark.choose_etf(view, only, start=SESSIONS[0], end=SESSIONS[-1])


def test_ties_break_on_the_isin():
    twins = [NameSpec(i, c, drift_bps=3, vol_bps=40, start_price=Decimal("100"), volume=1_000_000)
             for i, c in zip(reversed(ETF_ISINS), ("TWOB", "ONEB"))]
    view = _view(make_rows(SESSIONS, default_names(3) + twins))
    assert benchmark.choose_etf(view, SPEC, start=SESSIONS[0], end=SESSIONS[-1]).anchor_isin == ETF_ISINS[0]


def test_etf_buy_and_hold_prices_one_round_trip_with_the_60_estimator():
    ctx = make_context(make_rows(SESSIONS, default_names(3) + etf_names()), FAR)
    res = benchmark.etf_buy_and_hold(ctx.view, ETF_ISINS[0], start=SESSIONS[0], end=SESSIONS[-1], capital=Decimal(50000),
                                     ticks=ctx.ticks, schedules=ctx.schedules, pricing_basis=ctx.pricing_basis)
    assert res.shares == int(Decimal(50000) // res.entry_price) and res.round_trip_cost > 0
    gross = Decimal(res.shares) * (res.exit_price - res.entry_price)
    assert res.net_return == (gross - res.round_trip_cost) / Decimal(50000)
    assert res.curve[0][1] == Decimal(50000) and len(res.curve) == len(SESSIONS)


def test_etf_dates_with_no_tick_table_are_unknown_not_defaulted():
    ctx = make_context(make_rows(SESSIONS, default_names(3) + etf_names()), FAR)
    equity_only = TickTables({EQUITY: load_default_tables().table_for(EQUITY)})  # deliberately leave the ETF class unregistered
    with pytest.raises(TickSizeUnavailable):
        benchmark.etf_buy_and_hold(ctx.view, ETF_ISINS[0], start=SESSIONS[0], end=SESSIONS[-1], capital=Decimal(50000),
                                   ticks=equity_only, schedules=ctx.schedules, pricing_basis=ctx.pricing_basis)


# ---- TRI -------------------------------------------------------------------------------------------
def test_tri_loads_only_by_path_and_matching_sha256(tmp_path):
    path = tmp_path / "tri.csv"
    digest = write_tri(path, SESSIONS)
    tri = load_tri(path, digest)
    assert isinstance(tri, TriSeries) and tri.sha256 == digest
    assert tri.period_return(SESSIONS[0], SESSIONS[-1]) > 0
    for bad in (
        load_tri(path, "0" * 64),  # bad hash
        load_tri(tmp_path / "missing.csv", digest),  # missing file
        load_tri(None, None),
        load_tri(path, None),
    ):
        assert isinstance(bad, TriUnavailable) and bad.label == TRI_LABEL == "TRI unavailable"
    junk = tmp_path / "junk.csv"
    junk.write_text("a,b\n1,2\n")
    assert isinstance(load_tri(junk, hashlib.sha256(junk.read_bytes()).hexdigest()), TriUnavailable)


def test_tri_unavailable_never_substitutes_a_price_index(tmp_path):
    inputs = study_inputs(tmp_path, tri=(tmp_path / "nope.csv", "0" * 64))
    study.register(inputs, hypothesis="h")
    report = study.run_research(inputs)
    assert report.benchmark.tri_label == "TRI unavailable" and not report.benchmark.tri_available
    assert report.benchmark.tri_sha256 is None
    assert all(unit.excess_vs_tri is None for unit in report.units)  # no substitute number appears
    assert report.benchmark.etf_stock_code == "ETFONE"


def test_written_report_and_fixtures_hold_no_tri_values(tmp_path):
    sessions = weekday_sessions(SESSION_START, 400)
    tri_path = tmp_path / "tri_private.csv"
    digest = write_tri(tri_path, sessions)
    inputs = study_inputs(tmp_path, tri=(tri_path, digest))
    study.register(inputs, hypothesis="h")
    report = study.run_research(inputs)
    assert report.benchmark.tri_available and report.benchmark.tri_sha256 == digest
    assert all(unit.excess_vs_tri is not None for unit in report.units)
    written = write_report(tmp_path / "out", report).read_text()
    for row in tri_path.read_text().splitlines()[1:]:
        level = row.split(",")[2]
        assert level not in written, "a TRI index level leaked into the report"
    assert "91234" not in written and "Total Returns Index" not in written
    assert digest in written  # path plus sha256 is the only TRI reference
    for fixture in FIXTURES.iterdir():
        text = fixture.read_text()
        assert "Total Returns Index" not in text and "NIFTY 500," not in text


@pytest.mark.parametrize("day", [date(2025, 4, 15), date(2026, 9, 4)])
def test_committed_etf_table_gap_is_unknown_not_defaulted(day):
    sessions = weekday_sessions(day, 2)
    ctx = make_context(make_rows(sessions, default_names(3) + etf_names()), FAR)
    with pytest.raises(TickSizeUnavailable):
        benchmark.etf_buy_and_hold(
            ctx.view, ETF_ISINS[0], start=sessions[0], end=sessions[-1], capital=Decimal(50000),
            ticks=ctx.ticks, schedules=ctx.schedules, pricing_basis=ctx.pricing_basis,
        )


@pytest.mark.parametrize("instrument_class, expected", [(EQUITY, "0.05"), (NON_GOLD_ETF, "0.01")])
def test_adapter_returns_the_public_resolution_with_version_provenance(instrument_class, expected):
    from costs.ticks import InstrumentClass, resolve_nse_cash_tick

    arguments = dict(session_date=SESSION_START, band_reference_price=Decimal("400"), series="EQ")
    resolution = resolve_nse_cash_tick(**arguments, instrument_class=InstrumentClass[instrument_class])
    tick = resolve_tick(tick_tables(), **arguments, instrument_class=instrument_class)
    assert tick == resolution.tick and tick.value == Decimal(expected)
    assert tick.source.endswith(f":{resolution.version_id}")
    assert tick.source_hash == resolution.version_hash


@pytest.mark.parametrize("instrument_class", [EQUITY, NON_GOLD_ETF])
@pytest.mark.parametrize("change", ["band", "version_hash", "source_id"])
def test_adapter_rejects_registration_that_differs_from_resolved_schedule(instrument_class, change):
    from dataclasses import replace

    table = load_default_tables().table_for(instrument_class)
    version = table.versions[-1]
    if change == "band":
        band = replace(version.bands[0], tick=Decimal("9"))
        version = replace(version, bands=(band,) + version.bands[1:])
        altered = replace(table, versions=table.versions[:-1] + (version,))
    elif change == "version_hash":
        altered = replace(table, versions=table.versions[:-1] + (replace(version, version_hash="0" * 64),))
    else:
        altered = replace(table, source_id="injected")
    with pytest.raises(TickSizeUnavailable, match="differs from the committed schedule"):
        TickTables({instrument_class: altered})

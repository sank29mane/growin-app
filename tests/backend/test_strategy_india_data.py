"""AC-6 (data mapping, D-05) and AC-7 (eligibility, D-07)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from costs.fills import BandUnavailable, PriceBand
from pilot_data import universe as pd_universe
from pilot_data.core import standard_caveats
from pilot_data.price_bands import BandObservation
from pilot_data.universe import UniverseDecision, UniversePolicy, UniverseResult

from strategy_india.data import (
    Bar,
    DatasetView,
    DividendEvents,
    UniverseEligibility,
    band_for_session,
    bar_from_row,
    load_dataset_rows,
    session_bar_for,
)
from strategy_india.engine import simulate_segment
from strategy_india.holdout import HoldoutRange
from strategy_india.signals import SignalTable
from strategy_india.ticks import EQUITY, resolve_tick

from test_strategy_india_support import (
    SESSION_START,
    StaticBands,
    default_names,
    etf_names,
    make_context,
    make_rows,
    params,
    sha,
    tick_tables,
    weekday_sessions,
)

SESSIONS = weekday_sessions(SESSION_START, 140)
HOLDOUT = HoldoutRange(SESSIONS[-20], SESSIONS[-1])


def _tick(price="100", day=date(2025, 6, 2)):
    return resolve_tick(tick_tables(), session_date=day, band_reference_price=Decimal(price),
                        instrument_class=EQUITY, series="EQ")


def _bar(**kw) -> Bar:
    base = dict(anchor_isin="A", isin="A", stock_code="A", session=date(2025, 6, 2), series="EQ",
                raw_open=Decimal("100"), raw_high=Decimal("103"), raw_low=Decimal("99"), raw_close=Decimal("101"),
                raw_volume=1000, adj_open=Decimal("50"), adj_high=Decimal("51"), adj_low=Decimal("49.5"),
                adj_close=Decimal("50.5"), quarantined=False)
    base.update(kw)
    return Bar(**base)


def _obs(status="fixed", percent="20", reason=None, kind="list"):
    return BandObservation(isin="A", session=date(2025, 6, 2), status=status, percent=Decimal(percent) if percent else None,
                           nse_symbol="A", series="EQ", source_kind=kind, source_sha256s=(sha("b"),), reason=reason)


# ---- AC-6 ----------------------------------------------------------------------------------------
def test_signals_read_adjusted_prices_and_fills_read_raw():
    # raw prices are flat, adjusted prices trend: only the adjusted series can produce a momentum signal
    sessions = weekday_sessions(SESSION_START, 40)
    bars = []
    for i, day in enumerate(sessions):
        bars.append(_bar(session=day, raw_open=Decimal("100"), raw_high=Decimal("100.5"), raw_low=Decimal("99.5"),
                         raw_close=Decimal("100"), adj_open=Decimal(50 + i), adj_high=Decimal(51 + i),
                         adj_low=Decimal(49 + i), adj_close=Decimal(50 + i)))
    view = DatasetView.from_rows(bars, holdout=HoldoutRange(date(2030, 1, 1), date(2030, 2, 1)))
    table = SignalTable(view, params(vol_adjusted=False), DividendEvents())
    assert table.raw_score("A", sessions[-1]) > Decimal("0.25")
    sb = session_bar_for(bars[-1], previous_raw_close=Decimal("100"), observation=_obs(), unavailable_reason=None,
                         tick=_tick())
    assert (sb.open, sb.high, sb.low, sb.close) == (Decimal("100"), Decimal("100.5"), Decimal("99.5"), Decimal("100"))
    assert sb.price_basis == "raw" and sb.volume == 1000


def test_quarantined_row_yields_no_signal():
    sessions = weekday_sessions(SESSION_START, 80)
    names = default_names(3)
    rows = make_rows(sessions, names, quarantined=[("INE000A01000", sessions[40])])
    assert next(r for r in rows if r.anchor_isin == "INE000A01000" and r.trade_date == sessions[40]).adjusted_quarantined
    view = DatasetView.from_rows(rows, holdout=HoldoutRange(date(2030, 1, 1), date(2030, 2, 1)))
    table = SignalTable(view, params(), DividendEvents())
    anchor = "INE000A01000"
    assert table.raw_score(anchor, sessions[39]) is not None
    assert table.raw_score(anchor, sessions[40]) is None  # the quarantined session itself
    for day in sessions[41:51]:  # any window that still touches it
        assert table.raw_score(anchor, day) is None
    assert table.raw_score(anchor, sessions[70]) is not None  # clean windows return
    assert bar_from_row(next(r for r in rows if r.trade_date == sessions[40] and r.anchor_isin == anchor)).adj_close is None
    assert table.raw_score("INE000A01001", sessions[45]) is not None


def test_fixed_band_maps_percent_and_previous_raw_close_to_a_price_band():
    band = band_for_session(_bar(), previous_raw_close=Decimal("100"), observation=_obs(percent="20"),
                            unavailable_reason=None, tick=_tick())
    assert isinstance(band, PriceBand) and band.category == "fixed"
    assert (band.lower, band.upper) == (Decimal("80.00"), Decimal("120.00"))
    assert band.effective_date == date(2025, 6, 2) and band.source_hash


def test_fixed_band_is_widened_outward_to_the_tick_grid():
    band = band_for_session(_bar(), previous_raw_close=Decimal("101.03"), observation=_obs(percent="10"),
                            unavailable_reason=None, tick=_tick())
    assert (band.lower, band.upper) == (Decimal("90.92"), Decimal("111.14"))  # 90.927 floored, 111.133 ceiled


def test_no_band_maps_to_a_no_band_price_band():
    band = band_for_session(_bar(), previous_raw_close=Decimal("100"), observation=_obs(status="no_band", percent=None),
                            unavailable_reason=None, tick=_tick())
    assert isinstance(band, PriceBand) and band.category == "no_band" and band.lower is None and band.upper is None


@pytest.mark.parametrize(
    "observation, reason, previous, expected",
    [
        (_obs(status="unknown", percent=None, reason="band_convention_unverified", kind=None), None, Decimal("100"),
         "band_convention_unverified"),
        (_obs(status="unknown", percent=None, reason=None, kind=None), None, Decimal("100"), "band_unknown"),
        (_obs(), "band_crosscheck_row_conflict", Decimal("100"), "band_crosscheck_row_conflict"),  # UnavailableBand row
        (None, None, Decimal("100"), "band_observation_missing"),
        (_obs(), None, None, "band_percent_or_previous_close_missing"),
        (_obs(percent=None), None, Decimal("100"), "band_percent_or_previous_close_missing"),
    ],
)
def test_every_unknown_maps_to_band_unavailable(observation, reason, previous, expected):
    band = band_for_session(_bar(), previous_raw_close=previous, observation=observation, unavailable_reason=reason,
                            tick=_tick())
    assert isinstance(band, BandUnavailable) and band.reason == expected


# ---- the real parquet adapter, on a synthetic published dataset ----------------------------------
def test_published_dataset_directory_loads_through_verify_dataset(tmp_path):
    from pilot_data.core import standard_caveats as caveats
    from pilot_data.dataset import DatasetManifest, _export, dataset_hash

    rows = sorted(make_rows(weekday_sessions(SESSION_START, 5), default_names(2)), key=lambda r: (r.anchor_isin, r.trade_date))
    digest = dataset_hash(rows)
    manifest = DatasetManifest(
        workspace="india", caveats=caveats(), dataset_sha256=digest, row_count=len(rows), anchor_count=2,
        window_start=rows[0].trade_date, window_end=rows[-1].trade_date, as_of=rows[-1].trade_date,
        crosscheck_run_id="r", report_sha256=sha("r"), targets_sha256=sha("t"), lineage_hashes={}, factor_set_hashes={},
        spans={}, quarantine_totals={}, rawness_counts={}, created_at_utc="2026-10-01T00:00:00+00:00",
    )
    published = _export(rows, manifest, tmp_path / "exports", "india")
    loaded_manifest, loaded = load_dataset_rows(tmp_path / "exports" / digest, expected_dataset_sha256=digest)
    assert loaded_manifest.dataset_sha256 == published.dataset_sha256 == digest
    assert len(loaded) == len(rows)
    from strategy_india.errors import DataError

    with pytest.raises(DataError):
        load_dataset_rows(tmp_path / "exports" / digest, expected_dataset_sha256=sha("other"))


def _published(tmp_path, rows, events):
    from pilot_data.core import standard_caveats as caveats
    from pilot_data.dataset import DatasetManifest, DividendAmountUnknownEvent, _export, dataset_hash

    from inspect import signature

    rows = sorted(rows, key=lambda r: (r.anchor_isin, r.trade_date))
    event_map = {a: tuple(DividendAmountUnknownEvent(event_id=i, ex_date=d) for i, d in evs)
                 for a, evs in events.items()}
    # #542 is approved but not merged into this branch yet. Both APIs are exercised.
    kwargs = {"events": event_map} if "events" in signature(dataset_hash).parameters else {}
    digest = dataset_hash(rows, **kwargs)
    manifest = DatasetManifest(
        workspace="india", caveats=caveats(), dataset_sha256=digest, row_count=len(rows), anchor_count=2,
        window_start=rows[0].trade_date, window_end=rows[-1].trade_date, as_of=rows[-1].trade_date,
        crosscheck_run_id="r", report_sha256=sha("r"), targets_sha256=sha("t"), lineage_hashes={}, factor_set_hashes={},
        spans={}, quarantine_totals={}, rawness_counts={}, created_at_utc="2026-10-01T00:00:00+00:00",
        dividend_amount_unknown_events=event_map,
    )
    _export(rows, manifest, tmp_path / "exports", "india")
    return tmp_path / "exports" / digest


def test_dividend_events_are_built_from_the_manifest_and_cross_checked_against_row_tags(tmp_path):
    from pilot_data.core import PilotDataError

    from strategy_india.data import events_from_manifest

    sessions = weekday_sessions(SESSION_START, 12)
    ex = sessions[6]
    base = make_rows(sessions, default_names(2))
    tagged = [r.model_copy(update={"dividend_amount_unknown": r.anchor_isin == "INE000A01000" and r.trade_date <= ex,
                                   "dividend_amount_unknown_ex_date": r.anchor_isin == "INE000A01000" and r.trade_date == ex})
              for r in base]
    path = _published(tmp_path, tagged, {"INE000A01000": [("EV1", ex)]})
    manifest, rows = load_dataset_rows(path)
    events = events_from_manifest(manifest)
    assert [(e.anchor_isin, e.event_id, e.ex_date) for e in events.all()] == [("INE000A01000", "EV1", ex)]
    assert events.ex_dates("INE000A01000") == frozenset({ex}) and events.sealed_sha256() != DividendEvents().sealed_sha256()
    # a dataset with no tagged events gives an empty list, which seals as empty
    plain = _published(tmp_path / "plain", base, {})
    assert not events_from_manifest(load_dataset_rows(plain)[0])
    # a manifest event with no tagged row, or a flagged ex-date the manifest does not list, is refused by 59 verify
    with pytest.raises(PilotDataError):
        load_dataset_rows(_published(tmp_path / "bad1", base, {"INE000A01000": [("EV1", ex)]}))
    other = [r.model_copy(update={"dividend_amount_unknown_ex_date": r.anchor_isin == "INE000A01000" and r.trade_date == ex})
             for r in base]
    with pytest.raises(PilotDataError):
        load_dataset_rows(_published(tmp_path / "bad2", other, {}))


# ---- AC-7 ----------------------------------------------------------------------------------------
def _universe_result(as_of: date, eligible: set[str], names, smallcap=None) -> UniverseResult:
    decisions = tuple(
        UniverseDecision(
            anchor_isin=n.anchor, isin_on_date=n.anchor, stock_code=n.code, eligible=n.anchor in eligible,
            reasons=() if n.anchor in eligible else ("liquidity_unknown",), close=Decimal("500"),
            median_traded_value=None, eligibility_median_traded_value=None, known_sessions=60, unknown_sessions=0,
            excluded_prelisting_sessions=0, smallcap_class=(smallcap or {}).get(n.anchor, "not_small"),
        )
        for n in names
    )
    return UniverseResult(
        workspace="india", caveats=standard_caveats(), as_of=as_of, mode="research", policy_sha256=sha("p"),
        decisions=decisions, eligible_isins=tuple(sorted(eligible)), exclusions_by_reason={}, input_hashes={},
        result_sha256=sha(f"u-{as_of}"),
    )


def test_each_rebalance_calls_evaluate_universe_with_as_of_equal_to_the_decision_date(monkeypatch):
    names = default_names(8)
    rows = make_rows(SESSIONS, names + etf_names())
    seen: list[date] = []

    def spy(store, *, as_of, **kwargs):
        seen.append(as_of)
        assert kwargs["workspace"] == "india" and kwargs["mode"] == "research"
        return _universe_result(as_of, {n.anchor for n in names}, names)

    monkeypatch.setattr(pd_universe, "evaluate_universe", spy)
    eligibility = UniverseEligibility(object(), object(), UniversePolicy())
    ctx = make_context(rows, HOLDOUT, eligibility=eligibility)
    dev = ctx.view.sessions()
    result = simulate_segment(ctx, sessions=dev, scenario=ctx.scenarios.gate(), slope=Decimal("0.01"), regime_cash=None,
                              mode="base", fold="x")
    every_other = dev[:-1][:: ctx.params.rebalance_every]
    assert seen == every_other  # one call per rebalance decision date, none later than the decision
    assert all(day <= dev[-1] for day in seen) and max(seen) < dev[-1]
    assert result.entries > 0


def test_a_late_as_of_from_the_universe_is_refused(monkeypatch):
    names = default_names(2)
    monkeypatch.setattr(pd_universe, "evaluate_universe",
                        lambda store, *, as_of, **kw: _universe_result(date(2030, 1, 1), set(), names))
    from costs.core import LookaheadError

    with pytest.raises(LookaheadError):
        UniverseEligibility(object(), object(), UniversePolicy()).snapshot(date(2025, 6, 2))


def test_unknown_eligibility_excludes_the_name(monkeypatch):
    names = default_names(8)
    rows = make_rows(SESSIONS, names + etf_names())
    top = names[0].anchor  # the strongest trend, so it would be bought first if eligible
    monkeypatch.setattr(pd_universe, "evaluate_universe",
                        lambda store, *, as_of, **kw: _universe_result(as_of, {n.anchor for n in names[1:]}, names))
    ctx = make_context(rows, HOLDOUT, params_obj=params(min_universe_for_entry=5),
                       eligibility=UniverseEligibility(object(), object(), UniversePolicy()))
    dev = ctx.view.sessions()
    res = simulate_segment(ctx, sessions=dev, scenario=ctx.scenarios.gate(), slope=Decimal("0.01"), regime_cash=None,
                           mode="base", fold="x")
    touched = {t.anchor_isin for t in res.closed} | {p.anchor_isin for p in res.open_positions} | {a.anchor_isin for a in res.attempts}
    assert top not in touched and res.entries > 0


def test_small_cap_exposure_stays_within_thirty_percent():
    names = default_names(8)
    rows = make_rows(SESSIONS, names + etf_names())
    small = {n.anchor: "small" for n in names}
    ctx = make_context(rows, HOLDOUT, smallcap=small)
    dev = ctx.view.sessions()
    capped = simulate_segment(ctx, sessions=dev, scenario=ctx.scenarios.gate(), slope=Decimal("0.01"), regime_cash=None,
                              mode="base", fold="x")
    assert capped.smallcap_rejections > 0
    assert max(v for _, v in capped.exposure) <= Decimal("0.34")  # one 10k position of a 50k book, plus drift
    free = simulate_segment(make_context(rows, HOLDOUT), sessions=dev, scenario=ctx.scenarios.gate(),
                            slope=Decimal("0.01"), regime_cash=None, mode="base", fold="x")
    assert max(v for _, v in free.exposure) > Decimal("0.6")
    assert free.smallcap_rejections == 0


def test_a_valid_dataset_with_a_different_hash_than_the_registered_one_is_refused_before_any_evaluation(tmp_path):
    from strategy_india import study
    from strategy_india.errors import DataError, StrategyIndiaError
    from strategy_india.registry import Registry

    from test_strategy_india_support import registration_record

    sessions = weekday_sessions(SESSION_START, 12)
    ex = sessions[6]
    base = make_rows(sessions, default_names(2))
    tagged = [r.model_copy(update={"dividend_amount_unknown": r.anchor_isin == "INE000A01000" and r.trade_date <= ex,
                                   "dividend_amount_unknown_ex_date": r.anchor_isin == "INE000A01000" and r.trade_date == ex})
              for r in base]
    registered_dir = _published(tmp_path / "a", tagged, {"INE000A01000": [("EV1", ex)]})
    # the same data edited and re-hashed: valid in itself, published in a new directory under a new hash
    shifted = [r.model_copy(update={"raw_volume": r.raw_volume + 1}) if r.trade_date == sessions[2] else r for r in tagged]
    other_dir = _published(tmp_path / "b", shifted, {"INE000A01000": [("EV1", ex)]})
    assert registered_dir.name != other_dir.name
    manifest, _ = load_dataset_rows(registered_dir)  # both verify on their own
    load_dataset_rows(other_dir)
    registry = Registry(tmp_path / "private" / "registry.jsonl")
    registry.register(registration_record(dataset_sha256=manifest.dataset_sha256))
    config = {"registry": str(registry.path), "dataset_dir": str(registered_dir)}
    assert study.load_bound_dataset(config, bind_to_registration=True)[0].dataset_sha256 == manifest.dataset_sha256
    with pytest.raises(DataError, match="dataset_sha256"):
        study.load_bound_dataset({**config, "dataset_dir": str(other_dir)}, bind_to_registration=True)
    with pytest.raises(StrategyIndiaError) as err:  # the configured hash must agree with the registered one as well
        study.load_bound_dataset({**config, "dataset_sha256": sha("x")}, bind_to_registration=True)
    assert err.value.code == "dataset_mismatch"
    # a first registration has nothing registered, so it can load a new dataset
    assert study.load_bound_dataset({**config, "dataset_dir": str(other_dir)}, bind_to_registration=False)[0].dataset_sha256 \
        == load_dataset_rows(other_dir)[0].dataset_sha256
    with pytest.raises(DataError):  # an explicit hash in the config is enforced even for a registration
        study.load_bound_dataset({**config, "dataset_dir": str(other_dir), "dataset_sha256": manifest.dataset_sha256},
                                 bind_to_registration=False)


@pytest.mark.parametrize("event_id, expected", [
    (None, "c7f4f443361b25c792bc60db26915f7d42149ba286a7c8a380d2292f9b47752c"),
    ("EV1", "449c92b11a31d10bb8dc6b43a242b2694b3b0ce159072ee02acd9bd5cb4df8b5"),
    ("EV2", "2787d414fd5d54328b5f7f6671864f8e7634fd87ea12d2a609bed0d9d5ce3624"),
])
def test_dataset_digest_matches_golden_hashes_from_approved_pr542(event_id, expected):
    # Golden values re-derived from main 101d3b4's event-bound dataset_hash.
    from strategy_india.data import DividendUnknownEvent, dataset_digest

    rows = make_rows(weekday_sessions(SESSION_START, 5), default_names(2))
    events = DividendEvents([] if event_id is None else [
        DividendUnknownEvent("INE000A01000", event_id, date(2025, 4, 29))
    ])
    assert dataset_digest(rows, events) == expected
    assert dataset_digest(reversed(rows), events) == expected


@pytest.mark.parametrize("quarantined", [False, True])
def test_d20_row_tags_are_exact_through_last_ex_date_including_quarantined_rows(quarantined):
    from strategy_india.data import DividendUnknownEvent, check_events_against_rows
    from strategy_india.errors import DataError
    from test_strategy_india_support import tag_rows

    sessions = weekday_sessions(SESSION_START, 5)
    events = DividendEvents([DividendUnknownEvent("INE000A01000", "EV1", sessions[1]),
                             DividendUnknownEvent("INE000A01000", "EV2", sessions[3])])
    rows = tag_rows(make_rows(sessions, default_names(2)), events)
    rows = [row.model_copy(update={"adjusted_quarantined": quarantined}) for row in rows]
    check_events_against_rows(events, rows)
    for row in rows:
        for field in ("dividend_amount_unknown", "dividend_amount_unknown_ex_date"):
            wrong = row.model_copy(update={field: not getattr(row, field)})
            with pytest.raises(DataError) as caught:
                check_events_against_rows(events, [wrong])
            assert caught.value.code == "events_mismatch"

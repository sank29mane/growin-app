"""A5: the benchmark ETF tick inferred from its own prices in the window the ETF schedule leaves uncovered.

All prices are synthetic. The pure rule lives in ``costs.tick_inference``; the resolver wiring and the seal
live in ``strategy_india.ticks``.
"""

from __future__ import annotations

import random
import re
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from costs import tick_inference as ti
from costs.core import InputError, TickSizeUnavailable
from costs.ticks import InstrumentClass, committed_tick_table

from strategy_india.registry import canonical_sha256
from strategy_india.ticks import EQUITY, NON_GOLD_ETF, TickTables, infer_benchmark_ticks, load_default_tables, resolve_tick

D = Decimal
WINDOW = (date(2025, 4, 15), date(2026, 9, 6))
ETF = "INF000000ETF1"
OTHER_ETF = "INF000000ETF2"


def weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def observations(n: int = 140, *, grid: str = "0.01", seed: int = 7, per_session: int = 4,
                 start: date = date(2025, 6, 2)) -> list[ti.TickObservation]:
    """n sessions of per_session prices around Rs 250, each a multiple of the grid (0.01 or 0.05)."""
    rng = random.Random(seed)
    steps = 100 if grid == "0.01" else 20  # grid steps per rupee
    return [
        ti.TickObservation(day, tuple(D(rng.randint(25000 * steps // 100, 27500 * steps // 100)) / steps
                                      for _ in range(per_session)))
        for day in weekdays(start, n)
    ]


def infer(obs: list[ti.TickObservation], security: str = ETF) -> ti.InferredTickSource:
    return ti.infer_tick(security=security, window_start=WINDOW[0], window_end=WINDOW[1], observations=obs)


def with_price(obs: list[ti.TickObservation], index: int, value) -> list[ti.TickObservation]:
    out = list(obs)
    prices = list(out[index].prices)
    prices[0] = value
    out[index] = ti.TickObservation(out[index].session, tuple(prices))
    return out


# ---- the rule -------------------------------------------------------------------------------------------
def test_the_committed_etf_table_leaves_exactly_the_a5_window_uncovered():
    assert ti.uncovered_windows(committed_tick_table(InstrumentClass.NON_GOLD_ETF)) == (WINDOW,)


def test_synthetic_0_01_prices_infer_0_01():
    result = infer(observations(grid="0.01"))
    assert result.status == ti.INFERRED and result.tick == D("0.01")
    assert result.provenance["prices_off_0_05"] > 0 and result.provenance["prices_off_0_01"] == 0


def test_synthetic_0_05_prices_infer_0_05():
    result = infer(observations(grid="0.05"))
    assert result.status == ti.INFERRED and result.tick == D("0.05")
    assert result.provenance["prices_off_0_05"] == 0 and result.provenance["sessions"] == 140


def test_one_price_off_the_0_05_grid_among_a_long_aligned_run_is_a_tick_change_not_0_01():
    # A genuine 0.01 security does not print 70 sessions on the 0.05 grid, so one off-grid print inside a long
    # aligned run says the tick moved inside the window. That refuses; it never resolves to the optimistic 0.01.
    obs = with_price(observations(grid="0.05"), 70, D("250.03"))
    result = infer(obs)
    assert result.status == ti.UNAVAILABLE and "mixed sample" in result.reason


def test_off_grid_prints_in_a_short_sample_that_is_otherwise_aligned_still_need_the_sample_floor():
    obs = with_price(observations(30, grid="0.05"), 5, D("250.03"))
    assert "sample too small" in infer(obs).reason


def test_a_sample_with_too_few_sessions_refuses():
    # 99 sessions of 5 prices: 495 prices and plenty of distinct values, so only the session floor trips.
    result = infer(observations(99, grid="0.05", per_session=5))
    assert result.status == ti.UNAVAILABLE and result.tick is None and "sample too small" in result.reason
    assert infer(observations(100, grid="0.05", per_session=5)).tick == D("0.05")  # the edge is exact


def test_a_sample_with_too_few_prices_refuses():
    # 120 sessions of 3 prices: 360 prices, so only the price floor trips.
    result = infer(observations(120, grid="0.05", per_session=3))
    assert result.status == ti.UNAVAILABLE and "sample too small" in result.reason
    assert infer(observations(100, grid="0.05", per_session=4)).tick == D("0.05")  # 400 prices is enough


def test_a_flat_or_frozen_price_series_refuses_even_when_it_is_long():
    pool = [D("250.05") + D("0.05") * n for n in range(10)]  # 10 distinct prices over 480 samples
    rng = random.Random(3)
    obs = [ti.TickObservation(day, tuple(rng.choice(pool) for _ in range(4))) for day in weekdays(date(2025, 6, 2), 120)]
    result = infer(obs)
    assert result.status == ti.UNAVAILABLE and result.provenance["distinct_prices"] <= 10
    assert "sample too small" in result.reason


def test_a_price_off_the_0_01_grid_refuses_instead_of_inferring_0_01():
    result = infer(with_price(observations(grid="0.05"), 70, D("250.005")))
    assert result.status == ti.UNAVAILABLE and "not multiples of 0.01" in result.reason
    assert result.provenance["prices_off_0_01"] == 1
    assert infer(with_price(observations(grid="0.01"), 5, D("250.0001"))).status == ti.UNAVAILABLE


@pytest.mark.parametrize("bad", [None, 250.05, 0, D("-1"), D("NaN"), "250.05"])
def test_a_missing_or_unusable_price_refuses(bad):
    result = infer(with_price(observations(grid="0.05"), 70, bad))
    assert result.status == ti.UNAVAILABLE and "missing, non-Decimal or non-positive price" in result.reason
    assert result.provenance["input_rows_sha256"]  # the refusal is sealed with its inputs too


def test_a_session_with_no_prices_refuses():
    obs = observations(grid="0.05")
    obs[10] = ti.TickObservation(obs[10].session, ())
    assert infer(obs).status == ti.UNAVAILABLE


def test_a_duplicated_session_refuses():
    obs = observations(grid="0.05")
    assert infer(obs + [obs[3]]).status == ti.UNAVAILABLE


def corrupt(obs: list[ti.TickObservation], count: int) -> list[ti.TickObservation]:
    """A 0.05 sample with ``count`` prints knocked off the grid, spread evenly over the sample."""
    out = list(obs)
    for n in range(count):
        out = with_price(out, n * len(out) // count, D("250.03"))
    return out


@pytest.mark.parametrize("sessions, bad", [(100, 4), (300, 14), (100, 1), (100, 30)])
def test_a_few_corrupted_prints_on_a_0_05_sample_never_flip_it_to_0_01(sessions, bad):
    # 4 of 400 and 14 of 1200 prices: 1% off the grid. Spacing of 20 or fewer sessions used to reset the
    # aligned-run counter and return the optimistic 0.01. A real 0.01 ETF has about 80% off the grid.
    result = infer(corrupt(observations(sessions, grid="0.05"), bad))
    assert result.status == ti.UNAVAILABLE and result.tick is None and "mixed sample" in result.reason


def test_the_exact_review_cases_refuse():
    assert infer(corrupt(observations(100, grid="0.05"), 4)).provenance["prices"] == 400
    assert infer(corrupt(observations(300, grid="0.05"), 14)).provenance["prices"] == 1200


def test_a_tick_change_inside_the_window_refuses():
    # 60 sessions that sit on the 0.05 grid, then 60 that prove 0.01: the window is not one regime.
    mixed = observations(60, grid="0.05", start=date(2025, 6, 2)) + observations(60, grid="0.01", start=date(2025, 8, 26))
    result = infer(mixed)
    assert result.status == ti.UNAVAILABLE and "mixed sample" in result.reason


@pytest.mark.parametrize("tail", [20, 10])
def test_a_tick_change_inside_the_last_or_first_sessions_refuses(tail):
    # A 0.01 sample whose last (or first) `tail` sessions sit on the 0.05 grid: overall it is still about 70%
    # off the grid, so only the block rule can see it. (A tick change in fewer than about 8 sessions leaves
    # the worst 20-session block above half off the grid; that is the limit of a 20-session block.)
    obs = observations(140, grid="0.01")
    late = obs[:-tail] + observations(tail, grid="0.05", seed=11, start=obs[-tail].session)
    early = observations(tail, grid="0.05", seed=11, start=obs[0].session) + obs[tail:]
    for sample in (late, early):
        result = infer(sample)
        assert result.status == ti.UNAVAILABLE and "mixed sample" in result.reason
        assert result.provenance["prices_off_0_05"] > 0.6 * result.provenance["prices"]


def test_a_clean_0_01_sample_infers_0_01():
    obs = observations(300, grid="0.01", per_session=4)
    result = infer(obs)
    off = result.provenance["prices_off_0_05"] / result.provenance["prices"]
    assert 0.7 < off < 0.9  # the synthetic sample looks like a real 0.01 security
    assert result.tick == D("0.01")


def test_a_few_chance_aligned_sessions_do_not_stop_a_genuine_0_01_inference():
    obs = observations(160, grid="0.01")
    for i in (30, 70, 71, 120):  # a session that happens to land entirely on the 0.05 grid
        obs[i] = ti.TickObservation(obs[i].session, tuple(round(p * 20) / D(20) for p in obs[i].prices))
    assert infer(obs).tick == D("0.01")


def test_a_20_session_run_on_the_0_05_grid_inside_a_0_01_sample_refuses():
    obs = observations(160, grid="0.01")
    for i in range(40, 60):
        obs[i] = ti.TickObservation(obs[i].session, tuple(round(p * 20) / D(20) for p in obs[i].prices))
    result = infer(obs)
    assert result.status == ti.UNAVAILABLE and "mixed sample" in result.reason
    assert result.provenance["worst_block_off_0_05"] == "0/80"


def mixed_session(rng: random.Random, off: int, per_session: int = 4) -> tuple[Decimal, ...]:
    """One session of ``per_session`` prices, exactly ``off`` of them on the 0.01 grid but off the 0.05 grid."""
    prices = []
    for n in range(per_session):
        base = D(rng.randint(5000, 5500)) / 20  # a multiple of 0.05
        prices.append(base + D(rng.randint(1, 4)) / 100 if n < off else base)
    return tuple(prices)


def shaped(off_per_session: list[int], seed: int = 5) -> list[ti.TickObservation]:
    rng = random.Random(seed)
    days = weekdays(date(2025, 6, 2), len(off_per_session))
    return [ti.TickObservation(day, mixed_session(rng, off)) for day, off in zip(days, off_per_session)]


def test_the_overall_half_off_grid_rule_refuses_when_every_block_sits_exactly_at_half():
    # Alternating 10 clean and 10 corrupted sessions: every window of 20 sessions holds exactly 10 of each, so
    # every block is at exactly 50%, yet the 110 sessions start and end clean, so the whole sample is 45%.
    # Only the overall check can refuse it.
    off = ([0] * 10 + [4] * 10) * 5 + [0] * 10
    result = infer(shaped(off))
    assert result.provenance["worst_block_off_0_05"] == "40/80"  # no block is below the 50% line
    assert result.provenance["prices_off_0_05"] * 100 < 50 * result.provenance["prices"]  # 200 of 440: 45%
    assert result.status == ti.UNAVAILABLE and result.tick is None and result.category == ti.CATEGORY_MIXED


@pytest.mark.parametrize("sessions", [140, 147, 153])
def test_a_tick_change_confined_to_the_final_block_refuses(sessions):
    # A 0.01 security whose last 20 sessions sit on the 0.05 grid. Overall it is still about 70% off the grid
    # and every window that starts earlier still holds some 0.01 sessions, so only the window that ends at the
    # last session can refuse it, whatever the length of the sample.
    obs = observations(sessions, grid="0.01")
    obs[-20:] = observations(20, grid="0.05", seed=11, start=obs[-20].session)
    result = infer(obs)
    assert result.provenance["prices_off_0_05"] * 100 >= 50 * result.provenance["prices"]
    assert result.provenance["worst_block_off_0_05"] == "0/80"
    assert result.status == ti.UNAVAILABLE and result.category == ti.CATEGORY_MIXED


def test_the_half_off_grid_boundary_is_exactly_40_of_80_per_block_and_50_percent_overall():
    steady = [2] * 120  # 2 of 4 prices off the grid in every session: every block 40/80, overall 240/480
    at_half = infer(shaped(steady))
    assert at_half.provenance["worst_block_off_0_05"] == "40/80" and at_half.provenance["prices_off_0_05"] == 240
    assert at_half.status == ti.INFERRED and at_half.tick == D("0.01")
    # One session short by one print, another long by one (far away): overall still 240/480, one block 39/80.
    short = list(steady)
    short[5], short[100] = 1, 3
    refused = infer(shaped(short))
    assert refused.provenance["worst_block_off_0_05"] == "39/80" and refused.provenance["prices_off_0_05"] == 240
    assert refused.status == ti.UNAVAILABLE and refused.category == ti.CATEGORY_MIXED
    # And one print short overall (239/480, no block below 40/80 elsewhere): refuses on the overall rule.
    overall = list(steady)
    overall[5] = 1
    assert infer(shaped(overall)).status == ti.UNAVAILABLE


def test_a_sample_with_every_price_off_the_grid_seals_its_real_worst_block():
    result = infer(shaped([4] * 120))
    assert result.tick == D("0.01")
    assert result.provenance["worst_block_off_0_05"] == "80/80"  # not a placeholder 1/1
    assert infer(observations(140, grid="0.05")).provenance["worst_block_off_0_05"] == "0/80"


def test_the_method_name_is_pinned():
    assert ti.METHOD == "price-grid-inference/2"
    assert infer(observations(grid="0.01")).provenance["method"] == "price-grid-inference/2"


def _category_samples() -> dict[str, list[ti.TickObservation]]:
    off_grid = with_price(observations(grid="0.05"), 70, D("250.005"))
    missing = with_price(observations(grid="0.05"), 70, None)
    mixed = with_price(observations(grid="0.05"), 70, D("250.03"))
    duplicate = observations(grid="0.05") + [observations(grid="0.05")[3]]
    return {ti.CATEGORY_TOO_SMALL: observations(40, grid="0.05"), ti.CATEGORY_MIXED: mixed,
            ti.CATEGORY_OFF_GRID: off_grid, ti.CATEGORY_MISSING: missing, ti.CATEGORY_DUPLICATE: duplicate}


@pytest.mark.parametrize("category", ti.CATEGORIES)
def test_every_refusal_category_reaches_the_operator_with_no_digit_from_the_sample(category):
    sample = _category_samples()[category]
    result = infer(sample)
    assert result.status == ti.UNAVAILABLE and result.category == category
    assert result.provenance["category"] == category
    assert result.provenance["reason"] == result.reason  # the detailed text (with counts) stays sealed
    tables = tables_with(sample)
    shown = [result.category, tables.uncovered_reason(NON_GOLD_ETF, date(2025, 9, 15), security=ETF)]
    with pytest.raises(TickSizeUnavailable) as err:
        etf_tick(tables, date(2025, 9, 15))
    shown.append(str(err.value))
    assert shown[1] == category and category in shown[2]
    sample_dates = [o.session.isoformat() for o in sample]
    for text in shown:
        # Constants are not sample data: the category names (the 0.01 grid), the security and the window dates.
        residue = text
        for constant in (*ti.CATEGORIES, ETF, *(d.isoformat() for d in WINDOW)):
            residue = residue.replace(constant, "")
        assert not re.search(r"\d", residue), text
        assert not any(d in text for d in sample_dates), text


def test_rows_outside_the_window_are_not_inputs():
    inside = observations(140, grid="0.05", start=date(2025, 6, 2))
    junk = [ti.TickObservation(date(2025, 4, 14), (D("1.003"),) * 4), ti.TickObservation(date(2026, 9, 7), (None,) * 4)]
    assert infer(inside + junk).provenance_sha256 == infer(inside).provenance_sha256
    assert infer(inside + junk).tick == D("0.05")


# ---- provenance and the hash ------------------------------------------------------------------------------------
def test_provenance_records_method_window_counts_tick_and_input_hash():
    result = infer(observations(grid="0.01"))
    p = result.provenance
    assert p["method"] == ti.METHOD and p["security"] == ETF and p["series"] == "EQ"
    assert (p["window_start"], p["window_end"]) == ("2025-04-15", "2026-09-06")
    assert p["status"] == "inferred" and p["tick"] == "0.01"
    assert p["sessions"] == 140 and p["prices"] == 560 and p["distinct_prices"] > 50
    assert p["thresholds"] == {"min_sessions": 100, "min_prices": 400, "min_distinct_prices": 50,
                               "block_sessions": 20, "min_off_0_05_percent": 50}
    assert len(p["input_rows_sha256"]) == 64 and len(result.provenance_sha256) == 64


def test_changed_input_rows_change_the_provenance_hash():
    base = observations(grid="0.01")
    one = infer(base)
    nudged = infer(with_price(base, 70, base[70].prices[0] + D("0.01")))
    assert nudged.provenance["input_rows_sha256"] != one.provenance["input_rows_sha256"]
    assert nudged.provenance_sha256 != one.provenance_sha256
    assert infer(list(reversed(base))).provenance_sha256 == one.provenance_sha256  # input order is not an input
    same_value_other_scale = [ti.TickObservation(o.session, tuple(p.quantize(D("0.0001")) for p in o.prices)) for o in base]
    assert infer(same_value_other_scale).provenance_sha256 == one.provenance_sha256  # 250.10 and 250.1 are one price


def test_the_security_and_window_are_part_of_the_provenance_hash():
    obs = observations(grid="0.01")
    assert infer(obs, OTHER_ETF).provenance_sha256 != infer(obs, ETF).provenance_sha256


def test_a_source_must_match_its_provenance():
    good = infer(observations(grid="0.05"))
    with pytest.raises(TickSizeUnavailable):
        replace(good, tick=D("0.01"))
    with pytest.raises(TickSizeUnavailable):
        replace(good, provenance_sha256="0" * 64)
    with pytest.raises(TickSizeUnavailable):
        replace(good, security=OTHER_ETF)


# ---- the resolver: window, security and the committed rows -------------------------------------------------------------
def rows_for(isin: str, obs: list[ti.TickObservation], series: str = "EQ"):
    class Row:
        def __init__(self, o: ti.TickObservation) -> None:
            self.anchor_isin, self.series, self.trade_date = isin, series, o.session
            self.raw_open, self.raw_high, self.raw_low, self.raw_close = o.prices

    return [Row(o) for o in obs]


def tables_with(obs: list[ti.TickObservation], isin: str = ETF) -> TickTables:
    return load_default_tables(rows=rows_for(isin, obs), benchmark_isins=(isin,))


def etf_tick(tables: TickTables, day: date, security: str | None = ETF):
    return resolve_tick(tables, session_date=day, band_reference_price=D("250"), instrument_class=NON_GOLD_ETF,
                        series="EQ", security=security)


def test_the_benchmark_resolves_inside_the_window_from_the_inferred_source():
    tables = tables_with(observations(grid="0.05"))
    tick = etf_tick(tables, date(2025, 9, 15))
    assert tick.value == D("0.05") and tick.source.startswith(ti.METHOD) and ETF in tick.source
    (record,) = tables.inference_provenance()
    assert tick.source_hash == record["provenance_sha256"]
    assert (tick.effective_from, tick.effective_to) == WINDOW
    assert tables.covers(NON_GOLD_ETF, date(2025, 9, 15), series="EQ", security=ETF)


@pytest.mark.parametrize("day, expected_source", [(date(2025, 4, 14), "nse-cash-non-gold-etf-ticks"),
                                                  (date(2026, 9, 7), "nse-cash-non-gold-etf-ticks")])
def test_a_date_outside_the_window_still_resolves_from_the_committed_rows(day, expected_source):
    tables = tables_with(observations(grid="0.05"))  # inferred 0.05, schedule says 0.01 on both edges
    tick = etf_tick(tables, day)
    assert tick.value == D("0.01") and tick.source.startswith(expected_source)
    assert etf_tick(tables_with(observations(grid="0.05")), day, security=None) == tick
    assert tables.covers(NON_GOLD_ETF, day, series="EQ", security=ETF)


def test_dates_the_window_does_not_hold_and_the_schedule_does_not_cover_stay_uncovered():
    tables = tables_with(observations(grid="0.05"))
    assert not tables.covers(NON_GOLD_ETF, date(2020, 12, 31), series="EQ", security=ETF)
    with pytest.raises(TickSizeUnavailable):
        etf_tick(tables, date(2020, 12, 31))


def test_a_non_benchmark_etf_is_still_uncovered():
    tables = tables_with(observations(grid="0.05"))  # inference exists for ETF only
    day = date(2025, 9, 15)
    for other in (OTHER_ETF, None, "", "INF999999999"):
        assert not tables.covers(NON_GOLD_ETF, day, series="EQ", security=other)
        with pytest.raises(TickSizeUnavailable):
            etf_tick(tables, day, security=other)
    assert not tables.covers(NON_GOLD_ETF, day, series="EQ")  # no security named, nothing inferred


def test_the_plain_default_tables_keep_the_gap_uncovered():
    tables = load_default_tables()
    assert not tables.covers(NON_GOLD_ETF, date(2025, 9, 15), series="EQ", security=ETF)
    with pytest.raises(TickSizeUnavailable):
        etf_tick(tables, date(2025, 9, 15))


def test_an_unavailable_inference_keeps_the_gap_uncovered_and_says_why():
    tables = tables_with(observations(40, grid="0.05"))
    day = date(2025, 9, 15)
    assert not tables.covers(NON_GOLD_ETF, day, series="EQ", security=ETF)
    assert "sample too small" in tables.uncovered_reason(NON_GOLD_ETF, day, security=ETF)
    with pytest.raises(TickSizeUnavailable, match="sample too small"):
        etf_tick(tables, day)


def test_the_equity_class_never_uses_an_inferred_source():
    tables = tables_with(observations(grid="0.05"))
    assert not tables.covers(EQUITY, date(2020, 12, 31), series="EQ", security=ETF)
    tick = resolve_tick(tables, session_date=date(2025, 9, 15), band_reference_price=D("250"),
                        instrument_class=EQUITY, series="EQ", security=ETF)
    assert not tick.source.startswith(ti.METHOD)


def test_only_series_eq_rows_feed_the_inference_and_only_eq_resolves():
    obs = observations(140, grid="0.05")
    tables = load_default_tables(rows=rows_for(ETF, obs, series="BE"), benchmark_isins=(ETF,))
    assert tables.uncovered_reason(NON_GOLD_ETF, date(2025, 9, 15), security=ETF) == ti.CATEGORY_TOO_SMALL
    (record,) = tables.inference_provenance()
    assert record["sessions"] == 0 and "0 sessions" in record["reason"]  # the counts live in the sealed record
    ok = tables_with(obs)
    assert not ok.covers(NON_GOLD_ETF, date(2025, 9, 15), series="BE", security=ETF)
    with pytest.raises(TickSizeUnavailable):
        resolve_tick(ok, session_date=date(2025, 9, 15), band_reference_price=D("250"),
                     instrument_class=NON_GOLD_ETF, series="BE", security=ETF)


def test_a_non_positive_reference_price_is_refused_on_the_inferred_path_too():
    with pytest.raises(InputError):
        resolve_tick(tables_with(observations(grid="0.05")), session_date=date(2025, 9, 15),
                     band_reference_price=D("0"), instrument_class=NON_GOLD_ETF, series="EQ", security=ETF)


def test_an_inferred_source_cannot_sit_on_dates_the_schedule_covers():
    base = load_default_tables()
    bad = ti.infer_tick(security=ETF, window_start=date(2025, 1, 1), window_end=date(2025, 12, 31),
                        observations=observations(grid="0.05", start=date(2025, 4, 1)))
    with pytest.raises(TickSizeUnavailable, match="leaves uncovered"):
        TickTables({NON_GOLD_ETF: base.table_for(NON_GOLD_ETF), EQUITY: base.table_for(EQUITY)}, inferred=[bad])
    narrower = ti.infer_tick(security=ETF, window_start=date(2025, 4, 15), window_end=date(2026, 9, 7),
                             observations=observations(grid="0.05"))
    with pytest.raises(TickSizeUnavailable, match="leaves uncovered"):
        TickTables({NON_GOLD_ETF: base.table_for(NON_GOLD_ETF)}, inferred=[narrower])


def test_an_inferred_source_needs_the_etf_table_and_a_unique_window():
    good = infer(observations(grid="0.05"))
    with pytest.raises(TickSizeUnavailable, match="NON_GOLD_ETF table"):
        TickTables({EQUITY: load_default_tables().table_for(EQUITY)}, inferred=[good])
    with pytest.raises(TickSizeUnavailable, match="same window"):
        TickTables({NON_GOLD_ETF: load_default_tables().table_for(NON_GOLD_ETF)}, inferred=[good, good])


def test_benchmark_isins_without_rows_refuse():
    with pytest.raises(TickSizeUnavailable):
        load_default_tables(benchmark_isins=(ETF,))


# ---- the seal ---------------------------------------------------------------------------------------------------------
def legacy_sha256(tables: TickTables) -> str:
    """The hash body as it was before inference existed."""
    return canonical_sha256({
        name: [[v.version, v.version_hash] for v in tables.table_for(name).versions] for name in sorted(tables.classes())
    })


def test_without_inference_the_seal_is_unchanged():
    assert load_default_tables().sha256() == legacy_sha256(load_default_tables())
    assert tables_with(observations(grid="0.05")).sha256() != legacy_sha256(tables_with(observations(grid="0.05")))


def test_changed_input_rows_change_the_tick_table_seal():
    base = observations(grid="0.05")
    nudged = with_price(base, 70, base[70].prices[0] + D("0.05"))  # still on the 0.05 grid: same tick, other input
    one, two = tables_with(base), tables_with(nudged)
    assert one.sha256() != two.sha256()
    assert etf_tick(one, date(2025, 9, 15)).value == etf_tick(two, date(2025, 9, 15)).value == D("0.05")


def test_an_unavailable_result_is_sealed_too():
    assert tables_with(observations(40, grid="0.05")).sha256() != tables_with(observations(41, grid="0.05")).sha256()


def test_infer_benchmark_ticks_makes_one_record_per_configured_isin_and_window():
    obs = observations(grid="0.05")
    sources = infer_benchmark_ticks(rows_for(ETF, obs) + rows_for(OTHER_ETF, obs), (OTHER_ETF, ETF))
    assert [s.security for s in sources] == [ETF, OTHER_ETF] and all(s.tick == D("0.05") for s in sources)
    only = infer_benchmark_ticks(rows_for(ETF, obs) + rows_for(OTHER_ETF, obs), (ETF,))
    assert [s.security for s in only] == [ETF]

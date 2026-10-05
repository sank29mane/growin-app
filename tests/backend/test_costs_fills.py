"""D-10 scenarios, aggregate participation, circuit band (D15) and look-ahead guard.

All prices, bars and bands here are SYNTHETIC test values.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from costs.core import CostModelError, InputError, LookaheadError, Side, sha256_hex
from costs.fills import (
    FILLED_AT_LIMIT,
    MISSED_LOCKED_AT_BAND,
    MISSED_THRESHOLD,
    MISSED_VOLUME_CAP_ZERO,
    NO_FILL_AMBIGUOUS_SINGLE_PRICE,
    NO_FILL_BAND_DATE_MISMATCH,
    NO_FILL_BAND_UNAVAILABLE,
    PARTIAL_VOLUME_CAP,
    REJECTED_OUTSIDE_BAND,
    BandUnavailable,
    FillOutcome,
    LimitOrder,
    PriceBand,
    SessionBar,
    TickSize,
    load_fill_scenarios,
    simulate_session,
)
from costs.run import simulate_and_price
from costs.schedule import PricingBasis, load_schedule_set
from costs.ticks import load_tick_table, resolve_tick_from_table

D = Decimal
ISIN = "INE0TEST0001"
SESSION = date(2026, 10, 6)
SCENARIOS_PATH = Path(__file__).resolve().parents[2] / "backend" / "costs" / "schedules" / "daily_bar_fill_scenarios.json"
TICKS_PATH = Path(__file__).resolve().parents[2] / "backend" / "costs" / "schedules" / "nse_cash_tick_sizes.json"

# Any change to the committed scenario file needs a new version id and a new
# literal here, in the same commit.
EXPECTED_SCENARIOS_HASH = "eefec7ddcba790829b8b21a90894666cec16867c72d07772f77ac30e5ec456a0"

TICK = TickSize(D("0.05"), date(2025, 4, 15), "test-explicit", sha256_hex("test-explicit"))
FIXED_BAND = PriceBand("fixed", D("360.00"), D("440.00"), SESSION, "test-band", sha256_hex("test-band"))
NO_BAND = PriceBand("no_band", None, None, SESSION, "test-band", sha256_hex("test-band"))


def scenarios():
    return load_fill_scenarios()


def make_bar(*, low="395.00", high="402.00", open_="400.50", close="401.00", volume=20000, band=FIXED_BAND,
             basis="raw"):
    return SessionBar(ISIN, "NSE", SESSION, D(open_), D(high), D(low), D(close), volume, basis, band, "test-bar")


def flat_bar(price, *, band=FIXED_BAND, volume=20000):
    return make_bar(low=price, high=price, open_=price, close=price, band=band, volume=volume)


def make_order(order_id="o1", *, side=Side.BUY, quantity=50, limit="400.00", reference="399.00",
               submitted="2026-10-05T18:00:00+05:30", information_as_of=date(2026, 10, 5), tick=TICK):
    return LimitOrder(
        order_id, ISIN, "NSE", side, quantity, D(limit), D(reference), SESSION,
        datetime.fromisoformat(submitted), information_as_of, tick,
    )


def sim(orders, bar, scenario_id="base"):
    return simulate_session(orders, bar, scenarios().get(scenario_id))


def one(order, bar, scenario_id="base"):
    (result,) = sim([order], bar, scenario_id)
    return result


# ---- scenario loader ---------------------------------------------------------


def test_loader_reads_the_three_d10_scenarios():
    loaded = scenarios()
    assert [s.scenario_id for s in loaded.all()] == ["base", "adverse", "pessimistic"]
    assert [s.k_ticks for s in loaded.all()] == [1, 2, 3]
    assert [s.volume_participation for s in loaded.all()] == [D("0.01"), D("0.005"), D("0.0025")]
    assert loaded.gate().scenario_id == "pessimistic"
    assert loaded.gate().phase62_gate is True
    assert [s.phase62_gate for s in loaded.all()] == [False, False, True]
    assert loaded.submission_cutoff == time(9, 0)
    assert loaded.fill_price_rule == "at_limit"
    assert loaded.version == "daily-bar-limit-fills-2026-10-01.r1"
    for scenario in loaded.all():
        assert scenario.scenarios_hash == loaded.scenarios_hash
        assert scenario.scenarios_version == loaded.version
        assert scenario.submission_cutoff == time(9, 0)
        assert scenario.assumption_note == loaded.assumption_note


def test_scenarios_hash_is_pinned():
    assert len(scenarios().scenarios_hash) == 64
    assert scenarios().scenarios_hash == EXPECTED_SCENARIOS_HASH


def raw_scenarios() -> dict:
    return json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))


def write(tmp_path, document) -> Path:
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


BAD_SCENARIO_FILES = {
    "unknown top-level key": lambda d: d.update(extra="x"),
    "unknown scenario key": lambda d: d["scenarios"][0].update(extra="x"),
    "two gates": lambda d: d["scenarios"][0].update(phase62_gate=True),
    "no gate": lambda d: d["scenarios"][2].update(phase62_gate=False),
    "k_ticks zero": lambda d: d["scenarios"][0].update(k_ticks=0),
    "k_ticks string": lambda d: d["scenarios"][0].update(k_ticks="1"),
    "k_ticks bool": lambda d: d["scenarios"][0].update(k_ticks=True),
    "participation zero": lambda d: d["scenarios"][0].update(volume_participation="0"),
    "participation above one": lambda d: d["scenarios"][0].update(volume_participation="1.5"),
    "duplicate id": lambda d: d["scenarios"][1].update(id="base"),
    "wrong fill rule": lambda d: d.update(fill_price_rule="at_open"),
    "bad cutoff": lambda d: d.update(submission_cutoff_ist="9am"),
    "wrong schema": lambda d: d.update(schema="growin.costs.fill_scenarios/2"),
    "empty scenarios": lambda d: d.update(scenarios=[]),
}


@pytest.mark.parametrize("label", list(BAD_SCENARIO_FILES))
def test_loader_rejects_bad_files(tmp_path, label):
    document = raw_scenarios()
    BAD_SCENARIO_FILES[label](document)
    with pytest.raises(CostModelError):
        load_fill_scenarios(write(tmp_path, document))


def test_loader_rejects_bare_json_number(tmp_path):
    text = SCENARIOS_PATH.read_text(encoding="utf-8").replace('"volume_participation": "0.01"', '"volume_participation": 0.01')
    path = tmp_path / "bare.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CostModelError):
        load_fill_scenarios(path)


# ---- scenario matrix ---------------------------------------------------------

SCENARIO_ROWS = [("base", 1, D("0.01"), 200), ("adverse", 2, D("0.005"), 100), ("pessimistic", 3, D("0.0025"), 50)]


@pytest.mark.parametrize("scenario_id, k, participation, cap", SCENARIO_ROWS)
def test_each_scenario_can_miss_fill_partially_and_drift(scenario_id, k, participation, cap):
    step = D("0.05")
    miss_low = D("400.00") - (k - 1) * step
    miss = one(make_order(), make_bar(low=str(miss_low)), scenario_id)
    assert miss.outcome is FillOutcome.MISSED
    assert miss.reason_code == MISSED_THRESHOLD
    assert miss.filled_quantity == 0

    edge_low = D("400.00") - k * step
    edge = one(make_order(), make_bar(low=str(edge_low)), scenario_id)
    assert edge.outcome is FillOutcome.FILLED
    assert edge.reason_code == FILLED_AT_LIMIT

    partial = one(make_order(quantity=250), make_bar(low="395.00", volume=20000), scenario_id)
    assert partial.outcome is FillOutcome.PARTIAL
    assert partial.reason_code == PARTIAL_VOLUME_CAP
    assert partial.filled_quantity == cap
    assert partial.session_volume_cap == cap
    assert partial.k_ticks == k
    assert partial.volume_participation == participation
    assert partial.scenario_id == scenario_id

    assert partial.fill_price == D("400.00")
    assert partial.decision_drift_per_share == D("1.00")
    assert partial.decision_drift_bps == D("25.06")
    assert partial.drift_adverse is True
    assert partial.execution_slippage_per_share == D("0.00")
    assert partial.execution_slippage_bps == D("0.00")

    favourable = one(make_order(quantity=250, reference="401.00"), make_bar(low="395.00"), scenario_id)
    assert favourable.fill_price == D("400.00")
    assert favourable.decision_drift_per_share == D("-1.00")
    assert favourable.drift_adverse is False
    assert favourable.execution_slippage_per_share == D("0.00")

    gap = one(make_order(quantity=250), make_bar(low="395.00", open_="398.00"), scenario_id)
    assert gap.fill_price == D("400.00")
    assert gap.forgone_improvement_per_share == D("2.00")


def test_sell_threshold_is_symmetric():
    sell = make_order("s1", side=Side.SELL, limit="400.00", reference="401.00")
    bar_miss = make_bar(low="398.00", high="400.04", open_="399.00", close="399.50")
    bar_fill = make_bar(low="398.00", high="400.05", open_="399.00", close="399.50")
    assert one(sell, bar_miss).reason_code == MISSED_THRESHOLD
    filled = one(sell, bar_fill)
    assert filled.outcome is FillOutcome.FILLED
    assert filled.decision_drift_per_share == D("1.00")
    assert filled.forgone_improvement_per_share == D("0.00")


# ---- aggregation -------------------------------------------------------------


def by_id(results):
    return {r.order_id: r for r in results}


def test_participation_is_aggregated_first_in_first_out():
    a = make_order("a", quantity=120, submitted="2026-10-05T17:00:00+05:30")
    b = make_order("b", quantity=120, submitted="2026-10-05T18:00:00+05:30")
    forward = sim([a, b], make_bar())
    backward = sim([b, a], make_bar())
    assert forward == backward
    got = by_id(forward)
    assert (got["a"].outcome, got["a"].filled_quantity) == (FillOutcome.FILLED, 120)
    assert (got["b"].outcome, got["b"].filled_quantity) == (FillOutcome.PARTIAL, 80)
    assert [r.order_id for r in forward] == ["a", "b"]


def test_equal_submission_time_falls_back_to_order_id():
    x = make_order("x", quantity=150)
    y = make_order("y", quantity=150)
    got = by_id(sim([y, x], make_bar()))
    assert got["x"].filled_quantity == 150
    assert got["y"].filled_quantity == 50


def test_buys_and_sells_share_one_session_cap():
    buy = make_order("buy", quantity=150, submitted="2026-10-05T17:00:00+05:30")
    sell = make_order("sell", side=Side.SELL, quantity=150, limit="400.00", reference="401.00",
                      submitted="2026-10-05T18:00:00+05:30")
    got = by_id(sim([sell, buy], make_bar()))
    assert got["buy"].filled_quantity == 150
    assert got["sell"].filled_quantity == 50
    assert got["sell"].outcome is FillOutcome.PARTIAL


def test_a_quantity_that_rounds_to_zero_is_a_miss():
    first = make_order("first", quantity=200, submitted="2026-10-05T17:00:00+05:30")
    second = make_order("second", quantity=10, submitted="2026-10-05T18:00:00+05:30")
    got = by_id(sim([first, second], make_bar()))
    assert got["first"].outcome is FillOutcome.FILLED
    assert got["second"].outcome is FillOutcome.MISSED
    assert got["second"].reason_code == MISSED_VOLUME_CAP_ZERO


def test_an_order_that_misses_its_threshold_consumes_no_cap():
    far = make_order("far", quantity=200, limit="390.00", reference="391.00", submitted="2026-10-05T17:00:00+05:30")
    near = make_order("near", quantity=200, submitted="2026-10-05T18:00:00+05:30")
    got = by_id(sim([near, far], make_bar()))
    assert got["far"].reason_code == MISSED_THRESHOLD
    assert got["near"].outcome is FillOutcome.FILLED
    assert got["near"].filled_quantity == 200


def test_duplicate_order_ids_raise():
    with pytest.raises(InputError):
        sim([make_order("dup"), make_order("dup")], make_bar())


def test_volume_cap_rounds_down_to_whole_shares():
    result = one(make_order(quantity=10), make_bar(volume=999))
    assert result.session_volume_cap == 9
    assert result.filled_quantity == 9
    assert result.outcome is FillOutcome.PARTIAL


def test_filled_quantity_never_rises_with_a_harsher_scenario():
    for low in ("400.00", "399.95", "399.90", "399.85", "399.80"):
        for volume in (0, 99, 1000, 20000, 100000):
            for quantity in (1, 50, 250):
                filled = [
                    one(make_order(quantity=quantity), make_bar(low=low, volume=volume), name).filled_quantity
                    for name in ("base", "adverse", "pessimistic")
                ]
                assert filled[0] >= filled[1] >= filled[2], (low, volume, quantity, filled)


# ---- circuit band (D15) ------------------------------------------------------


def test_limit_outside_a_fixed_band_is_rejected():
    result = one(make_order(limit="445.00"), make_bar())
    assert result.outcome is FillOutcome.REJECTED
    assert result.reason_code == REJECTED_OUTSIDE_BAND
    assert result.filled_quantity == 0
    assert result.to_trade_fill() is None


def test_session_locked_against_the_order_at_the_band_edge():
    upper = one(make_order(limit="440.00", reference="439.00"), flat_bar("440.00"))
    assert (upper.outcome, upper.reason_code) == (FillOutcome.MISSED, MISSED_LOCKED_AT_BAND)
    lower = one(make_order(side=Side.SELL, limit="360.00", reference="361.00"), flat_bar("360.00"))
    assert (lower.outcome, lower.reason_code) == (FillOutcome.MISSED, MISSED_LOCKED_AT_BAND)


def test_bar_trading_outside_its_own_fixed_band_raises():
    with pytest.raises(InputError):
        one(make_order(), make_bar(high="441.00"))


def test_single_price_session_is_not_a_lock_by_itself():
    inside = one(make_order(limit="380.15", reference="380.00"), flat_bar("380.00"))
    assert inside.outcome is FillOutcome.FILLED
    assert inside.reason_code != MISSED_LOCKED_AT_BAND

    upper_lock_sell = one(make_order(side=Side.SELL, limit="439.90", reference="440.00"), flat_bar("440.00"))
    assert upper_lock_sell.outcome is FillOutcome.FILLED
    assert upper_lock_sell.reason_code == FILLED_AT_LIMIT

    lower_lock_buy = one(make_order(limit="360.10", reference="360.00"), flat_bar("360.00"))
    assert lower_lock_buy.outcome is FillOutcome.FILLED
    assert lower_lock_buy.reason_code != MISSED_LOCKED_AT_BAND


def test_no_band_security_fill_rules():
    normal = one(make_order(quantity=250), make_bar(band=NO_BAND))
    fixed = one(make_order(quantity=250), make_bar())
    assert (normal.filled_quantity, normal.fill_price, normal.reason_code) == (
        fixed.filled_quantity, fixed.fill_price, fixed.reason_code,
    )
    assert normal.band_check == "no_band"
    assert fixed.band_check == "inside_band"
    far = one(make_order(limit="445.00", reference="444.00"), make_bar(band=NO_BAND))
    assert far.outcome is not FillOutcome.REJECTED
    for side, limit in ((Side.BUY, "380.15"), (Side.SELL, "379.90")):
        ambiguous = one(make_order(side=side, limit=limit, reference="380.00"), flat_bar("380.00", band=NO_BAND))
        assert ambiguous.outcome is FillOutcome.NO_ASSUMED_FILL
        assert ambiguous.reason_code == NO_FILL_AMBIGUOUS_SINGLE_PRICE
        assert ambiguous.filled_quantity == 0
        assert ambiguous.outcome is not FillOutcome.MISSED


def test_missing_or_wrong_date_band_gives_no_assumed_fill():
    reason = "no band source for 2026-10-06"
    unavailable = BandUnavailable(reason)
    for bar in (make_bar(band=unavailable), flat_bar("380.00", band=unavailable)):
        results = sim([make_order("a", limit="400.00"), make_order("b", limit="380.15")], bar)
        for result in results:
            assert result.outcome is FillOutcome.NO_ASSUMED_FILL
            assert result.reason_code == NO_FILL_BAND_UNAVAILABLE
            assert result.filled_quantity == 0
            assert result.fill_price is None
            assert result.band_check == f"unavailable:{reason}"
            assert result.session_volume_cap == 200
            assert result.to_trade_fill() is None
    stale = PriceBand("fixed", D("360.00"), D("440.00"), date(2026, 10, 5), "test-band", sha256_hex("test-band"))
    result = one(make_order(), make_bar(band=stale))
    assert result.outcome is FillOutcome.NO_ASSUMED_FILL
    assert result.reason_code == NO_FILL_BAND_DATE_MISMATCH
    assert result.to_trade_fill() is None


def test_an_unavailable_band_blocks_an_exit_too():
    # Phase 59 passes BandUnavailable for row-level NSE defects as well; a sell (an exit) gets no fill either.
    unavailable = BandUnavailable("band_crosscheck_row_conflict")
    sell = make_order("s1", side=Side.SELL, limit="399.00", reference="401.00")
    result = one(sell, make_bar(band=unavailable))
    assert result.outcome is FillOutcome.NO_ASSUMED_FILL and result.reason_code == NO_FILL_BAND_UNAVAILABLE
    assert result.filled_quantity == 0 and result.fill_price is None and result.to_trade_fill() is None


def test_no_assumed_fill_consumes_no_cap_and_differs_from_a_miss():
    unavailable = one(make_order(), make_bar(band=BandUnavailable("no band source for 2026-10-06")))
    missed = one(make_order(), make_bar(low="400.00"))
    assert unavailable.outcome is not missed.outcome
    assert unavailable.reason_code != missed.reason_code
    assert unavailable.result_hash != missed.result_hash


# ---- provenance flows into the run hash --------------------------------------


def run_with(tick, scenario):
    return simulate_and_price(
        workspace="india",
        currency="INR",
        orders=[make_order(tick=tick)],
        bars=[make_bar()],
        scenario=scenario,
        schedules=load_schedule_set(),
        pricing_basis=PricingBasis.trade_date(),
    )


def committed_tick():
    return resolve_tick_from_table(load_tick_table(), session_date=SESSION, band_reference_price=D("400.00"))


def test_tick_and_scenario_files_flow_into_fill_and_run_hashes(tmp_path):
    base = run_with(committed_tick(), scenarios().get("base"))
    (fill,) = base.fills
    assert fill.tick_source_hash == load_tick_table().versions[0].version_hash
    assert fill.scenarios_hash == scenarios().scenarios_hash
    assert fill.tick_size == D("0.05")

    document = json.loads(TICKS_PATH.read_text(encoding="utf-8"))
    document["versions"][0]["status"] = "unconfirmed: edited for the provenance test"
    ticks_path = tmp_path / "ticks.json"
    ticks_path.write_text(json.dumps(document), encoding="utf-8")
    edited_tick = resolve_tick_from_table(load_tick_table(ticks_path), session_date=SESSION, band_reference_price=D("400.00"))
    changed_tick = run_with(edited_tick, scenarios().get("base"))
    (tick_fill,) = changed_tick.fills
    assert tick_fill.tick_size == fill.tick_size
    assert tick_fill.filled_quantity == fill.filled_quantity
    assert tick_fill.tick_source_hash != fill.tick_source_hash
    assert tick_fill.result_hash != fill.result_hash
    assert changed_tick.run_hash != base.run_hash

    scenario_doc = raw_scenarios()
    scenario_doc["assumption_note"] = "Edited note, otherwise identical."
    edited_set = load_fill_scenarios(write(tmp_path, scenario_doc))
    changed_scenario = run_with(committed_tick(), edited_set.get("base"))
    (scenario_fill,) = changed_scenario.fills
    assert scenario_fill.filled_quantity == fill.filled_quantity
    assert scenario_fill.scenarios_hash != fill.scenarios_hash
    assert scenario_fill.result_hash != fill.result_hash
    assert changed_scenario.run_hash != base.run_hash


# ---- look-ahead and bar validity ---------------------------------------------


def test_sizing_data_from_the_fill_session_raises():
    with pytest.raises(LookaheadError):
        one(make_order(information_as_of=SESSION), make_bar())
    with pytest.raises(LookaheadError):
        one(make_order(information_as_of=date(2026, 10, 7)), make_bar())


def test_submission_cutoff_is_nine_ist_exclusive():
    with pytest.raises(LookaheadError):
        one(make_order(submitted="2026-10-06T09:00:00+05:30"), make_bar())
    with pytest.raises(LookaheadError):
        one(make_order(submitted="2026-10-06T09:15:00+05:30"), make_bar())
    accepted = one(make_order(submitted="2026-10-06T08:59:00+05:30"), make_bar())
    assert accepted.outcome is FillOutcome.FILLED


def test_cutoff_is_evaluated_in_ist_not_utc():
    # 2026-10-06T03:29Z is 08:59 IST (accepted); 03:30Z is 09:00 IST (rejected).
    one(make_order(submitted="2026-10-06T03:29:00+00:00"), make_bar())
    with pytest.raises(LookaheadError):
        one(make_order(submitted="2026-10-06T03:30:00+00:00"), make_bar())


def test_adjusted_prices_are_refused():
    with pytest.raises(InputError):
        one(make_order(), make_bar(basis="adjusted"))


def test_inconsistent_bars_raise():
    with pytest.raises(InputError):
        one(make_order(), make_bar(low="402.00", high="403.00", open_="402.50", close="401.00"))
    with pytest.raises(InputError):
        one(make_order(), make_bar(high="400.00"))


def test_order_for_another_instrument_raises():
    other = LimitOrder(
        "o9", "INE0TEST0002", "NSE", Side.BUY, 10, D("400.00"), D("399.00"), SESSION,
        datetime.fromisoformat("2026-10-05T18:00:00+05:30"), date(2026, 10, 5), TICK,
    )
    with pytest.raises(InputError):
        one(other, make_bar())


def test_empty_order_list_raises():
    with pytest.raises(InputError):
        sim([], make_bar())

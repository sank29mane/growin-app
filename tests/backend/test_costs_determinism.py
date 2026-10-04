"""End-to-end determinism matrix for backend/costs (COST-03).

Every input below is a SYNTHETIC literal: no RNG, no clock. The matrix covers
every fill outcome, every same-day class and a DP-bearing delivery sell, so the
hash guarantees are exercised on the full surface and not just the tracer path.
"""

from __future__ import annotations

import dataclasses
import decimal
import json
import os
import subprocess
import sys
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest

from costs.core import Side, canonical_json, sha256_hex
from costs.fills import (
    FILLED_AT_LIMIT,
    MISSED_LOCKED_AT_BAND,
    MISSED_THRESHOLD,
    MISSED_VOLUME_CAP_ZERO,
    NO_FILL_AMBIGUOUS_SINGLE_PRICE,
    NO_FILL_BAND_UNAVAILABLE,
    PARTIAL_VOLUME_CAP,
    REJECTED_OFF_TICK,
    REJECTED_OUTSIDE_BAND,
    BandUnavailable,
    LimitOrder,
    PriceBand,
    SessionBar,
    TickSize,
    load_fill_scenarios,
)
from costs.run import simulate_and_price
from costs.schedule import PricingBasis, load_schedule_set

D = Decimal
REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEDULE_PATH = REPO_ROOT / "backend" / "costs" / "schedules" / "icici_nse_cash_charges.json"

A, B, C = "INE0TEST0001", "INE0TEST0002", "INE0TEST0003"
S1, S2, S3 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)
PREVIOUS = {S1: date(2026, 10, 2), S2: date(2026, 10, 5), S3: date(2026, 10, 6)}


def tick() -> TickSize:
    return TickSize(D("0.05"), date(2025, 4, 15), "test-explicit", sha256_hex("test-explicit"))


def fixed_band(day: date) -> PriceBand:
    return PriceBand("fixed", D("360.00"), D("440.00"), day, "test-band", sha256_hex("test-band"))


def no_band(day: date) -> PriceBand:
    return PriceBand("no_band", None, None, day, "test-band", sha256_hex("test-band"))


def bar(isin, day, *, low="395.00", high="402.00", open_="400.50", close="401.00", volume=20000, band=None):
    return SessionBar(
        isin, "NSE", day, D(open_), D(high), D(low), D(close), volume, "raw",
        band if band is not None else fixed_band(day), "test-bar",
    )


def order(order_id, isin, side, quantity, limit, day, at="17:00"):
    previous = PREVIOUS[day].isoformat()
    reference = "399.00" if side is Side.BUY else "401.00"
    return LimitOrder(
        order_id, isin, "NSE", side, quantity, D(limit), D(reference), day,
        datetime.fromisoformat(f"{previous}T{at}:00+05:30"), PREVIOUS[day], tick(),
    )


def build_inputs():
    bars = [
        bar(A, S1), bar(A, S2),
        bar(B, S1, low="440.00", high="440.00", open_="440.00", close="440.00"),
        bar(B, S2), bar(B, S3),
        bar(C, S1, band=BandUnavailable("no band source for 2026-10-05")),
        bar(C, S2),
        bar(C, S3, low="400.00", high="400.00", open_="400.00", close="400.00", band=no_band(S3)),
    ]
    orders = [
        # A, session 1: shared volume cap of 200 shares allocated first in first out.
        order("a1", A, Side.BUY, 70, "400.00", S1, "17:00"),
        order("a2", A, Side.BUY, 250, "400.00", S1, "17:10"),
        order("a3", A, Side.BUY, 10, "400.00", S1, "17:20"),
        order("a4", A, Side.BUY, 10, "390.00", S1, "17:30"),
        order("a5", A, Side.BUY, 10, "400.03", S1, "17:40"),
        order("a6", A, Side.BUY, 10, "445.00", S1, "17:50"),
        # A, session 2: next-session delivery sell, DP applies.
        order("a7", A, Side.SELL, 100, "400.00", S2, "17:00"),
        # B: locked at the upper band, then a same-day net-zero pair, then a plain buy.
        order("b0", B, Side.BUY, 10, "440.00", S1, "17:00"),
        order("b1", B, Side.BUY, 50, "400.00", S2, "17:00"),
        order("b2", B, Side.SELL, 50, "400.00", S2, "17:05"),
        order("b3", B, Side.BUY, 30, "400.00", S3, "17:00"),
        # C: missing band, partial same-day square-off, No Band single-price session.
        order("c0", C, Side.BUY, 10, "400.00", S1, "17:00"),
        order("c1", C, Side.BUY, 100, "400.00", S2, "17:00"),
        order("c2", C, Side.SELL, 60, "400.00", S2, "17:05"),
        order("c3", C, Side.BUY, 10, "400.05", S3, "17:00"),
    ]
    return orders, bars


def run(scenario_id: str, *, orders=None, bars=None, schedules=None):
    built_orders, built_bars = build_inputs()
    return simulate_and_price(
        workspace="india",
        currency="INR",
        orders=built_orders if orders is None else orders,
        bars=built_bars if bars is None else bars,
        scenario=load_fill_scenarios().get(scenario_id),
        schedules=load_schedule_set() if schedules is None else schedules,
        pricing_basis=PricingBasis.trade_date(),
    )


def run_hash_for(scenario_id: str) -> str:
    return run(scenario_id).run_hash


def scenario_ids() -> list[str]:
    return [scenario.scenario_id for scenario in load_fill_scenarios().all()]


# ---- coverage of the matrix --------------------------------------------------


def test_matrix_covers_every_outcome_and_same_day_class():
    result = run("base")
    reasons = {fill.reason_code for fill in result.fills}
    required = {
        FILLED_AT_LIMIT, PARTIAL_VOLUME_CAP, MISSED_THRESHOLD, MISSED_VOLUME_CAP_ZERO, REJECTED_OFF_TICK,
        REJECTED_OUTSIDE_BAND, MISSED_LOCKED_AT_BAND, NO_FILL_BAND_UNAVAILABLE, NO_FILL_AMBIGUOUS_SINGLE_PRICE,
    }
    assert required <= reasons, f"matrix lost coverage of {sorted(required - reasons)}"

    rows = {row.order_id: row for day in result.days for row in day.order_brokerage}
    assert rows["b1"].classification == "intraday" and rows["b2"].classification == "intraday"
    assert rows["c2"].classification == "squared_off_no_brokerage"
    assert rows["c1"].classification == "delivery"
    assert rows["a7"].classification == "delivery"

    partial_day = next(day for day in result.days if any(r.order_id == "c2" for r in day.order_brokerage))
    assert partial_day.provisional_flags == ("same_day_partial_statutory_split",)
    dp_day = next(day for day in result.days if any(r.order_id == "a7" for r in day.order_brokerage))
    assert dp_day.line("dp_charge") > 0
    assert len({fill.isin for fill in result.fills}) >= 3
    assert len({fill.session_date for fill in result.fills}) >= 3


# ---- determinism -------------------------------------------------------------


@pytest.mark.parametrize("scenario_id", scenario_ids())
def test_input_order_and_fresh_loads_do_not_change_the_run(scenario_id):
    orders, bars = build_inputs()
    first = run(scenario_id)
    second = run(scenario_id, orders=list(reversed(orders)), bars=list(reversed(bars)))
    assert canonical_json(first) == canonical_json(second)
    assert first.run_hash == second.run_hash
    assert [f.result_hash for f in first.fills] == [f.result_hash for f in second.fills]
    assert len(first.run_hash) == 64


def test_scenarios_actually_differ_in_their_fills():
    base, adverse, pessimistic = (run(name) for name in ("base", "adverse", "pessimistic"))
    assert base.run_hash != adverse.run_hash != pessimistic.run_hash
    totals = [sum(fill.filled_quantity for fill in r.fills) for r in (base, adverse, pessimistic)]
    assert totals[0] >= totals[1] >= totals[2]
    assert totals[0] > totals[2]


def test_schedule_version_change_moves_the_run_hash(tmp_path):
    document = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    original_ids = [version.version for version in load_schedule_set().versions]
    for version in document["versions"]:
        version["version"] = version["version"] + "-sensitivity-check"
    path = tmp_path / "renamed.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    renamed = load_schedule_set(path)
    changed = run("base", schedules=renamed)
    baseline = run("base")
    assert changed.run_hash != baseline.run_hash
    expected_ids = {original + "-sensitivity-check" for original in original_ids}
    assert {version_id for _, version_id, _ in changed.schedule_refs} <= expected_ids
    assert changed.schedule_refs
    assert changed.total_charges == baseline.total_charges


def test_a_bar_change_moves_the_run_hash():
    orders, bars = build_inputs()
    nudged = [dataclasses.replace(bars[0], volume=bars[0].volume + 1)] + bars[1:]
    assert run("base", orders=orders, bars=nudged).run_hash != run("base").run_hash


def test_an_order_change_moves_the_run_hash():
    orders, bars = build_inputs()
    nudged = [dataclasses.replace(orders[0], reference_price=D("398.00"))] + orders[1:]
    assert run("base", orders=nudged, bars=bars).run_hash != run("base").run_hash


def test_global_decimal_context_cannot_change_the_run():
    clean = run("base")
    saved = decimal.getcontext().copy()
    try:
        decimal.setcontext(decimal.Context(prec=6, rounding=ROUND_DOWN))
        hostile = run("base")
    finally:
        decimal.setcontext(saved)
    assert hostile.run_hash == clean.run_hash
    assert canonical_json(hostile) == canonical_json(clean)


@pytest.mark.parametrize("scenario_id", ["base", "pessimistic"])
def test_hash_seed_cannot_change_the_run(scenario_id):
    in_process = run_hash_for(scenario_id)
    program = (
        "import sys; sys.path[:0] = [sys.argv[1], sys.argv[2]]; "
        "import test_costs_determinism as t; print(t.run_hash_for(sys.argv[3]))"
    )
    for seed in ("0", "4242"):
        completed = subprocess.run(
            [sys.executable, "-c", program, str(REPO_ROOT / "backend"), str(REPO_ROOT / "tests" / "backend"), scenario_id],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip().splitlines()[-1] == in_process, f"hash differs under PYTHONHASHSEED={seed}"

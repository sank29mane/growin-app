"""Dated NSE tick table, limit alignment and fail-closed tick evidence."""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from costs.core import CostModelError, InputError, Side, TickSizeUnavailable, sha256_hex
from costs.fills import (
    REJECTED_OFF_TICK,
    FillOutcome,
    FillScenario,
    LimitOrder,
    PriceBand,
    SessionBar,
    TickSize,
    simulate_session,
)
from costs.ticks import align_limit, is_on_tick, load_tick_table, resolve_tick_from_table

D = Decimal
TABLE_PATH = Path(__file__).resolve().parents[2] / "backend" / "costs" / "schedules" / "nse_cash_tick_sizes.json"
TABLE_VERSION = "nse-cash-ticks-2025-04-15.r1"
SESSION = date(2026, 10, 6)

# Any change to the committed tick table needs a new version id and a new literal
# here, in the same commit.
EXPECTED_TICK_TABLE_HASH = "c8d6fb8b7412903d942e2d6de347a4f6a424cf63f982c955e2513edd861c4543"


def resolve(reference, session=SESSION):
    return resolve_tick_from_table(load_tick_table(), session_date=session, band_reference_price=D(reference))


@pytest.mark.parametrize(
    "reference, tick",
    [
        ("249.99", "0.01"),
        ("250.00", "0.05"),
        ("999.95", "0.05"),
        ("1000.00", "0.10"),
        ("4999.90", "0.10"),
        ("5000.00", "0.50"),
        ("10000.00", "1.00"),
        ("20000.00", "5.00"),
        ("75000.00", "5.00"),
    ],
)
def test_band_edges_are_lower_inclusive_upper_exclusive(reference, tick):
    assert resolve(reference).value == D(tick)


def test_resolved_tick_carries_table_evidence():
    table = load_tick_table()
    tick = resolve("400.00")
    assert tick.effective_from == date(2025, 4, 15)
    assert tick.source == f"nse-cash-price-band-ticks:{TABLE_VERSION}"
    assert tick.source_hash == table.versions[0].version_hash
    assert isinstance(tick, TickSize)


def test_pre_revision_dates_fail_closed():
    with pytest.raises(TickSizeUnavailable):
        resolve("400.00", date(2025, 4, 14))


@pytest.mark.parametrize("bad", ["0", "-1", "0.00"])
def test_non_positive_reference_price_raises(bad):
    with pytest.raises(InputError):
        resolve(bad)


def test_float_reference_price_raises():
    with pytest.raises(InputError):
        resolve_tick_from_table(load_tick_table(), session_date=SESSION, band_reference_price=400.0)


def test_table_hash_is_pinned():
    assert load_tick_table().versions[0].version_hash == EXPECTED_TICK_TABLE_HASH


def test_align_limit_floors_buys_and_ceils_sells():
    tick = D("0.05")
    assert align_limit(D("400.03"), tick, Side.BUY) == D("400.00")
    assert align_limit(D("400.03"), tick, Side.SELL) == D("400.05")
    assert align_limit(D("400.05"), tick, Side.BUY) == D("400.05")
    assert align_limit(D("400.05"), tick, Side.SELL) == D("400.05")
    wrapped = TickSize(tick, date(2025, 4, 15), "test-explicit", sha256_hex("test-explicit"))
    assert align_limit(D("400.03"), wrapped, Side.BUY) == D("400.00")


def test_is_on_tick():
    assert is_on_tick(D("400.05"), D("0.05")) is True
    assert is_on_tick(D("400.03"), D("0.05")) is False
    wrapped = TickSize(D("0.05"), date(2025, 4, 15), "test-explicit", sha256_hex("test-explicit"))
    assert is_on_tick(D("400.10"), wrapped) is True


# ---- table strictness --------------------------------------------------------


def raw_table() -> dict:
    return json.loads(TABLE_PATH.read_text(encoding="utf-8"))


def write(tmp_path, document) -> Path:
    path = tmp_path / "ticks.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def edit_bands(document, bands):
    document["versions"][0]["bands"] = bands


GOOD_BANDS = [
    {"from": "0", "to": "250", "tick": "0.01"},
    {"from": "250", "to": None, "tick": "0.05"},
]

BAD_TABLES = {
    "unknown top-level key": lambda d: d.update(extra="x"),
    "unknown version key": lambda d: d["versions"][0].update(extra="x"),
    "unknown band key": lambda d: d["versions"][0]["bands"][0].update(extra="x"),
    "bands not starting at zero": lambda d: edit_bands(d, [{"from": "1", "to": None, "tick": "0.01"}]),
    "gap between bands": lambda d: edit_bands(
        d, [{"from": "0", "to": "250", "tick": "0.01"}, {"from": "300", "to": None, "tick": "0.05"}]
    ),
    "overlap between bands": lambda d: edit_bands(
        d, [{"from": "0", "to": "250", "tick": "0.01"}, {"from": "200", "to": None, "tick": "0.05"}]
    ),
    "open-ended band not last": lambda d: edit_bands(
        d, [{"from": "0", "to": None, "tick": "0.01"}, {"from": "250", "to": None, "tick": "0.05"}]
    ),
    "last band closed": lambda d: edit_bands(d, [{"from": "0", "to": "250", "tick": "0.01"}]),
    "zero tick": lambda d: edit_bands(d, [{"from": "0", "to": None, "tick": "0"}]),
    "negative tick": lambda d: edit_bands(d, [{"from": "0", "to": None, "tick": "-0.01"}]),
    "empty bands": lambda d: edit_bands(d, []),
    "wrong schema": lambda d: d.update(schema="growin.costs.tick_sizes/2"),
    "wrong exchange": lambda d: d["versions"][0].update(exchange="BSE"),
}


@pytest.mark.parametrize("label", list(BAD_TABLES))
def test_strict_table_validation(tmp_path, label):
    document = raw_table()
    BAD_TABLES[label](document)
    with pytest.raises(CostModelError):
        load_tick_table(write(tmp_path, document))


def test_bare_json_number_raises(tmp_path):
    text = TABLE_PATH.read_text(encoding="utf-8").replace('"tick": "0.01"', '"tick": 0.01', 1)
    path = tmp_path / "bare.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CostModelError):
        load_tick_table(path)


def test_duplicate_key_raises(tmp_path):
    text = TABLE_PATH.read_text(encoding="utf-8").replace(
        '"exchange": "NSE",', '"exchange": "NSE",\n      "exchange": "NSE",', 1
    )
    path = tmp_path / "dup.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CostModelError):
        load_tick_table(path)


def test_minimal_good_table_loads(tmp_path):
    document = raw_table()
    edit_bands(document, GOOD_BANDS)
    table = load_tick_table(write(tmp_path, document))
    assert resolve_tick_from_table(table, session_date=SESSION, band_reference_price=D("300")).value == D("0.05")


def test_changing_the_table_changes_its_hash(tmp_path):
    document = raw_table()
    document["versions"][0]["status"] = "unconfirmed: edited for the hash test"
    assert load_tick_table(write(tmp_path, document)).versions[0].version_hash != EXPECTED_TICK_TABLE_HASH


# ---- fill-model tick checks --------------------------------------------------

ISIN = "INE0TEST0001"
SCENARIO = FillScenario(
    "base", 1, D("0.01"), False, time(9, 0),
    "Simulation assumptions, not evidence of actual fill probability.", "test-inline", sha256_hex("test-inline"),
)


def tick(**overrides):
    kwargs = dict(
        value=D("0.05"), effective_from=date(2025, 4, 15), source="test-explicit",
        source_hash=sha256_hex("test-explicit"),
    )
    kwargs.update(overrides)
    return TickSize(**kwargs)


def bar() -> SessionBar:
    return SessionBar(
        ISIN, "NSE", SESSION, D("400.50"), D("402.00"), D("395.00"), D("401.00"), 20000, "raw",
        PriceBand("fixed", D("360.00"), D("440.00"), SESSION, "test-band", sha256_hex("test-band")), "test-bar",
    )


def order(*, limit="400.00", order_tick="default") -> LimitOrder:
    return LimitOrder(
        "o1", ISIN, "NSE", Side.BUY, 50, D(limit), D("399.00"), SESSION,
        datetime.fromisoformat("2026-10-05T18:00:00+05:30"), date(2026, 10, 5),
        tick() if order_tick == "default" else order_tick,
    )


def test_off_tick_limit_is_rejected_not_crashed():
    (result,) = simulate_session([order(limit="400.03")], bar(), SCENARIO)
    assert result.outcome is FillOutcome.REJECTED
    assert result.reason_code == REJECTED_OFF_TICK
    assert result.filled_quantity == 0
    assert result.fill_price is None
    assert result.to_trade_fill() is None


def test_on_tick_limit_still_fills():
    (result,) = simulate_session([order(limit="400.00")], bar(), SCENARIO)
    assert result.outcome is FillOutcome.FILLED


def test_tick_not_yet_effective_raises():
    with pytest.raises(TickSizeUnavailable):
        simulate_session([order(order_tick=tick(effective_from=date(2026, 10, 7)))], bar(), SCENARIO)


def test_tick_already_expired_raises():
    expired = tick(effective_to=date(2026, 10, 5))
    with pytest.raises(TickSizeUnavailable):
        simulate_session([order(order_tick=expired)], bar(), SCENARIO)


def test_tick_effective_on_the_session_edge_is_accepted():
    edge = tick(effective_from=SESSION, effective_to=SESSION)
    (result,) = simulate_session([order(order_tick=edge)], bar(), SCENARIO)
    assert result.outcome is FillOutcome.FILLED


def test_missing_tick_still_raises():
    with pytest.raises(TickSizeUnavailable):
        simulate_session([order(order_tick=None)], bar(), SCENARIO)

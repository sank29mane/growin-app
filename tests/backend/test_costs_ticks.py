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
from costs.ticks import (
    NON_GOLD_ETF_TICK_TABLE_PATH,
    align_limit,
    is_on_tick,
    load_tick_table,
    resolve_tick_from_table,
)

D = Decimal
TABLE_PATH = Path(__file__).resolve().parents[2] / "backend" / "costs" / "schedules" / "nse_cash_tick_sizes.json"
TABLE_VERSION = "nse-cash-ticks-2025-04-15.r2"
FLAT_VERSION = "nse-cash-ticks-2021-01-01.r1"
TWO_BAND_VERSION = "nse-cash-ticks-2024-06-10.r1"
ETF_VERSION = "nse-cash-etf-ticks-2021-01-01.r1"
ETF_2026_VERSION = "nse-cash-etf-ticks-2026-09-07.r1"
SESSION = date(2026, 10, 6)

# Any change to a committed tick version needs a new version id and a new literal
# here, in the same commit.
EXPECTED_TICK_TABLE_HASH = "73311d339eb6d01ca81589b0510a873146a425b7e8cf75352ee78cb11becdacc"
EXPECTED_VERSION_HASHES = {
    FLAT_VERSION: "9cfb9c95c6ecc244cc94b9813eefbd4dd6a29d3e0f46c7329ef70eb7a059f80f",
    TWO_BAND_VERSION: "e2262cd5bcaab22488e7ba0e6e2b587f84fcc9a3f940156717cccb92eac57159",
    TABLE_VERSION: EXPECTED_TICK_TABLE_HASH,
    ETF_VERSION: "219c90b126ece971802eb2641e8eb4f45c37725988469955e35dc223b618b04c",
    ETF_2026_VERSION: "ab4d5464043b4530c2b1bf64be4a1e033419f5e4c6e40a6d032fb64a45a0f5da",
}


def resolve(reference, session=SESSION):
    return resolve_tick_from_table(load_tick_table(), session_date=session, band_reference_price=D(reference))


def resolve_etf(reference, session):
    return resolve_tick_from_table(
        load_tick_table(NON_GOLD_ETF_TICK_TABLE_PATH), session_date=session, band_reference_price=D(reference)
    )


def version_by_id(table, version_id):
    (version,) = [v for v in table.versions if v.version == version_id]
    return version


# NSE/CMTR/67133 rows: Below 250; >= 250 to 1,000; > 1,000 to 5,000; > 5,000 to 10,000; > 10,000 to 20,000; > 20,000.
# Only 250 opens its band; 1,000, 5,000, 10,000 and 20,000 close the band below them.
@pytest.mark.parametrize(
    "reference, tick",
    [
        ("0.01", "0.01"),
        ("249.99", "0.01"),
        ("250.00", "0.05"),
        ("250.01", "0.05"),
        ("999.99", "0.05"),
        ("1000.00", "0.05"),
        ("1000.01", "0.10"),
        ("4999.99", "0.10"),
        ("5000.00", "0.10"),
        ("5000.01", "0.50"),
        ("9999.99", "0.50"),
        ("10000.00", "0.50"),
        ("10000.01", "1.00"),
        ("19999.99", "1.00"),
        ("20000.00", "1.00"),
        ("20000.01", "5.00"),
        ("75000.00", "5.00"),
    ],
)
def test_2025_band_edges_follow_the_circular_markers(reference, tick):
    assert resolve(reference).value == D(tick)
    # The same answer on the first day and on a later day of the version.
    assert resolve(reference, date(2025, 4, 15)).value == D(tick)


@pytest.mark.parametrize("session", [date(2024, 6, 10), date(2025, 4, 14)])
@pytest.mark.parametrize("reference, tick", [("249.99", "0.01"), ("250.00", "0.05"), ("250.01", "0.05")])
def test_2024_two_band_edge_at_250_is_regular_tick(session, reference, tick):
    # NSE/CMTR/62174: below Rs 250 is Rs 0.01; the reference price "less than Rs 250" else the regular Rs 0.05.
    assert resolve(reference, session).value == D(tick)


def test_upper_inclusive_defaults_to_false_so_other_versions_keep_lower_inclusive_edges():
    table = load_tick_table()
    for version_id in (FLAT_VERSION, TWO_BAND_VERSION):
        assert not any(band.upper_inclusive for band in version_by_id(table, version_id).bands)
    r2 = version_by_id(table, TABLE_VERSION)
    assert [band.upper_inclusive for band in r2.bands] == [False, True, True, True, True, False]


def test_resolved_tick_carries_table_evidence():
    table = load_tick_table()
    tick = resolve("400.00")
    assert tick.effective_from == date(2025, 4, 15)
    assert tick.source == f"nse-cash-price-band-ticks:{TABLE_VERSION}"
    assert tick.source_hash == version_by_id(table, TABLE_VERSION).version_hash
    assert isinstance(tick, TickSize)


@pytest.mark.parametrize("session", [date(2020, 12, 31), date(2019, 6, 3), date(2000, 1, 1)])
def test_dates_before_the_sourced_window_fail_closed(session):
    with pytest.raises(TickSizeUnavailable):
        resolve("400.00", session)
    with pytest.raises(TickSizeUnavailable):
        resolve_etf("400.00", session)


# (session, reference price, tick, version id, version effective_from, version effective_to)
HISTORY_CASES = [
    (date(2021, 1, 1), "100.00", "0.05", FLAT_VERSION, date(2021, 1, 1), date(2024, 6, 9)),
    (date(2021, 10, 1), "1.00", "0.05", FLAT_VERSION, date(2021, 1, 1), date(2024, 6, 9)),
    (date(2023, 6, 30), "49999.00", "0.05", FLAT_VERSION, date(2021, 1, 1), date(2024, 6, 9)),
    # The day before the 2024-06-10 revision is still the flat tick, even below Rs 250.
    (date(2024, 6, 9), "100.00", "0.05", FLAT_VERSION, date(2021, 1, 1), date(2024, 6, 9)),
    (date(2024, 6, 10), "100.00", "0.01", TWO_BAND_VERSION, date(2024, 6, 10), date(2025, 4, 14)),
    (date(2024, 6, 10), "249.99", "0.01", TWO_BAND_VERSION, date(2024, 6, 10), date(2025, 4, 14)),
    (date(2024, 6, 10), "250.00", "0.05", TWO_BAND_VERSION, date(2024, 6, 10), date(2025, 4, 14)),
    (date(2024, 12, 2), "1000.00", "0.05", TWO_BAND_VERSION, date(2024, 6, 10), date(2025, 4, 14)),
    (date(2024, 12, 2), "25000.00", "0.05", TWO_BAND_VERSION, date(2024, 6, 10), date(2025, 4, 14)),
    # The last day before the price-band table: a Rs 1,000+ reference still ticks at 0.05.
    (date(2025, 4, 14), "1500.00", "0.05", TWO_BAND_VERSION, date(2024, 6, 10), date(2025, 4, 14)),
    (date(2025, 4, 14), "249.99", "0.01", TWO_BAND_VERSION, date(2024, 6, 10), date(2025, 4, 14)),
    # The day of the price-band table: the same reference now ticks at 0.10.
    (date(2025, 4, 15), "1500.00", "0.10", TABLE_VERSION, date(2025, 4, 15), None),
    (date(2025, 4, 15), "249.99", "0.01", TABLE_VERSION, date(2025, 4, 15), None),
]


@pytest.mark.parametrize("session, reference, tick, version, effective_from, effective_to", HISTORY_CASES)
def test_dated_equity_tick_history(session, reference, tick, version, effective_from, effective_to):
    resolved = resolve(reference, session)
    assert resolved.value == D(tick)
    assert resolved.source == f"nse-cash-price-band-ticks:{version}"
    assert resolved.source_hash == EXPECTED_VERSION_HASHES[version]
    assert resolved.effective_from == effective_from
    assert resolved.effective_to == effective_to


def test_equity_versions_are_contiguous_from_2021():
    versions = load_tick_table().versions
    assert versions[0].effective_from == date(2021, 1, 1)
    assert [v.version for v in versions] == [FLAT_VERSION, TWO_BAND_VERSION, TABLE_VERSION]
    for earlier, later in zip(versions, versions[1:]):
        assert (later.effective_from - earlier.effective_to).days == 1
    assert versions[-1].effective_to is None


@pytest.mark.parametrize("reference", ["1.00", "249.99", "250.00", "5000.00", "75000.00"])
@pytest.mark.parametrize("session", [date(2021, 1, 1), date(2023, 6, 30), date(2024, 6, 10), date(2025, 4, 14)])
def test_non_gold_etf_tick_is_flat_one_paisa(session, reference):
    resolved = resolve_etf(reference, session)
    assert resolved.value == D("0.01")
    assert resolved.source == f"nse-cash-non-gold-etf-ticks:{ETF_VERSION}"
    assert resolved.source_hash == EXPECTED_VERSION_HASHES[ETF_VERSION]
    assert resolved.effective_from == date(2021, 1, 1)
    assert resolved.effective_to == date(2025, 4, 14)


def test_etf_table_does_not_cover_the_gap_or_before_2021():
    for session in (date(2020, 12, 31), date(2025, 4, 15), date(2025, 10, 1), date(2026, 9, 6)):
        with pytest.raises(TickSizeUnavailable):
            resolve_etf("400.00", session)


@pytest.mark.parametrize("session", [date(2026, 9, 7), date(2026, 10, 6)])
@pytest.mark.parametrize("reference", ["1.00", "249.99", "250.00", "5000.00", "75000.00"])
def test_non_gold_etf_tick_from_2026_09_07_cites_cmtr_76101(session, reference):
    resolved = resolve_etf(reference, session)
    assert resolved.value == D("0.01")
    assert resolved.source == f"nse-cash-non-gold-etf-ticks:{ETF_2026_VERSION}"
    assert resolved.effective_from == date(2026, 9, 7)
    assert resolved.effective_to is None


def test_etf_and_equity_ticks_differ_on_the_same_day():
    session = date(2023, 1, 2)
    assert resolve("400.00", session).value == D("0.05")
    assert resolve_etf("400.00", session).value == D("0.01")
    assert resolve("400.00", session).source != resolve_etf("400.00", session).source


def test_etf_table_declares_that_gold_etfs_are_not_covered():
    versions = load_tick_table(NON_GOLD_ETF_TICK_TABLE_PATH).versions
    assert [v.version for v in versions] == [ETF_VERSION, ETF_2026_VERSION]
    for version in versions:
        assert "NOT covered" in version.status
        assert "Gold" in version.status


@pytest.mark.parametrize("path", [TABLE_PATH, NON_GOLD_ETF_TICK_TABLE_PATH])
def test_every_sourced_version_cites_a_dated_nse_circular(path):
    for version in load_tick_table(path).versions:
        assert version.status.startswith("sourced from NSE")
        assert version.sources
        # r2 keeps two secondary news links after its circulars, as corroboration only.
        circulars = version.sources[:2] if version.version == TABLE_VERSION else version.sources
        for source in circulars:
            assert "NSE/CMTR/" in source
            assert "https://nsearchives.nseindia.com/content/circulars/CMTR" in source
            assert "dated 20" in source
            assert "fetched 2026-10-06" in source


@pytest.mark.parametrize("bad", ["0", "-1", "0.00"])
def test_non_positive_reference_price_raises(bad):
    with pytest.raises(InputError):
        resolve(bad)


def test_float_reference_price_raises():
    with pytest.raises(InputError):
        resolve_tick_from_table(load_tick_table(), session_date=SESSION, band_reference_price=400.0)


def test_table_hash_is_pinned():
    table = load_tick_table()
    assert version_by_id(table, TABLE_VERSION).version_hash == EXPECTED_TICK_TABLE_HASH
    etf = load_tick_table(NON_GOLD_ETF_TICK_TABLE_PATH)
    actual = {v.version: v.version_hash for v in (*table.versions, *etf.versions)}
    assert actual == EXPECTED_VERSION_HASHES


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
    document["versions"][-1]["bands"] = bands  # the open-ended 2025-04-15 version


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
    "upper_inclusive as a string": lambda d: d["versions"][-1]["bands"][1].update(upper_inclusive="true"),
    "upper_inclusive as a number": lambda d: d["versions"][-1]["bands"][1].update(upper_inclusive=1),
    "upper_inclusive on the open-ended band": lambda d: d["versions"][-1]["bands"][-1].update(upper_inclusive=True),
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
    document["versions"][-1]["status"] = "unconfirmed: edited for the hash test"
    edited = load_tick_table(write(tmp_path, document))
    assert version_by_id(edited, TABLE_VERSION).version_hash != EXPECTED_TICK_TABLE_HASH
    # Editing one version leaves the other versions' hashes alone.
    assert version_by_id(edited, FLAT_VERSION).version_hash == EXPECTED_VERSION_HASHES[FLAT_VERSION]


def test_overlapping_versions_are_rejected(tmp_path):
    document = raw_table()
    document["versions"][0]["effective_to"] = "2024-06-10"
    with pytest.raises(CostModelError):
        load_tick_table(write(tmp_path, document))


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

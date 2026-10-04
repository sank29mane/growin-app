"""Tracer: one UDiFF day plus Breeze v2 fixtures, joined on ISIN, series and date."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from pilot_data.bhavcopy import ingest_udiff
from pilot_data.breeze_bars import ingest_breeze_response
from pilot_data.core import PilotDataError, SourceDescriptor
from pilot_data.crosscheck import crosscheck_window
from pilot_data.models import CrossCheckTolerances, DailyBarConvention
from pilot_data.security_master import ingest_security_master, lineage_from_master
from pilot_data.store import PilotDataStore

import pilot_data_testkit as kit

D2, D3 = date(2025, 1, 2), date(2025, 1, 3)
FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
CONVENTION = DailyBarConvention(allowed_times=("00:00:00",), source="test-fixture")


def desc(source: str, kind: str, locator: str, for_date=None) -> SourceDescriptor:
    return SourceDescriptor(source=source, kind=kind, locator=locator, fetched_at=FETCHED, for_date=for_date)


def on(row: dict, day: date) -> dict:
    return dict(row, TradDt=day.isoformat(), BizDt=day.isoformat())


def ingest_day(store, day, rows):
    return ingest_udiff(
        store, desc("nse_archive", "udiff_cm", f"https://nsearchives.nseindia.com/udiff/{day:%Y%m%d}", day),
        kit.udiff_zip(day, rows), trade_date=day,
    )


def ingest_breeze(store, code, rows, start=D2, end=D3):
    return ingest_breeze_response(
        store, desc("breeze_relay", "breeze_v2_1day", f"relay/{code}/{start}/{end}"),
        kit.breeze_v2_json(rows), stock_code=code, requested_from=start, requested_to=end,
        convention=CONVENTION,
    )


def ingest_master(store):
    return ingest_security_master(
        store, desc("local_file", "security_master", "NSEScripMaster.txt"),
        kit.security_master_bytes(kit.MASTER_SAMPLE_ROWS), snapshot_date=date(2025, 1, 1),
        snapshot_date_basis="test_fixture",
    )


def lineage_for(store, code):
    return lineage_from_master(
        store, stock_code=code, as_of=D3, window_start=D2, window_end=D3, workspace="india"
    )


def run(store, code, series="EQ", tolerances=None, sessions=(D2, D3)):
    return crosscheck_window(
        store, lineage=lineage_for(store, code), stock_code=code, series=series, sessions=sessions,
        window_start=D2, window_end=D3, tolerances=tolerances or CrossCheckTolerances(), workspace="india",
    )


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


@pytest.fixture
def traced(store):
    ingest_master(store)
    ingest_day(store, D2, [kit.UDIFF_RELIANCE_20250102, kit.UDIFF_NIFTYBEES_20250102])
    ingest_day(store, D3, [on(kit.UDIFF_NIFTYBEES_20250102, D3)])
    ingest_breeze(
        store, "RELIND",
        [
            kit.breeze_row("RELIND", "2025-01-02 00:00:00", "1221.25", "1244.45", "1220.00", "1243.00"),
            kit.breeze_row("RELIND", "2025-01-03 00:00:00", "1240.00", "1250.00", "1235.00", "1245.00"),
        ],
    )
    ingest_breeze(
        store, "NIFBEE",
        [kit.breeze_row("NIFBEE", "2025-01-02 00:00:00", "266.99", "272.00", "265.20", "271.60")],
    )
    return store


def quarantine_reasons(store, code):
    rows = store.query(
        "SELECT reason_code, date_from FROM quarantine_records WHERE check_name = 'crosscheck' "
        "AND stock_code = ? ORDER BY date_from, reason_code",
        [code],
    )
    return [(reason, day) for reason, day in rows]


def test_relind_inside_tolerance_is_accepted(traced):
    result = run(traced, "RELIND")
    assert [(a.trade_date, a.isin, a.series) for a in result.accepted] == [(D2, "INE002A01018", "EQ")]
    assert result.anchor_isin == "INE002A01018"


def test_nifbee_close_beyond_half_percent_is_quarantined(traced):
    result = run(traced, "NIFBEE")
    assert result.accepted == ()
    assert (("close_tolerance", D2)) in quarantine_reasons(traced, "NIFBEE")
    assert result.quarantined_by_reason["close_tolerance"] == 1


def test_one_sided_bars_are_quarantined_on_both_sides(traced):
    run(traced, "RELIND")
    run(traced, "NIFBEE")
    assert ("one_sided_breeze", D3) in quarantine_reasons(traced, "RELIND")
    assert ("one_sided_bhavcopy", D3) in quarantine_reasons(traced, "NIFBEE")


def test_missing_primary_bhavcopy_raises_calendar_incomplete(traced):
    with pytest.raises(PilotDataError) as caught:
        run(traced, "RELIND", sessions=(D2, D3, date(2025, 1, 6)))
    assert caught.value.code == "calendar_incomplete"


def test_provenance_resolves_both_sides_for_accepted_bars(traced):
    result = run(traced, "RELIND")
    assert len(result.accepted) == 1
    rows = traced.query(
        "SELECT b.isin, b.nse_symbol, b.adjustment_basis, bf.source, bf.locator, bf.first_fetched_at_utc, "
        "bf.source_sha256, r.stock_code, r.adjustment_basis, rf.source, rf.locator, rf.first_fetched_at_utc, "
        "rf.source_sha256 "
        "FROM bhavcopy_bars b JOIN source_files bf ON bf.source_sha256 = b.source_sha256 "
        "JOIN breeze_bars_raw r ON r.trade_date = b.trade_date "
        "JOIN source_files rf ON rf.source_sha256 = r.source_sha256 "
        "WHERE b.trade_date = ? AND b.isin = ? AND r.stock_code = ?",
        [D2, "INE002A01018", "RELIND"],
    )
    assert len(rows) == 1
    (isin, symbol, bhav_basis, bsrc, bloc, bwhen, bhash, code, brz_basis, rsrc, rloc, rwhen, rhash) = rows[0]
    assert (isin, symbol, bhav_basis) == ("INE002A01018", "RELIANCE", "as_traded")
    assert (bsrc, rsrc) == ("nse_archive", "breeze_relay")
    assert bloc.startswith("https://nsearchives.nseindia.com/") and rloc.startswith("relay/RELIND")
    assert bwhen is not None and rwhen is not None
    assert len(bhash) == 64 and len(rhash) == 64 and bhash != rhash
    assert (code, brz_basis) == ("RELIND", "as_traded_claimed")


def test_rerun_is_idempotent(traced):
    first = run(traced, "NIFBEE")
    before = traced.query("SELECT count(*) FROM quarantine_records")[0][0]
    second = run(traced, "NIFBEE")
    after = traced.query("SELECT count(*) FROM quarantine_records")[0][0]
    assert first.run_id == second.run_id
    assert before == after
    assert traced.query("SELECT count(*) FROM crosscheck_runs")[0][0] == 1


def test_result_carries_all_three_caveats(traced):
    codes = [caveat.code for caveat in run(traced, "RELIND").caveats]
    assert codes[:2] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS"]
    assert "BREEZE_RAW_UNVERIFIED" in codes


# --------------------------------------------------------------- D-06 boundaries
BASE = dict(open_="10000.00", high="10200.00", low="9900.00", close="10000.00")


def boundary_store(tmp_path, breeze_open, breeze_close):
    store = PilotDataStore(tmp_path / "edge", workspace="india")
    ingest_security_master(
        store, desc("local_file", "security_master", "NSEScripMaster.txt"),
        kit.security_master_bytes([kit.master_row(77, "TESTCO", "EQ", "TEST CO", "0.01", "10", "INE002A01018", "TESTCO")]),
        snapshot_date=date(2025, 1, 1), snapshot_date_basis="test_fixture",
    )
    ingest_day(store, D2, [kit.udiff_row("TESTCO", "EQ", "INE002A01018", BASE["open_"], BASE["high"], BASE["low"],
                                         BASE["close"], trade_date=D2)])
    ingest_breeze(
        store, "TESTCO",
        [kit.breeze_row("TESTCO", "2025-01-02 00:00:00", breeze_open, "10200.00", "9900.00", breeze_close)],
        start=D2, end=D2,
    )
    return store


def boundary_run(store):
    return crosscheck_window(
        store, lineage=lineage_from_master(store, stock_code="TESTCO", as_of=D2, window_start=D2, window_end=D2,
                                           workspace="india"),
        stock_code="TESTCO", series="EQ", sessions=(D2,), window_start=D2, window_end=D2,
        tolerances=CrossCheckTolerances(), workspace="india",
    )


@pytest.mark.parametrize(
    "breeze_open,breeze_close,accepted,reason",
    [
        ("10000.00", "10050.00", True, None),            # close exactly +0.5 percent
        ("10000.00", "9950.00", True, None),             # close exactly -0.5 percent
        ("10000.00", "10050.01", False, "close_tolerance"),
        ("10100.00", "10000.00", True, None),            # open exactly +1 percent
        ("10100.01", "10000.00", False, "ohl_tolerance"),  # open 1.0001 percent
        ("9900.00", "10000.00", True, None),             # open exactly -1 percent
    ],
)
def test_d06_boundaries(tmp_path, breeze_open, breeze_close, accepted, reason):
    store = boundary_store(tmp_path, breeze_open, breeze_close)
    try:
        result = boundary_run(store)
        assert bool(result.accepted) is accepted
        if reason is None:
            assert result.quarantined_by_reason == {}
        else:
            assert reason in result.quarantined_by_reason
    finally:
        store.close()


def test_default_tolerances_are_the_locked_d06_values():
    tolerances = CrossCheckTolerances()
    assert tolerances.close_max_rel == Decimal("0.005")
    assert tolerances.ohl_max_rel == Decimal("0.01")

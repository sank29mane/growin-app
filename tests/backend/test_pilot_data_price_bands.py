"""NSE price bands: parsers, append-only ingest, effective dates, band_on, coverage and the CLI."""

import json
import stat
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from pilot_data.bhavcopy import ingest_udiff
from pilot_data.core import PilotDataError, SourceDescriptor, standard_caveats, utc_naive
from pilot_data.nse_http import NseHttp
from pilot_data.nse_ingest import _log_attempt
from pilot_data.price_bands import (
    BAND_CHANGES_URL,
    SEC_LIST_DATED_URL,
    BandConvention,
    band_on,
    build_band_coverage,
    check_band_convention,
    current_band_convention,
    ingest_band_changes,
    ingest_band_list,
    ingest_range,
    main,
    parse_band_changes,
    parse_band_value,
    parse_sec_list,
    record_archive_depth,
    record_band_convention,
)
from pilot_data.sessions import ensure_fetch_log
from pilot_data.store import PilotDataStore
from pilot_data.targets import TargetMember, TargetUniverseResult, ensure_target_tables

import pilot_data_testkit as kit

FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
LATER = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
SESS = [date(2025, 3, 3) + timedelta(days=i) for i in range(5)]  # Mon 3 March to Fri 7 March
S1, S2, S3, S4, S5 = SESS
F0 = date(2025, 2, 28)  # file date whose list is effective on S1 under the next-session rule
FILE_FOR = {S1: F0, S2: S1, S3: S2, S4: S3, S5: S4}
RELIANCE, CANBK, AGSTRA = "INE002A01018", "INE476A01022", "INE155A01022"
FIXTURE = BandConvention(list_rule="next_session_after_file_date", changes_rule="next_session_after_file_date",
                         basis="test_fixture", evidence_sha256="e" * 64, recorded_at_utc=LATER)


def desc(kind="price_band_list", url="https://nsearchives.nseindia.com/content/equities/x.csv", day=None):
    return SourceDescriptor(source="nse_archive", kind=kind, locator=url, fetched_at=FETCHED, for_date=day)


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


def mark_calendar(store, start, end, data_days):
    ensure_fetch_log(store)
    day = start
    while day <= end:
        if day in data_days:
            _log_attempt(store, d=day, kind="udiff", outcome="ingested", http_status=200, error_code=None,
                         source_sha256=None, url="u", attempted_at=LATER)
        else:
            for kind in (("pr",) if day.weekday() >= 5 else ("pr", "udiff", "cm_legacy")):
                _log_attempt(store, d=day, kind=kind, outcome="no_file", http_status=404, error_code=None,
                             source_sha256=None, url="u", attempted_at=LATER)
        day += timedelta(days=1)


def bars(store, *, symbol_by_day=None, instruments=None):
    """A UDiFF day for S1..S5. instruments: list of (symbol, series, isin)."""
    instruments = instruments or [("RELIANCE", "EQ", RELIANCE), ("CANBK", "EQ", CANBK), ("AGSTRA", "BZ", AGSTRA)]
    for day in SESS:
        rows = []
        for symbol, series, isin in instruments:
            name = (symbol_by_day or {}).get((symbol, day), symbol)
            rows.append(kit.udiff_row(name, series, isin, "100", "101", "99", "100", trade_date=day))
        ingest_udiff(store, desc("udiff_cm"), kit.udiff_zip(day, rows), trade_date=day)
    mark_calendar(store, date(2025, 3, 1), date(2025, 3, 9), set(SESS))


STANDARD = [kit.SEC_LIST_RELIANCE, kit.SEC_LIST_CANBK, kit.SEC_LIST_AGSTRA]


def put_list(store, file_date, rows=None):
    return ingest_band_list(store, desc(day=file_date), kit.sec_list_csv(rows or STANDARD), file_date=file_date,
                            workspace="india")


def put_changes(store, file_date, rows=()):
    return ingest_band_changes(store, desc("price_band_changes", day=file_date), kit.band_changes_csv(list(rows)),
                               file_date=file_date, workspace="india")


def with_convention(store):
    record_band_convention(store, FIXTURE, workspace="india")


def band(store, isin, session):
    return band_on(store, isin=isin, session=session, workspace="india")


# --------------------------------------------------------------------------- parsing
def test_parse_band_value_is_a_closed_set():
    assert parse_band_value("20") == ("fixed", Decimal("20"))
    assert parse_band_value("5") == ("fixed", Decimal("5"))
    assert parse_band_value("No Band") == ("no_band", None)
    assert parse_band_value("No band") == ("no_band", None)  # the second spelling NSE publishes
    for bad in ("15", "", "-", "20.5", "NO BAND", "no band", "abc"):
        with pytest.raises(PilotDataError) as caught:
            parse_band_value(bad)
        assert caught.value.code == "band_value_unrecognized"


def test_sec_list_parsing_and_failures():
    rows = {r.nse_symbol: r for r in parse_sec_list(kit.sec_list_csv(STANDARD))}
    assert (rows["RELIANCE"].category, rows["RELIANCE"].percent) == ("no_band", None)
    assert (rows["AGSTRA"].category, rows["AGSTRA"].percent, rows["AGSTRA"].remarks) == (
        "fixed", Decimal("2"), "GSM STAGE - 0")
    assert rows["AGSTRA"].raw_band == "2"
    with pytest.raises(PilotDataError) as header:
        parse_sec_list(kit.sec_list_csv(STANDARD, header=["Symbol", "Series", "Name", "Band", "Remarks"]))
    assert header.value.code == "band_list_schema_mismatch"
    with pytest.raises(PilotDataError) as dup:
        parse_sec_list(kit.sec_list_csv([kit.SEC_LIST_CANBK, kit.SEC_LIST_CANBK]))
    assert dup.value.code == "band_list_duplicate"
    with pytest.raises(PilotDataError) as empty:
        parse_sec_list(kit.sec_list_csv([]))
    assert empty.value.code == "band_list_empty"
    with pytest.raises(PilotDataError):
        parse_sec_list(kit.sec_list_csv([dict(kit.SEC_LIST_CANBK, Band="15")]))


def test_band_changes_parsing():
    (row,) = parse_band_changes(kit.band_changes_csv([kit.BAND_CHANGE_ANANDRATHI]))
    assert (row.from_category, row.from_percent, row.to_category, row.to_percent) == (
        "fixed", Decimal("20"), "no_band", None)
    assert parse_band_changes(kit.band_changes_csv([])) == ()  # a header-only file is a valid no-change file
    with pytest.raises(PilotDataError) as caught:
        parse_band_changes(kit.band_changes_csv([], header=["No", "Symbol", "Series", "Security Name", "From", "To"]))
    assert caught.value.code == "band_changes_schema_mismatch"


def test_ingest_records_provenance_and_is_append_only(store):
    first = put_list(store, S1)
    again = put_list(store, S1)
    assert first.source_sha256 == again.source_sha256 and first.row_count == 3
    assert store.query("SELECT count(*) FROM price_band_list_rows")[0][0] == 3
    assert store.query("SELECT file_kind, file_date, row_count FROM price_band_files") == [("list", S1, 3)]
    assert store.query("SELECT source, kind, for_date FROM source_files") == [
        ("nse_archive", "price_band_list", S1)]
    put_changes(store, S1)
    assert store.query("SELECT file_kind, row_count FROM price_band_files ORDER BY file_kind") == [
        ("changes", 0), ("list", 3)]


# --------------------------------------------------------------------------- band_on
def test_each_session_uses_the_list_effective_on_that_session(store):
    bars(store)
    with_convention(store)
    for session in SESS:
        put_list(store, FILE_FOR[session], rows=STANDARD if session != S3 else [
            dict(kit.SEC_LIST_RELIANCE, Band="10"), kit.SEC_LIST_CANBK, kit.SEC_LIST_AGSTRA])
    plain = band(store, RELIANCE, S2)
    assert (plain.status, plain.percent, plain.nse_symbol, plain.series, plain.source_kind) == (
        "no_band", None, "RELIANCE", "EQ", "list")
    tenth = band(store, RELIANCE, S3)
    assert (tenth.status, tenth.percent) == ("fixed", Decimal("10"))
    bz = band(store, AGSTRA, S4)
    assert (bz.status, bz.percent, bz.series) == ("fixed", Decimal("2"), "BZ")
    assert len(tenth.source_sha256s) == 1 and len(tenth.source_sha256s[0]) == 64


def test_the_isin_comes_from_that_sessions_symbol_not_todays(store):
    renamed = {("RELIANCE", day): "OLDSYM" for day in (S1, S2)}
    bars(store, symbol_by_day=renamed)
    with_convention(store)
    old_list = [dict(kit.SEC_LIST_RELIANCE, Symbol="OLDSYM", Band="5"), dict(kit.SEC_LIST_RELIANCE, Band="20")]
    for session in SESS:
        put_list(store, FILE_FOR[session], rows=old_list + [kit.SEC_LIST_CANBK, kit.SEC_LIST_AGSTRA])
    assert (band(store, RELIANCE, S2).nse_symbol, band(store, RELIANCE, S2).percent) == ("OLDSYM", Decimal("5"))
    assert (band(store, RELIANCE, S3).nse_symbol, band(store, RELIANCE, S3).percent) == ("RELIANCE", Decimal("20"))
    missing = band(store, "INE040A01034", S2)
    assert (missing.status, missing.reason) == ("unknown", "no_bar_on_session")


def test_a_symbol_absent_from_the_effective_list_is_not_in_band_list(store):
    bars(store)
    with_convention(store)
    for session in SESS:
        put_list(store, FILE_FOR[session], rows=[kit.SEC_LIST_RELIANCE, kit.SEC_LIST_AGSTRA])
    got = band(store, CANBK, S2)
    assert (got.status, got.reason) == ("unknown", "not_in_band_list")


def test_no_recorded_convention_means_unknown_everywhere(store):
    bars(store)
    for session in SESS:
        put_list(store, FILE_FOR[session])
    got = band(store, RELIANCE, S2)
    assert (got.status, got.reason) == ("unknown", "band_convention_unverified")
    assert current_band_convention(store) is None
    with_convention(store)
    assert current_band_convention(store).basis == "test_fixture"
    assert band(store, RELIANCE, S2).status == "no_band"


def test_changes_bridge_forward_from_a_validated_baseline(store):
    bars(store)
    with_convention(store)
    base = put_list(store, F0)  # effective S1
    move = put_changes(store, S1, [{"Symbol": "RELIANCE", "Series": "EQ", "Security Name": "R", "From": "No Band",
                                    "To": "20"}])  # applies from S2
    got = band(store, RELIANCE, S2)
    assert (got.status, got.percent, got.source_kind) == ("fixed", Decimal("20"), "changes_chain")
    assert got.source_sha256s == (base.source_sha256, move.source_sha256)
    # S3 needs its own changes file
    gap = band(store, RELIANCE, S3)
    assert (gap.status, gap.reason) == ("unknown", "band_chain_gap")
    put_changes(store, S2, [])  # an empty file is a valid no-change day
    assert band(store, RELIANCE, S3).percent == Decimal("20")


def test_a_changes_row_whose_from_value_disagrees_breaks_the_chain(store):
    bars(store)
    with_convention(store)
    put_list(store, F0)
    put_changes(store, S1, [{"Symbol": "RELIANCE", "Series": "EQ", "Security Name": "R", "From": "10", "To": "20"}])
    got = band(store, RELIANCE, S2)
    assert (got.status, got.reason) == ("unknown", "band_change_from_mismatch")
    stored = store.query(
        "SELECT check_name, reason_code, scope, nse_symbol, date_from FROM quarantine_records "
        "WHERE check_name = 'price_band'")
    assert stored == [("price_band", "band_change_from_mismatch", "raw", "RELIANCE", S2)]


def test_changes_without_a_baseline_are_never_applied(store):
    bars(store)
    with_convention(store)
    put_changes(store, S1, [{"Symbol": "RELIANCE", "Series": "EQ", "Security Name": "R", "From": "No Band",
                             "To": "20"}])
    got = band(store, RELIANCE, S2)
    assert (got.status, got.reason) == ("unknown", "band_no_baseline")


def test_disagreeing_consecutive_lists_and_changes_are_a_conflict(store):
    bars(store)
    with_convention(store)
    put_list(store, F0)  # effective S1
    put_list(store, S1, rows=[dict(kit.SEC_LIST_RELIANCE, Band="10"), kit.SEC_LIST_CANBK, kit.SEC_LIST_AGSTRA])
    put_changes(store, S1, [{"Symbol": "RELIANCE", "Series": "EQ", "Security Name": "R", "From": "No Band",
                             "To": "20"}])  # says 20, the newer list says 10
    got = band(store, RELIANCE, S2)
    assert (got.status, got.reason) == ("unknown", "band_crosscheck_conflict")
    assert store.query("SELECT reason_code FROM quarantine_records WHERE check_name = 'price_band'") == [
        ("band_crosscheck_conflict",)]
    # when the changes file matches the lists, the later session is fine
    agree = put_changes(store, S2, [{"Symbol": "RELIANCE", "Series": "EQ", "Security Name": "R", "From": "10",
                                     "To": "5"}])
    assert agree.row_count == 1
    put_list(store, S2, rows=[dict(kit.SEC_LIST_RELIANCE, Band="5"), kit.SEC_LIST_CANBK, kit.SEC_LIST_AGSTRA])
    assert band(store, RELIANCE, S3).percent == Decimal("5")


def change(symbol, old, new, series="EQ"):
    return {"Symbol": symbol, "Series": series, "Security Name": symbol, "From": old, "To": new}


def extra(symbol, value):
    return dict(kit.SEC_LIST_CANBK, Symbol=symbol, **{"Security Name": symbol, "Band": value})


def row_conflict_world(store, *, agreeing=2, conflicting=1):
    """S2's lists and changes agree on every row except RELIANCE (and any further conflicting rows).

    S3 has no list, so it is bridged from S2's list by an empty changes file. S4 and S5 have lists.
    """
    bars(store)
    with_convention(store)
    movers = [f"XA{i}" for i in range(agreeing)]
    breakers = [f"XB{i}" for i in range(conflicting - 1)]
    before = [*STANDARD, *(extra(s, "20") for s in movers + breakers)]
    after = [dict(kit.SEC_LIST_RELIANCE, Band="10"), kit.SEC_LIST_CANBK, kit.SEC_LIST_AGSTRA,
             *(extra(s, "10") for s in movers + breakers)]
    lists = {S1: put_list(store, F0, rows=before), S2: put_list(store, S1, rows=after)}
    changes = put_changes(store, S1, [change("RELIANCE", "No Band", "20"),  # the newer list says 10
                                      *(change(s, "20", "10") for s in movers),
                                      *(change(s, "20", "5") for s in breakers)])
    bridge = put_changes(store, S2, [])  # S3: no list, nothing moves
    put_list(store, S3, rows=after)
    put_list(store, S4, rows=after)
    return lists, changes, bridge


def test_a_small_cross_check_conflict_is_confined_to_its_row_and_what_depends_on_it(store):
    lists, changes, bridge = row_conflict_world(store)
    evidence = (lists[S1].source_sha256, lists[S2].source_sha256, changes.source_sha256)
    got = band(store, RELIANCE, S2)
    assert (got.status, got.reason, got.nse_symbol, got.series) == (
        "unknown", "band_crosscheck_row_conflict", "RELIANCE", "EQ")
    assert got.source_sha256s == evidence
    neighbour = band(store, CANBK, S2)
    assert (neighbour.status, neighbour.source_kind) == ("no_band", "list")
    # S3 is bridged from S2's list, so the conflicting row stays unknown there and carries the bridge file too
    bridged = band(store, RELIANCE, S3)
    assert (bridged.status, bridged.reason) == ("unknown", "band_crosscheck_row_conflict")
    assert bridged.source_sha256s == (*evidence, bridge.source_sha256)
    assert (band(store, CANBK, S3).status, band(store, CANBK, S3).source_kind) == ("no_band", "changes_chain")
    # S4 has its own list and no conflicting changes file: RELIANCE resolves again
    assert (band(store, RELIANCE, S4).status, band(store, RELIANCE, S4).percent) == ("fixed", Decimal("10"))
    stored = store.query(
        "SELECT DISTINCT reason_code, nse_symbol, series, date_from, detail_json FROM quarantine_records "
        "WHERE check_name = 'price_band'")
    assert [row[:4] for row in stored] == [("band_crosscheck_row_conflict", "RELIANCE", "EQ", S2)]
    assert json.loads(stored[0][4]) == {"list_before": "no_band", "list_after": "fixed:10.00",
                                        "changes": "no_band>fixed:20.00"}


def test_a_row_level_conflict_does_not_block_but_is_reported_row_by_row(store):
    lists, changes, bridge = row_conflict_world(store)
    report = coverage(store)
    assert report.phase62_blocked is False and report.blocked_reasons == ()
    assert report.unknown_by_reason == {"band_crosscheck_row_conflict": 2}
    assert report.unsupported_sessions == () and report.row_conflict_sessions == (S2, S3)
    assert report.target_unknown_counts == {"RELIND": 2}
    assert [(u.stock_code, u.isin, u.session, u.reason) for u in report.unavailable_bands] == [
        ("RELIND", RELIANCE, S2, "band_crosscheck_row_conflict"), ("RELIND", RELIANCE, S3, "band_crosscheck_row_conflict")]
    assert report.unavailable_bands[1].source_sha256s[-1] == bridge.source_sha256
    assert len(report.nonblocking_reasons) == 1 and "band_crosscheck_row_conflict" in report.nonblocking_reasons[0]
    assert report.fixed_count + report.no_band_count + report.unknown_count == 10


@pytest.mark.parametrize("agreeing, conflicting", [(0, 1), (1, 1), (9, 4)])
def test_a_conflict_whose_scope_is_not_proven_blocks_the_session_and_its_bridges(store, agreeing, conflicting):
    row_conflict_world(store, agreeing=agreeing, conflicting=conflicting)
    for session in (S2, S3):  # S3 is bridged from S2's list
        got = band(store, CANBK, session)
        assert (got.status, got.reason) == ("unknown", "band_crosscheck_conflict")
    report = coverage(store)
    assert report.phase62_blocked is True and report.unsupported_sessions == (S2, S3)
    assert report.blocked_reasons[0].startswith("band_crosscheck_conflict: 2 sessions")
    assert report.unknown_by_reason == {"band_crosscheck_conflict": 4} and report.unavailable_bands == ()


def test_only_classified_row_defects_are_nonblocking():
    from pilot_data.price_bands import NONBLOCKING_ROW_DEFECTS

    # Widening this set changes what Phase 62 may run on: it needs an amendment to 59-10 and 62-CONTEXT.
    assert NONBLOCKING_ROW_DEFECTS == frozenset({"band_crosscheck_row_conflict", "not_in_band_list"})


def test_a_symbol_absent_from_the_list_flows_but_a_from_mismatch_blocks(tmp_path):
    with PilotDataStore(tmp_path / "absent", workspace="india") as absent:
        bars(absent)
        with_convention(absent)
        for session in SESS:
            put_list(absent, FILE_FOR[session], rows=[kit.SEC_LIST_RELIANCE, kit.SEC_LIST_AGSTRA])
        report = coverage(absent)
        assert report.phase62_blocked is False and report.unknown_by_reason == {"not_in_band_list": 5}
        assert {u.stock_code for u in report.unavailable_bands} == {"CANBAN"} and len(report.unavailable_bands) == 5
    with PilotDataStore(tmp_path / "mismatch", workspace="india") as mismatch:
        bars(mismatch)
        with_convention(mismatch)
        put_list(mismatch, F0)
        put_changes(mismatch, S1, [change("RELIANCE", "10", "20")])  # From disagrees with the baseline
        report = build_band_coverage(mismatch, start=S1, end=S2, targets=TARGETS, workspace="india")
        assert report.unknown_by_reason == {"band_change_from_mismatch": 1} and report.phase62_blocked is True
        assert report.unavailable_bands == ()


def test_todays_bands_are_never_substituted_for_a_historical_date(store):
    late = date(2026, 10, 1)
    session = date(2026, 9, 30)
    ingest_udiff(store, desc("udiff_cm"), kit.udiff_zip(session, [
        kit.udiff_row("RELIANCE", "EQ", RELIANCE, "100", "101", "99", "100", trade_date=session)]), trade_date=session)
    mark_calendar(store, date(2026, 9, 29), date(2026, 10, 4), {session, late, date(2026, 10, 2)})
    with_convention(store)
    put_list(store, late)  # file date 2026-10-01: effective 2026-10-02, after the session
    got = band(store, RELIANCE, session)
    assert (got.status, got.reason, got.source_sha256s) == ("unknown", "band_no_baseline", ())


# --------------------------------------------------------------------------- coverage
def member(symbol, isin, code):
    return TargetMember(kind="nifty500", anchor_isin=isin, nse_symbol=symbol, stock_code=code, token=1,
                        company_name=symbol)


def targets_of(*members):
    return TargetUniverseResult(
        workspace="india", caveats=standard_caveats(), as_of=S5, members=members, exclusions=(), etf_rejected=(),
        master_snapshot="m" * 64, nifty500_snapshot="n" * 64, target_sha256="t" * 64,
    )


TARGETS = targets_of(member("RELIANCE", RELIANCE, "RELIND"), member("CANBK", CANBK, "CANBAN"))


def full_world(store, *, skip=None, convention=True, change=None):
    bars(store)
    if convention:
        with_convention(store)
    for session in SESS:
        if session == skip:
            continue
        rows = list(STANDARD)
        if change is not None and session == change:
            rows[0] = dict(kit.SEC_LIST_RELIANCE, Band="10")
        put_list(store, FILE_FOR[session], rows=rows)


def coverage(store, start=S1, end=S5):
    return build_band_coverage(store, start=start, end=end, targets=TARGETS, workspace="india")


def test_coverage_with_full_lists_is_not_blocked(store):
    full_world(store)
    report = coverage(store)
    assert report.sessions == 5 and report.sessions_by_status == {"list": 5} and report.unsupported_sessions == ()
    assert (report.fixed_count, report.no_band_count, report.unknown_count) == (0, 10, 0)
    assert report.phase62_blocked is False and report.blocked_reasons == () and report.targets_checked == 2
    assert [c.code for c in report.caveats] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS", "PRICE_BAND_UNSUPPORTED"]
    assert report.convention.basis == "test_fixture"


def test_a_missing_session_is_unsupported_and_blocks_phase_62(store):
    full_world(store, skip=S3)  # no list for S3, and no changes file bridging S2 to S3
    report = coverage(store)
    assert report.unsupported_sessions[0] == S3 and S3.isoformat() in "".join(report.blocked_reasons)
    assert report.phase62_blocked is True and report.unknown_count > 0
    assert report.unknown_by_reason.get("band_chain_gap", 0) == 2  # both targets on S3
    assert report.target_unknown_counts["RELIND"] >= 1
    assert report.fixed_count + report.no_band_count + report.unknown_count == 10


def test_no_convention_blocks_with_its_own_reason(store):
    full_world(store, convention=False)
    report = coverage(store)
    assert report.phase62_blocked and report.convention is None
    assert any("band_convention_unverified" in reason for reason in report.blocked_reasons)
    assert report.unknown_by_reason == {"band_convention_unverified": 10}


def test_archive_depth_is_recorded_and_reported_unchanged(store):
    full_world(store)
    depth = record_archive_depth(
        store, workspace="india",
        probed={date(2021, 6, 30): "no_file", date(2021, 7, 1): "ingested", date(2021, 7, 2): "ingested",
                date(2021, 7, 5): "failed"},
    )
    assert depth.earliest_ingested_list == date(2021, 7, 1) and depth.no_file_dates == (date(2021, 6, 30),)
    assert depth.failed_dates == (date(2021, 7, 5),)
    report = coverage(store)
    assert report.archive_depth.earliest_ingested_list == date(2021, 7, 1)
    assert report.archive_depth.no_file_dates == (date(2021, 6, 30),)  # nothing is inferred for unprobed dates


def test_report_hash_is_stable_and_follows_the_band_rows(tmp_path):
    with PilotDataStore(tmp_path / "a", workspace="india") as one:
        full_world(one)
        first, again = coverage(one), coverage(one)
        assert first.report_sha256 == again.report_sha256
        assert one.query("SELECT count(*) FROM band_coverage_reports")[0][0] == 1
    with PilotDataStore(tmp_path / "b", workspace="india") as two:
        full_world(two, change=S3)
        assert coverage(two).report_sha256 != first.report_sha256
        assert coverage(two).fixed_count == 1  # only RELIANCE on S3 has a fixed band


def test_block_deal_t0_and_buyback_bars_do_not_make_the_band_ambiguous(store):
    # NSE lists never carry BL, T0 or BO; a block deal or a buyback window on the
    # stock's ISIN used to turn the session unknown (band_isin_ambiguous).
    bars(store, instruments=[("RELIANCE", "EQ", RELIANCE), ("RELIANCE", "BL", RELIANCE), ("CANBK", "EQ", CANBK),
                             ("CANBK", "T0", CANBK), ("CANBK", "BO", CANBK), ("AGSTRA", "BZ", AGSTRA)])
    with_convention(store)
    for session in SESS:
        put_list(store, FILE_FOR[session])
    report = coverage(store)
    assert report.unknown_count == 0 and report.phase62_blocked is False


def test_two_regular_series_on_one_isin_stay_ambiguous(store):
    bars(store, instruments=[("RELIANCE", "EQ", RELIANCE), ("RELIANCE", "BE", RELIANCE), ("CANBK", "EQ", CANBK),
                             ("AGSTRA", "BZ", AGSTRA)])
    with_convention(store)
    for session in SESS:
        put_list(store, FILE_FOR[session])
    report = coverage(store)
    assert report.unknown_by_reason.get("band_isin_ambiguous", 0) > 0 and report.phase62_blocked is True


def test_check_convention_reports_which_file_explains_the_differences(store):
    bars(store)
    put_list(store, S1)
    put_list(store, S2, rows=[dict(kit.SEC_LIST_RELIANCE, Band="10"), kit.SEC_LIST_CANBK, kit.SEC_LIST_AGSTRA])
    put_list(store, S3, rows=[dict(kit.SEC_LIST_RELIANCE, Band="10"), kit.SEC_LIST_CANBK, kit.SEC_LIST_AGSTRA])
    put_changes(store, S1, [{"Symbol": "RELIANCE", "Series": "EQ", "Security Name": "R", "From": "No Band", "To": "10"}])
    check = check_band_convention(store, start=S1, end=S3)
    assert (check.pairs_examined, check.explained_by_previous_date, check.explained_by_same_date) == (1, 1, 0)
    assert len(check.evidence_sha256) == 64 and check.rows[0]["older_list"] == S1.isoformat()
    assert store.query("SELECT count(*) FROM price_band_conventions")[0][0] == 0  # reading records nothing


def test_check_convention_sees_the_next_session_changes_file(store):
    # NSE 2021-2026: list D already carries the bands for the next session, and
    # the changes file dated that next session lists the move.
    bars(store)
    put_list(store, S1)
    put_list(store, S2, rows=[dict(kit.SEC_LIST_RELIANCE, Band="10"), kit.SEC_LIST_CANBK, kit.SEC_LIST_AGSTRA])
    put_changes(store, S3, [{"Symbol": "RELIANCE", "Series": "EQ", "Security Name": "R", "From": "No Band", "To": "10"}])
    check = check_band_convention(store, start=S1, end=S2)
    assert (check.pairs_examined, check.explained_by_previous_date, check.explained_by_same_date) == (1, 0, 0)
    assert (check.explained_by_next_session_after_newer, check.explained_by_neither) == (1, 0)
    assert check.rows[0]["next_session_after_newer"] == S3.isoformat()
    assert check.rows[0]["explained_by_next_session_changes"] == "True"


# --------------------------------------------------------------------------- CLI
def persist_targets(store):
    ensure_target_tables(store)
    store.append_rows(
        "target_universe_snapshots",
        [{"target_sha256": "t" * 64, "workspace": "india", "as_of": S5, "built_at_utc": utc_naive(datetime.now(timezone.utc)),
          "payload_json": json.dumps(TARGETS.model_dump(mode="json"), sort_keys=True),
          "source_sha256": "m" * 64, "row_sha256": "t" * 64}],
        check="target_universe",
    )


def mock_client(routes):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        route = routes.get(str(request.url), 404)
        return httpx.Response(200, content=route) if isinstance(route, bytes) else httpx.Response(route, content=b"x")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.calls = calls  # type: ignore[attr-defined]
    return client


def ddmmyyyy(day):
    return f"{day.day:02d}{day.month:02d}{day.year}"


def test_cli_ingest_range_archive_depth_convention_and_coverage(tmp_path, capsys):
    root = tmp_path / "root"
    with PilotDataStore(root, workspace="india") as store:
        bars(store)
        persist_targets(store)
    common = ["--root", str(root), "--workspace", "india"]
    routes = {}
    for session in SESS:
        routes[SEC_LIST_DATED_URL.format(ddmmyyyy=ddmmyyyy(session))] = kit.sec_list_csv(STANDARD)
    routes[BAND_CHANGES_URL.format(ddmmyyyy=ddmmyyyy(S2))] = kit.band_changes_csv([])  # the rest are 404
    client = mock_client(routes)
    code = main(["ingest-range", *common, "--start", S1.isoformat(), "--end", date(2025, 3, 9).isoformat(),
                 "--min-interval-seconds", "0", "--min-free-gib", "0"], client=client)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["result"]["fetched"] == 5 and out["result"]["skipped_not_session"] == 2
    with PilotDataStore(root, workspace="india", read_only=True) as check:
        assert check.query("SELECT file_kind, outcome, count(*) FROM price_band_fetch_log GROUP BY ALL "
                           "ORDER BY file_kind, outcome") == [("changes", "ingested", 1), ("changes", "no_file", 4),
                                                              ("list", "ingested", 5)]
    before = len(client.calls)
    assert main(["ingest-range", *common, "--start", S1.isoformat(), "--end", S5.isoformat(),
                 "--min-interval-seconds", "0", "--min-free-gib", "0"], client=client) == 0
    capsys.readouterr()
    assert len(client.calls) == before  # a resume makes no request for final days
    assert main(["ingest-range", *common, "--start", S1.isoformat(), "--end", S5.isoformat(),
                 "--min-interval-seconds", "0", "--min-free-gib", "100000000"], client=client) == 0  # all final
    capsys.readouterr()

    assert main(["archive-depth", *common]) == 0
    depth = json.loads(capsys.readouterr().out)["result"]
    assert depth["earliest_ingested_list"] == S1.isoformat() and depth["no_file_dates"] == []

    assert main(["check-convention", *common, "--start", S1.isoformat(), "--end", S5.isoformat()]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["caveats"][0]["code"] == "SURVIVORSHIP_BIAS" and "evidence_sha256" in printed["result"]
    with PilotDataStore(root, workspace="india", read_only=True) as check:
        assert check.query("SELECT count(*) FROM price_band_conventions")[0][0] == 0

    assert main(["coverage", *common, "--start", S1.isoformat(), "--end", S5.isoformat()]) == 3  # blocked
    blocked = json.loads(capsys.readouterr().out)["result"]
    assert blocked["phase62_blocked"] is True

    with pytest.raises(SystemExit):  # the three evidence flags are required
        main(["record-convention", *common, "--list-rule", "file_date"])
    capsys.readouterr()
    evidence = "ab" * 32
    assert main(["record-convention", *common, "--list-rule", "next_session_after_file_date",
                 "--changes-rule", "next_session_after_file_date", "--evidence-sha256", evidence]) == 0
    capsys.readouterr()
    with PilotDataStore(root, workspace="india", read_only=True) as check:
        assert current_band_convention(check).basis == "operator_confirmed"
    # lists exist for S1..S5 under the rule only from S2 (file date S1): S1 has no list effective, so it stays blocked
    assert main(["coverage", *common, "--start", S2.isoformat(), "--end", S5.isoformat()]) in (0, 3)
    result = json.loads(capsys.readouterr().out)["result"]
    path = root / "reports" / result["report_path"].rsplit("/", 1)[1]
    assert path.name.startswith(f"band-coverage-{S2.isoformat()}-{S5.isoformat()}-")
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    body = json.loads(path.read_text())
    assert list(body)[0] == "caveats" and "PRICE_BAND_UNSUPPORTED" in [c["code"] for c in body["caveats"]]


def test_cli_exit_codes_for_unblocked_coverage_and_errors(tmp_path, capsys):
    root = tmp_path / "ok"
    with PilotDataStore(root, workspace="india") as store:
        full_world(store)
        persist_targets(store)
    assert main(["coverage", "--root", str(root), "--workspace", "india", "--start", S1.isoformat(),
                 "--end", S5.isoformat()]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["phase62_blocked"] is False
    assert main(["coverage", "--root", str(tmp_path / "none"), "--workspace", "india", "--start", S1.isoformat(),
                 "--end", S5.isoformat()]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "target_universe_missing"


# ------------------------------------------------- real NSE variants seen in the five-year band run (2021-2026)
def test_nil_changes_file_means_no_changes():
    assert parse_band_changes(b"Sr. No,Symbol,Series,Security Name,From,To\r\nNil,,,,,\r\n") == ()
    assert parse_band_changes(b"Sr. No,Symbol,Series,Security Name,From,To\r\nNIL,,,,,\r\n") == ()
    # Jan-Mar 2023 form: a bare "Nil" with no commas, sometimes with trailing spaces.
    assert parse_band_changes(b"Sr. No,Symbol,Series,Security Name,From,To\nNil      ") == ()
    assert parse_band_changes(b"Sr. No,Symbol,Series,Security Name,From,To\r\nNil\r\n") == ()
    with pytest.raises(PilotDataError):  # the header is still verified on a Nil file
        parse_band_changes(b"Sr. No,Symbol,Series,Name,From,To\nNil\n")
    with pytest.raises(PilotDataError):  # a Nil row that carries data is not a no-change file
        parse_band_changes(b"Sr. No,Symbol,Series,Security Name,From,To\nNil,X,EQ,X,5,10\n")


def test_changes_file_with_a_remarks_column_parses():
    content = (b"Sr. No,Symbol,Series,Security Name,From,To,Remarks\n"
               b"1,GRPLTD,BE,GRP LIMITED,10,5,ASM\n2,MOTOGENFIN,EQ,THE MOTOR & GENERAL FINANCE LIMITED,10,5,Daily PB\n")
    rows = parse_band_changes(content)
    assert [(r.nse_symbol, r.from_percent, r.to_percent) for r in rows] == [
        ("GRPLTD", Decimal("10"), Decimal("5")), ("MOTOGENFIN", Decimal("10"), Decimal("5"))]


def test_blank_from_and_zero_to_are_not_banded():
    content = (b"Sr. No,Symbol,Series,Security Name,From,To\n"
               b"1,BLUECHIP,BE,BLUE CHIP INDIA LIMITED,,2\n2,GFSTEELS,BE,GRAND FOUNDRY LIMITED,5,0\n")
    entering, leaving = parse_band_changes(content)
    assert (entering.from_category, entering.to_category, entering.to_percent) == ("not_banded", "fixed", Decimal("2"))
    assert (leaving.from_category, leaving.to_category, leaving.to_percent) == ("fixed", "not_banded", None)
    with pytest.raises(PilotDataError):  # a blank To is not a valid move
        parse_band_changes(b"Sr. No,Symbol,Series,Security Name,From,To\n1,X,EQ,X,5,\n")


def test_lower_case_no_band_in_a_list_is_no_band():
    content = b'Symbol,Series,Security Name,Band,Remarks\nABC,EQ,ABC LTD,No band,"-"\nDEF,EQ,DEF LTD,No Band,"-"\n'
    assert [r.category for r in parse_sec_list(content)] == ["no_band", "no_band"]


def test_an_empty_published_list_is_settled_and_not_retried(tmp_path):
    root = tmp_path / "root"
    with PilotDataStore(root, workspace="india") as store:
        bars(store)
        routes = {SEC_LIST_DATED_URL.format(ddmmyyyy=ddmmyyyy(S1)): b"",
                  BAND_CHANGES_URL.format(ddmmyyyy=ddmmyyyy(S1)): kit.band_changes_csv([])}
        client = mock_client(routes)
        http = NseHttp(client, min_interval_seconds=0.0)
        first = ingest_range(store, http, S1, S1, workspace="india", min_free_bytes=0)
        assert first["failed"] == 0
        assert store.query("SELECT file_kind, outcome, error_code FROM price_band_fetch_log ORDER BY file_kind") == [
            ("changes", "ingested", None), ("list", "no_file", "nse_empty_file")]
        calls = len(client.calls)
        assert ingest_range(store, http, S1, S1, workspace="india", min_free_bytes=0)["skipped_final"] == 1
        assert len(client.calls) == calls

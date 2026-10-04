"""Date-bounded ISIN lineage from primary bhavcopy observations."""

from datetime import date, datetime, timedelta, timezone

import pytest

from pilot_data.bhavcopy import ingest_cm_legacy, ingest_pr_zip, ingest_udiff
from pilot_data.core import PilotDataError, SourceDescriptor
from pilot_data.corporate_actions import derive_corporate_actions, events_for_symbol
from pilot_data.lineage import (
    LINK_MAX_GAP_SESSIONS,
    build_lineage,
    lineage_for_stock_code,
    primary_bars_for_lineage,
)
from pilot_data.nse_ingest import _log_attempt
from pilot_data.security_master import ingest_security_master
from pilot_data.sessions import ensure_fetch_log
from pilot_data.store import PilotDataStore

import pilot_data_testkit as kit

FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
LATER = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
OLD, NEW = "INE476A01014", "INE476A01022"
D1, D2, D3, D4, D5 = (date(2025, 3, 3) + timedelta(days=i) for i in range(5))  # Mon to Fri


def desc(kind="x") -> SourceDescriptor:
    return SourceDescriptor(source="nse_archive", kind=kind, locator=f"https://nsearchives.nseindia.com/{kind}",
                            fetched_at=FETCHED)


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


def mark_calendar(store, start, end, data_days):
    """Write fetch-log outcomes so every day in [start, end] has a final status."""
    ensure_fetch_log(store)
    day = start
    while day <= end:
        if day in data_days:
            _log_attempt(store, d=day, kind="udiff", outcome="ingested", http_status=200, error_code=None,
                         source_sha256=None, url="u", attempted_at=LATER)
        elif day.weekday() >= 5:
            _log_attempt(store, d=day, kind="pr", outcome="no_file", http_status=404, error_code=None,
                         source_sha256=None, url="u", attempted_at=LATER)
        else:
            for kind in ("pr", "udiff", "cm_legacy"):
                _log_attempt(store, d=day, kind=kind, outcome="no_file", http_status=404, error_code=None,
                             source_sha256=None, url="u", attempted_at=LATER)
        day += timedelta(days=1)


def legacy(store, day, symbol, isin, series="EQ", close="105"):
    ingest_cm_legacy(
        store, desc("cm_legacy"),
        kit.cm_legacy_zip(day, [kit.cm_legacy_row(symbol, series, isin, "100", "110", "90", close, trade_date=day)]),
        trade_date=day,
    )


def udiff(store, day, rows):
    ingest_udiff(store, desc("udiff_cm"), kit.udiff_zip(day, rows), trade_date=day)


def urow(day, symbol, isin, token, series="EQ", close="105"):
    return kit.udiff_row(symbol, series, isin, "100", "110", "90", close, trade_date=day, token=str(token))


def announce(store, day, symbol, purpose, ex_date):
    ingest_pr_zip(
        store, desc("pr_zip"),
        kit.pr_zip(day, [kit.pd_index_row()], [kit.bc_row("EQ", symbol, symbol, purpose, ex_date=ex_date)], []),
        trade_date=day,
    )
    derive_corporate_actions(store, workspace="india")


def build(store, anchor=NEW, as_of=D4, series="EQ"):
    return build_lineage(store, anchor_isin=anchor, anchor_series=series, as_of=as_of, workspace="india")


def test_legacy_split_links_the_old_isin_with_the_event_id(store):
    legacy(store, D1, "CANBK", OLD)
    legacy(store, D2, "CANBK", OLD)
    legacy(store, D3, "CANBK", NEW)
    legacy(store, D4, "CANBK", NEW)
    announce(store, D1, "CANBK", "FVSPLT FRM RS 10 TO RS 2", D3)
    mark_calendar(store, D1, D4, {D1, D2, D3, D4})
    lineage = build(store)
    (event,) = events_for_symbol(store, "CANBK", ex_from=D3, ex_to=D3)
    assert [(s.isin, s.valid_from, s.valid_to, s.link) for s in lineage.segments] == [
        (OLD, D1, D2, "isin_change_split"), (NEW, D3, D4, "anchor")]
    assert lineage.segments[0].evidence == event.event_id
    assert lineage.isin_on(D2) == OLD and lineage.isin_on(D3) == NEW and lineage.isin_on(date(2025, 3, 1)) is None
    assert lineage.resolved_from == D1 and lineage.unresolved_before is None and lineage.basis == "bhavcopy_walk"


def test_udiff_split_with_an_unchanged_token_records_the_token_in_the_evidence(store):
    udiff(store, D1, [urow(D1, "JAIBALAJI", OLD, 11256)])
    udiff(store, D2, [urow(D2, "JAIBALAJI", OLD, 11256)])
    udiff(store, D3, [urow(D3, "JAIBALAJI", NEW, 11256)])
    announce(store, D1, "JAIBALAJI", "FVSPLT FRM RS 10 TO RS 2", D3)
    mark_calendar(store, D1, D3, {D1, D2, D3})
    lineage = build(store, as_of=D3)
    assert lineage.segments[0].evidence.endswith("|token:11256") and lineage.segments[0].token == 11256
    assert [s.isin for s in lineage.segments] == [OLD, NEW]


def test_a_bonus_with_the_isin_unchanged_is_one_segment(store):
    for day in (D1, D2, D3):
        legacy(store, day, "GARFIBRES", NEW)
    announce(store, D1, "GARFIBRES", "BONUS 4:1", D3)
    mark_calendar(store, D1, D3, {D1, D2, D3})
    lineage = build(store, as_of=D3)
    assert len(lineage.segments) == 1 and lineage.segments[0].link == "anchor"


def test_a_rename_with_the_same_isin_continues_the_lineage(store):
    isin = "INE155A01022"
    legacy(store, D1, "TATAMOTORS", isin)
    legacy(store, D2, "TATAMOTORS", isin)
    legacy(store, D3, "TMPV", isin)
    mark_calendar(store, D1, D3, {D1, D2, D3})
    lineage = build(store, anchor=isin, as_of=D3)
    assert [(s.nse_symbol, s.link) for s in lineage.segments] == [("TATAMOTORS", "isin_continuity"), ("TMPV", "anchor")]
    assert lineage.symbol_on(D2) == "TATAMOTORS" and lineage.symbol_on(D3) == "TMPV"
    assert lineage.isin_on(D1) == isin == lineage.isin_on(D3)


def test_isin_change_without_a_split_event_stops_the_walk(store):
    udiff(store, D1, [urow(D1, "ABC", OLD, 77)])
    udiff(store, D2, [urow(D2, "ABC", NEW, 77)])
    udiff(store, D3, [urow(D3, "ABC", NEW, 77)])
    mark_calendar(store, D1, D3, {D1, D2, D3})
    lineage = build(store, as_of=D3)
    assert lineage.unresolved_before == D2 and lineage.unresolved_reason == "isin_change_unexplained"
    assert lineage.resolved_from == D2 and lineage.isin_on(D1) is None
    assert [s.isin for s in lineage.segments] == [NEW]


def test_a_split_event_on_the_wrong_date_does_not_bridge(store):
    udiff(store, D1, [urow(D1, "ABC", OLD, 77)])
    udiff(store, D2, [urow(D2, "ABC", NEW, 77)])
    announce(store, D1, "ABC", "FVSPLT FRM RS 10 TO RS 2", D3)
    mark_calendar(store, D1, D2, {D1, D2})
    assert build(store, as_of=D2).unresolved_reason == "isin_change_unexplained"


def test_bonus_event_on_the_change_date_is_not_split_evidence(store):
    udiff(store, D1, [urow(D1, "ABC", OLD, 77)])
    udiff(store, D2, [urow(D2, "ABC", NEW, 77)])
    announce(store, D1, "ABC", "BONUS 1:1", D2)
    mark_calendar(store, D1, D2, {D1, D2})
    assert build(store, as_of=D2).unresolved_reason == "isin_change_unexplained"


def test_two_candidate_predecessors_are_ambiguous(store):
    other = "INE002A01018"
    udiff(store, D1, [urow(D1, "AAA", OLD, 5), urow(D1, "BBB", other, 5)])
    udiff(store, D2, [urow(D2, "CCC", NEW, 5)])
    mark_calendar(store, D1, D2, {D1, D2})
    lineage = build(store, as_of=D2)
    assert lineage.unresolved_reason == "isin_change_ambiguous" and lineage.unresolved_before == D2


def test_a_predecessor_too_many_sessions_back_is_not_linked(store):
    days = []
    day = D1
    while len(days) < LINK_MAX_GAP_SESSIONS + 3:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    first, last = days[0], days[-1]
    for index, current in enumerate(days):
        rows = [urow(current, "FILL", "INE002A01018", 1)]
        if index == 0:
            rows.append(urow(current, "ABC", OLD, 9))
        if index == len(days) - 1:
            rows.append(urow(current, "ABC", NEW, 9))
        udiff(store, current, rows)
    announce(store, first, "ABC", "FVSPLT FRM RS 10 TO RS 2", last)
    mark_calendar(store, first, last, set(days))
    lineage = build(store, as_of=last)
    assert lineage.unresolved_reason == "isin_change_gap_too_long" and lineage.unresolved_before == last


def test_a_mid_history_listing_keeps_its_shorter_history(store):
    legacy(store, D1, "OTHER", "INE002A01018")
    legacy(store, D3, "NEWCO", NEW)
    legacy(store, D4, "NEWCO", NEW)
    mark_calendar(store, D1, D4, {D1, D3, D4})
    lineage = build(store)
    assert lineage.resolved_from == D3 and lineage.unresolved_before is None and lineage.unresolved_reason is None


def test_unobserved_anchor_and_unknown_days_fail_closed(store):
    legacy(store, D1, "OTHER", "INE002A01018")
    mark_calendar(store, D1, D1, {D1})
    with pytest.raises(PilotDataError) as unobserved:
        build(store)
    assert unobserved.value.code == "lineage_anchor_not_observed"
    legacy(store, D3, "NEWCO", NEW)
    mark_calendar(store, D3, D3, {D3})  # D2 (a Tuesday) has no recorded outcome
    with pytest.raises(PilotDataError) as unknown:
        build(store, as_of=D3)
    assert unknown.value.code == "calendar_unknown_dates"


def test_content_hash_is_stable_and_changes_with_new_observations(store):
    legacy(store, D2, "ABC", NEW)
    legacy(store, D3, "ABC", NEW)
    mark_calendar(store, D1, D3, {D2, D3})
    first = build(store, as_of=D3).content_sha256()
    assert build(store, as_of=D3).content_sha256() == first
    legacy(store, D1, "ABC", NEW)
    mark_calendar(store, D1, D1, {D1})
    assert build(store, as_of=D3).content_sha256() != first


def test_lineage_for_stock_code_resolves_through_the_master(store):
    ingest_security_master(store, desc("security_master"), kit.security_master_bytes(kit.MASTER_SAMPLE_ROWS),
                           snapshot_date=date(2025, 3, 1), snapshot_date_basis="test_fixture")
    udiff(store, D1, [kit.UDIFF_RELIANCE_20250102 | {"TradDt": D1.isoformat(), "BizDt": D1.isoformat()}])
    mark_calendar(store, D1, D1, {D1})
    lineage = lineage_for_stock_code(store, stock_code="RELIND", as_of=D1, workspace="india")
    assert lineage.anchor_isin == "INE002A01018" and lineage.stock_code == "RELIND"
    with pytest.raises(PilotDataError) as unmapped:
        lineage_for_stock_code(store, stock_code="ACRTEC", as_of=D1, workspace="india")
    assert unmapped.value.code == "stock_code_unmapped"


def test_primary_bars_follow_the_isin_on_each_date_and_prefer_udiff(store):
    legacy(store, D1, "CANBK", OLD, close="101")
    legacy(store, D2, "CANBK", NEW, close="102")
    udiff(store, D2, [urow(D2, "CANBK", NEW, 3, close="103")])  # both formats exist on D2
    legacy(store, D3, "CANBK", NEW, close="104")
    announce(store, D1, "CANBK", "FVSPLT FRM RS 10 TO RS 2", D2)
    mark_calendar(store, D1, D3, {D1, D2, D3})
    lineage = build(store, as_of=D3)
    bars = primary_bars_for_lineage(store, lineage, start=D1, end=D3)
    assert [(b.trade_date, b.isin, str(b.close), b.source_kind) for b in bars] == [
        (D1, OLD, "101.0000", "bhavcopy"), (D2, NEW, "103.0000", "bhavcopy"), (D3, NEW, "104.0000", "bhavcopy")]
    assert [b.trade_date for b in primary_bars_for_lineage(store, lineage, start=D2, end=D2)] == [D2]

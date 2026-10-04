"""Per-name spans, internal and trailing gaps, and unresolved-identity quarantine."""

from datetime import date, datetime, timedelta, timezone

import pytest

from pilot_data.bhavcopy import ingest_udiff
from pilot_data.core import SourceDescriptor
from pilot_data.history_quality import compute_span, record_history_quarantines
from pilot_data.lineage import build_lineage
from pilot_data.models import IsinSegment, Lineage
from pilot_data.nse_ingest import _log_attempt
from pilot_data.sessions import ensure_fetch_log
from pilot_data.store import PilotDataStore

import pilot_data_testkit as kit

FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
LATER = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
ISIN = "INE002A01018"
OTHER = "INE040A01034"
DAYS = [date(2025, 3, 3) + timedelta(days=i) for i in range(0, 9)]
SESSIONS = [d for d in DAYS if d.weekday() < 5]  # 3,4,5,6,7,10,11 March 2025
S1, S2, S3, S4, S5, S6, S7 = SESSIONS


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
            kinds = ("pr",) if day.weekday() >= 5 else ("pr", "udiff", "cm_legacy")
            for kind in kinds:
                _log_attempt(store, d=day, kind=kind, outcome="no_file", http_status=404, error_code=None,
                             source_sha256=None, url="u", attempted_at=LATER)
        day += timedelta(days=1)


def load(store, present_days, *, filler=True):
    """Ingest a UDiFF day per session: ISIN rows on present_days, an unrelated filler row otherwise."""
    for day in SESSIONS:
        rows = []
        if day in present_days:
            rows.append(kit.udiff_row("RELIANCE", "EQ", ISIN, "100", "110", "90", "105", trade_date=day, token="2885"))
        if filler:
            rows.append(kit.udiff_row("HDFCBANK", "EQ", OTHER, "100", "110", "90", "105", trade_date=day, token="1333"))
        ingest_udiff(
            store,
            SourceDescriptor(source="nse_archive", kind="udiff_cm", locator="https://nsearchives.nseindia.com/u",
                             fetched_at=FETCHED),
            kit.udiff_zip(day, rows), trade_date=day,
        )
    mark_calendar(store, S1, S7, set(SESSIONS))


def lineage(store):
    return build_lineage(store, anchor_isin=ISIN, anchor_series="EQ", as_of=S7, workspace="india")


def span(store, lin):
    return compute_span(store, lin, window_start=S1, window_end=S7, workspace="india")


def test_internal_gap_is_merged_into_one_range_and_quarantined(store):
    load(store, {S1, S2, S5, S6, S7})
    lin = lineage(store)
    report = span(store, lin)
    assert [(g.start, g.end, g.sessions) for g in report.gap_ranges] == [(S3, S4, 2)]
    assert report.trailing_gap is None and not report.short_history and not report.listed_within_window
    assert (report.sessions_expected, report.sessions_present) == (7, 5)
    assert (report.first_session, report.last_session) == (S1, S7)
    records = record_history_quarantines(store, lin, report, workspace="india")
    assert [(r.check, r.reason_code, r.scope, r.isin, r.date_from, r.date_to) for r in records] == [
        ("history_gap", "internal_gap", "both", ISIN, S3, S4)]
    assert records[0].evidence_sha256s == (lin.content_sha256(),)


def test_a_name_listed_mid_window_keeps_its_shorter_history_without_quarantine(store):
    load(store, {S3, S4, S5, S6, S7})
    lin = lineage(store)
    report = span(store, lin)
    assert report.short_history and report.listed_within_window and report.first_session == S3
    assert report.gap_ranges == () and report.trailing_gap is None
    assert (report.sessions_expected, report.sessions_present) == (5, 5)
    assert record_history_quarantines(store, lin, report, workspace="india") == ()
    assert store.query("SELECT count(*) FROM quarantine_records")[0][0] == 0


def manual_lineage(resolved_from):
    return Lineage(
        workspace="india", anchor_isin=ISIN, anchor_series="EQ", stock_code="RELIND",
        segments=(IsinSegment(isin=ISIN, nse_symbol="RELIANCE", valid_from=resolved_from, valid_to=S7,
                              link="anchor"),),
        resolved_from=resolved_from, built_as_of=S7, basis="bhavcopy_walk",
    )


def test_a_covered_session_before_the_first_bar_is_a_quarantined_gap_not_a_listing(store):
    load(store, {S3, S4, S5, S6, S7})
    lin = manual_lineage(S1)  # the lineage says the name existed from S1, but S1 and S2 have no bar
    report = span(store, lin)
    assert report.first_session == S3 and report.short_history
    assert not report.listed_within_window
    assert [(g.start, g.end, g.sessions) for g in report.gap_ranges] == [(S1, S2, 2)]
    assert (report.sessions_expected, report.sessions_present) == (7, 5)
    (record,) = record_history_quarantines(store, lin, report, workspace="india")
    assert (record.check, record.reason_code, record.scope, record.date_from, record.date_to) == (
        "history_gap", "internal_gap", "both", S1, S2)


def test_a_lineage_that_starts_before_the_window_with_a_late_first_bar_is_not_a_listing(store):
    load(store, {S4, S5, S6, S7})
    lin = manual_lineage(S1 - timedelta(days=30))
    report = span(store, lin)
    assert [(g.start, g.end) for g in report.gap_ranges] == [(S1, S3)]
    assert not report.listed_within_window


def test_trailing_gap_is_quarantined(store):
    load(store, {S1, S2, S3, S4})
    lin = lineage(store)
    report = span(store, lin)
    assert report.last_session == S4
    assert report.trailing_gap is not None and (report.trailing_gap.start, report.trailing_gap.end) == (S5, S7)
    (record,) = record_history_quarantines(store, lin, report, workspace="india")
    assert (record.check, record.reason_code, record.scope, record.date_from, record.date_to) == (
        "history_gap", "trailing_gap", "both", S5, S7)


def test_unresolved_identity_inside_the_window_is_quarantined(store):
    load(store, {S1, S2, S3, S4, S5, S6, S7})
    resolved_from = S4
    lin = Lineage(
        workspace="india", anchor_isin=ISIN, anchor_series="EQ", stock_code="RELIND",
        segments=(IsinSegment(isin=ISIN, nse_symbol="RELIANCE", valid_from=S4, valid_to=S7, link="anchor"),),
        resolved_from=resolved_from, unresolved_before=resolved_from, unresolved_reason="isin_change_unexplained",
        built_as_of=S7, basis="bhavcopy_walk",
    )
    report = span(store, lin)
    assert report.unresolved_before == S4 and not report.listed_within_window and report.short_history
    assert (report.sessions_expected, report.sessions_present) == (4, 4)
    records = record_history_quarantines(store, lin, report, workspace="india")
    (identity,) = records
    assert (identity.check, identity.reason_code, identity.scope) == ("identity", "isin_unresolved", "both")
    assert (identity.date_from, identity.date_to) == (S1, S4 - timedelta(days=1))
    assert identity.detail["unresolved_reason"] == "isin_change_unexplained"
    assert identity.stock_code == "RELIND"


def test_recording_twice_inserts_no_new_rows(store):
    load(store, {S1, S2, S5, S6})
    lin = lineage(store)
    report = span(store, lin)
    first = record_history_quarantines(store, lin, report, workspace="india")
    count = store.query("SELECT count(*) FROM quarantine_records")[0][0]
    assert count == len(first) == 2  # one internal gap and one trailing gap
    record_history_quarantines(store, lin, report, workspace="india")
    assert store.query("SELECT count(*) FROM quarantine_records")[0][0] == count


def test_span_report_carries_caveats_and_the_lineage_hash(store):
    load(store, set(SESSIONS))
    lin = lineage(store)
    report = span(store, lin)
    assert [c.code for c in report.caveats][:2] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS"]
    assert report.lineage_sha256 == lin.content_sha256() and report.anchor_isin == ISIN
    assert report.gap_ranges == () and report.trailing_gap is None
    assert report.content_sha256() == span(store, lin).content_sha256()

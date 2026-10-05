"""Session calendar, resumable range ingestion and the nse_ingest CLI (no network)."""

import json
from datetime import date, datetime, timezone

import httpx
import pytest

from pilot_data.core import PilotDataError
from pilot_data.corporate_actions import EVENTS_TABLE
from pilot_data.nse_http import NseHttp
from pilot_data.nse_ingest import (
    cm_legacy_url,
    ingest_day,
    ingest_range,
    main,
    pr_zip_url,
    udiff_url,
)
from pilot_data.sessions import day_status, pr_missing_sessions, previous_sessions, sessions_between
from pilot_data.store import PilotDataStore

import pilot_data_testkit as kit

TUE = date(2024, 10, 1)
HOLIDAY = date(2024, 10, 2)  # Gandhi Jayanti
THU = date(2024, 10, 3)
SAT = date(2024, 10, 5)
SUN = date(2024, 10, 6)
LEGACY_DAY = date(2023, 6, 1)


def at(day: date, hour: int = 10) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=timezone.utc)


class Clock:
    """Fake monotonic clock whose sleeper advances it, plus a record of requested sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self):
        self.now += 0.001
        return self.now


class Site:
    def __init__(self):
        self.routes: dict[str, object] = {}
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        route = self.routes.get(url, 404)
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, bytes):
            return httpx.Response(200, content=route)
        return httpx.Response(route, content=b"<html>" + b"x" * 3400 + b"</html>")

    def http(self) -> NseHttp:
        clock = Clock()
        return NseHttp(httpx.Client(transport=httpx.MockTransport(self.handler)), min_interval_seconds=0.0,
                       sleeper=clock.sleep, monotonic=clock.monotonic)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def udiff(self, day, rows=None):
        rows = rows if rows is not None else [kit.UDIFF_RELIANCE_20250102]
        self.routes[udiff_url(day)] = kit.udiff_zip(day, [dict(r, TradDt=day.isoformat(), BizDt=day.isoformat()) for r in rows])

    def pr(self, day, bc_rows=(), pd_rows=None):
        self.routes[pr_zip_url(day)] = kit.pr_zip(day, pd_rows if pd_rows is not None else [kit.pd_index_row()],
                                                  list(bc_rows), [])

    def legacy(self, day):
        row = dict(kit.CM_CANBK_20240514, TIMESTAMP=kit.legacy_stamp(day))
        self.routes[cm_legacy_url(day)] = kit.cm_legacy_zip(day, [row])


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


def test_url_builders():
    assert pr_zip_url(date(2024, 5, 2)) == "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR020524.zip"
    assert udiff_url(date(2025, 1, 2)).endswith("/content/cm/BhavCopy_NSE_CM_0_0_0_20250102_F_0000.csv.zip")
    assert cm_legacy_url(date(2024, 5, 14)).endswith("/content/historical/EQUITIES/2024/MAY/cm14MAY2024bhav.csv.zip")


def test_weekday_with_udiff_is_a_session_and_never_asks_for_legacy(store):
    site = Site()
    site.udiff(TUE)
    outcome = ingest_day(store, site.http(), TUE, workspace="india", clock=lambda: at(TUE + (THU - TUE)))
    assert outcome.status == "session" and day_status(store, TUE) == "session"
    assert cm_legacy_url(TUE) not in site.calls


def test_udiff_404_then_legacy_200_is_a_session_from_cm_legacy(store):
    site = Site()
    site.legacy(LEGACY_DAY)
    outcome = ingest_day(store, site.http(), LEGACY_DAY, workspace="india", clock=lambda: at(THU))
    assert outcome.status == "session" and outcome.outcomes["cm_legacy"] == "ingested"
    assert store.query("SELECT file_kind FROM bhavcopy_files") == [("cm_legacy",)]


def test_holiday_needs_every_404_on_a_later_ist_date(store):
    site = Site()
    ingest_day(store, site.http(), HOLIDAY, workspace="india", clock=lambda: at(THU))
    assert day_status(store, HOLIDAY) == "holiday"
    assert [pr_zip_url(HOLIDAY), udiff_url(HOLIDAY), cm_legacy_url(HOLIDAY)] == site.calls


def test_same_day_404_is_pending_and_sessions_between_raises(store):
    site = Site()
    ingest_day(store, site.http(), HOLIDAY, workspace="india", clock=lambda: at(HOLIDAY, 10))
    assert day_status(store, HOLIDAY) == "pending"
    with pytest.raises(PilotDataError) as caught:
        sessions_between(store, HOLIDAY, HOLIDAY)
    assert caught.value.code == "calendar_unknown_dates" and "2024-10-02" in str(caught.value)
    # a retry on a later IST date turns pending into a final holiday
    ingest_range(store, site.http(), HOLIDAY, HOLIDAY, workspace="india", clock=lambda: at(THU),
                 min_free_bytes=0)
    assert day_status(store, HOLIDAY) == "holiday"


def test_ist_date_not_utc_date_decides_pending(store):
    site = Site()
    # 20:00 UTC on 2 Oct is 01:30 IST on 3 Oct, so the attempt counts as later than the trading date
    ingest_day(store, site.http(), HOLIDAY, workspace="india", clock=lambda: at(HOLIDAY, 20))
    assert day_status(store, HOLIDAY) == "holiday"


def test_saturday_404_is_weekend_after_one_request_and_200_triggers_primary(store):
    site = Site()
    ingest_day(store, site.http(), SAT, workspace="india", clock=lambda: at(SUN))
    assert day_status(store, SAT) == "weekend_no_session"
    assert site.calls == [pr_zip_url(SAT)]
    special = Site()
    special.pr(SUN)
    special.udiff(SUN)
    ingest_day(store, special.http(), SUN, workspace="india", clock=lambda: at(date(2024, 10, 7)))
    assert day_status(store, SUN) == "session"
    assert udiff_url(SUN) in special.calls


def _misdated_pr(real_day: date) -> bytes:
    # A zip whose members are dated real_day, served under another date's URL.
    return kit.pr_zip(real_day, [kit.pd_index_row()], [], [])


def test_saturday_with_a_misdated_pr_and_no_primaries_is_weekend(store):
    # Real case: PR060424.zip (Saturday 6 April 2024) holds the 4 June 2024 members.
    sat = date(2024, 4, 6)
    site = Site()
    site.routes[pr_zip_url(sat)] = _misdated_pr(date(2024, 6, 4))
    outcome = ingest_day(store, site.http(), sat, workspace="india", clock=lambda: at(date(2024, 4, 8)))
    assert outcome.failed  # the PR attempt itself is still recorded as failed
    assert site.calls == [pr_zip_url(sat), udiff_url(sat), cm_legacy_url(sat)]
    assert day_status(store, sat) == "weekend_no_session"


def test_saturday_with_a_misdated_pr_but_a_bhavcopy_is_a_session(store):
    sat = date(2024, 4, 6)
    site = Site()
    site.routes[pr_zip_url(sat)] = _misdated_pr(date(2024, 6, 4))
    site.udiff(sat)
    ingest_day(store, site.http(), sat, workspace="india", clock=lambda: at(date(2024, 4, 8)))
    assert day_status(store, sat) == "session"


def test_saturday_with_a_misdated_pr_checked_same_day_stays_unsettled(store):
    sat = date(2024, 4, 6)
    site = Site()
    site.routes[pr_zip_url(sat)] = _misdated_pr(date(2024, 6, 4))
    ingest_day(store, site.http(), sat, workspace="india", clock=lambda: at(sat))
    assert day_status(store, sat) not in {"weekend_no_session", "holiday", "session"}


def test_saturday_pr_transport_failure_stays_unknown_without_primary_requests(store):
    sat = date(2024, 4, 6)
    site = Site()
    site.routes[pr_zip_url(sat)] = 503
    ingest_day(store, site.http(), sat, workspace="india", clock=lambda: at(date(2024, 4, 8)))
    assert udiff_url(sat) not in site.calls
    assert day_status(store, sat) == "unknown"


def test_pr_200_with_both_primaries_404_is_inconsistent(store):
    site = Site()
    site.pr(TUE)
    ingest_day(store, site.http(), TUE, workspace="india", clock=lambda: at(THU))
    assert day_status(store, TUE) == "inconsistent"
    with pytest.raises(PilotDataError):
        sessions_between(store, TUE, TUE)


def test_repeated_503_is_unknown_and_sessions_between_lists_the_date(store):
    site = Site()
    for url in (pr_zip_url(TUE), udiff_url(TUE), cm_legacy_url(TUE)):
        site.routes[url] = 503
    outcome = ingest_day(store, site.http(), TUE, workspace="india", clock=lambda: at(THU))
    assert outcome.failed and day_status(store, TUE) == "unknown"
    with pytest.raises(PilotDataError) as caught:
        sessions_between(store, TUE, TUE)
    assert caught.value.code == "calendar_unknown_dates"


def test_an_ingested_session_survives_a_later_404_on_every_file(store):
    site = Site()
    site.udiff(TUE)
    ingest_day(store, site.http(), TUE, workspace="india", clock=lambda: at(THU))
    assert day_status(store, TUE) == "session"
    del site.routes[udiff_url(TUE)]  # every file now answers 404
    ingest_day(store, site.http(), TUE, workspace="india", clock=lambda: at(date(2024, 10, 8)))
    assert day_status(store, TUE) == "session"
    assert sessions_between(store, TUE, TUE) == (TUE,)


def test_an_ingested_session_survives_a_later_transport_failure(store):
    site = Site()
    site.udiff(TUE)
    ingest_day(store, site.http(), TUE, workspace="india", clock=lambda: at(THU))
    for url in (pr_zip_url(TUE), udiff_url(TUE), cm_legacy_url(TUE)):
        site.routes[url] = 503
    later = ingest_day(store, site.http(), TUE, workspace="india", clock=lambda: at(date(2024, 10, 8)))
    assert later.failed
    assert day_status(store, TUE) == "session"
    assert sessions_between(store, TUE, TUE) == (TUE,)


def test_an_ingested_pr_file_is_not_undone_by_a_later_404(store):
    site = Site()
    site.pr(TUE)
    ingest_day(store, site.http(), TUE, workspace="india", clock=lambda: at(THU))
    assert day_status(store, TUE) == "inconsistent"
    del site.routes[pr_zip_url(TUE)]
    ingest_day(store, site.http(), TUE, workspace="india", clock=lambda: at(date(2024, 10, 8)))
    assert day_status(store, TUE) == "inconsistent"  # never re-read as a plain holiday


def test_resume_makes_no_requests_for_final_days_and_retries_the_rest(store):
    site = Site()
    site.udiff(TUE)
    site.routes[udiff_url(THU)] = [503, 503, 503]
    site.routes[pr_zip_url(THU)] = 503
    site.routes[cm_legacy_url(THU)] = 404
    clock = lambda: at(date(2024, 10, 8))
    first = ingest_range(store, site.http(), TUE, THU, workspace="india", clock=clock, min_free_bytes=0)
    assert first.statuses == {"holiday": 1, "session": 1, "unknown": 1} and first.failed_dates == (THU,)
    site.calls.clear()
    site.udiff(THU)
    second = ingest_range(store, site.http(), TUE, THU, workspace="india", clock=clock, min_free_bytes=0)
    assert second.skipped_final == 2 and second.days_processed == 1
    assert all(str(THU.day).zfill(2) in url or "20241003" in url or "031024" in url or "03OCT" in url
               for url in site.calls)
    assert day_status(store, THU) == "session"
    assert sessions_between(store, TUE, THU) == (TUE, THU)


def test_five_consecutive_failed_days_abort(store):
    site = Site()
    start = date(2024, 9, 2)  # Monday
    for offset in range(7):
        day = date(2024, 9, 2 + offset)
        for url in (pr_zip_url(day), udiff_url(day), cm_legacy_url(day)):
            site.routes[url] = 500
    with pytest.raises(PilotDataError) as caught:
        ingest_range(store, site.http(), start, date(2024, 9, 8), workspace="india", clock=lambda: at(THU),
                     min_free_bytes=0)
    assert caught.value.code == "nse_ingest_aborted"
    assert store.query("SELECT count(DISTINCT trade_date) FROM nse_fetch_log")[0][0] == 5


def test_disk_floor_stops_before_the_first_fetch_and_between_days(store):
    site = Site()
    site.udiff(TUE)
    site.udiff(THU)
    with pytest.raises(PilotDataError) as caught:
        ingest_range(store, site.http(), TUE, THU, workspace="india", min_free_bytes=1000,
                     disk_free=lambda path: 10)
    assert caught.value.code == "disk_floor_reached" and site.calls == []
    readings = iter([10_000, 10_000, 5])
    clock = lambda: at(date(2024, 10, 8))
    with pytest.raises(PilotDataError) as second:
        ingest_range(store, site.http(), TUE, THU, workspace="india", clock=clock, min_free_bytes=1000,
                     disk_free=lambda path: next(readings))
    assert second.value.code == "disk_floor_reached"
    assert day_status(store, TUE) == "session"  # ingested before the floor was hit and stays final
    assert day_status(store, THU) == "unknown"


def test_consistency_runs_per_day_and_corporate_actions_after_the_range(store):
    site = Site()
    site.udiff(TUE, [kit.UDIFF_RELIANCE_20250102])
    site.pr(
        TUE,
        bc_rows=[kit.bc_row("EQ", "RELIANCE", "RELIANCE INDUSTRIES", "BONUS 1:1", ex_date=date(2024, 12, 1))],
        pd_rows=[kit.pd_row("RELIANCE", "EQ", "1221.25", "1260.00", "1220.00", "1250.00", volume="15486276",
                            value="19115027208.35")],
    )
    outcome = ingest_range(store, site.http(), TUE, TUE, workspace="india", clock=lambda: at(THU), min_free_bytes=0)
    assert outcome.quarantines_by_reason == {"pd_primary_mismatch": 1}
    assert store.query(f"SELECT purpose_norm FROM {EVENTS_TABLE}") == [("BONUS 1:1",)]


def test_previous_sessions_and_pr_missing(store):
    site = Site()
    for day in (date(2024, 9, 30), TUE, THU):
        site.udiff(day)
    site.pr(TUE)
    ingest_range(store, site.http(), date(2024, 9, 28), THU, workspace="india", clock=lambda: at(date(2024, 10, 8)),
                 min_free_bytes=0)
    assert previous_sessions(store, THU, 3) == (date(2024, 9, 30), TUE, THU)
    assert pr_missing_sessions(store, date(2024, 9, 30), THU) == (date(2024, 9, 30), THU)
    with pytest.raises(PilotDataError):
        previous_sessions(store, THU, 50)


def test_main_prints_caveats_first_and_exits_zero_on_success(tmp_path, capsys):
    site = Site()
    site.udiff(TUE)
    code = main(["--root", str(tmp_path / "root"), "--workspace", "india", "--start", "2024-10-01", "--end",
                 "2024-10-01", "--min-interval-seconds", "0", "--min-free-gib", "0"], client=site.client())
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert list(payload)[0] == "caveats"
    assert [c["code"] for c in payload["caveats"]][:2] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS"]
    assert payload["range"]["statuses"] == {"session": 1}


def test_main_exits_two_on_failed_days_and_on_disk_floor(tmp_path, capsys):
    site = Site()
    for url in (pr_zip_url(TUE), udiff_url(TUE), cm_legacy_url(TUE)):
        site.routes[url] = 403  # refused, not retried
    code = main(["--root", str(tmp_path / "root"), "--workspace", "india", "--start", "2024-10-01", "--end",
                 "2024-10-01", "--min-interval-seconds", "0", "--min-free-gib", "0"], client=site.client())
    assert code == 2
    assert json.loads(capsys.readouterr().out)["range"]["failed_dates"] == ["2024-10-01"]
    code = main(["--root", str(tmp_path / "root2"), "--workspace", "india", "--start", "2024-10-01", "--end",
                 "2024-10-01", "--min-free-gib", "100000000"], client=Site().client())
    assert code == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "disk_floor_reached"

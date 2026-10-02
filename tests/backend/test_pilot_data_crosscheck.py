"""Universe-wide cross-check: lineage splits, duplicate conflicts, series mismatch, rawness and accepted sets."""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pilot_data.bhavcopy import ingest_pr_zip, ingest_udiff
from pilot_data.breeze_bars import breeze_bars_for, ingest_breeze_response
from pilot_data.core import PilotDataError, SourceDescriptor, standard_caveats
from pilot_data.corporate_actions import derive_corporate_actions
from pilot_data.crosscheck import accepted_bars, main, run_crosscheck
from pilot_data.models import CrossCheckTolerances, DailyBarConvention, QuarantineRecord
from pilot_data.nse_ingest import _log_attempt
from pilot_data.sessions import ensure_fetch_log
from pilot_data.store import PilotDataStore
from pilot_data.targets import TargetMember, TargetUniverseResult, ensure_target_tables
from pilot_data.core import utc_naive

import pilot_data_testkit as kit

FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
LATER = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
CONVENTION = DailyBarConvention(allowed_times=("00:00:00",), source="test-fixture")
OLD, NEW, STEADY_ISIN, OTHER_ISIN = "INE476A01014", "INE476A01022", "INE002A01018", "INE040A01034"
DAYS = [date(2025, 3, 3) + timedelta(days=i) for i in range(7)]  # Mon 3 March to Sun 9 March
SESSIONS = DAYS[:5]
D1, D2, D3, D4, D5 = SESSIONS
SAT = DAYS[5]
AS_OF, START, END = D5, DAYS[0], DAYS[-1]

# (open, high, low, close) of CANBK in raw, as-traded terms: a 10 to 2 split takes effect on D3
CANBK = {
    D1: ("555.4", "569", "553.55", "566.55"), D2: ("561", "565", "558", "560"),
    D3: ("116.25", "119.6", "116", "119"), D4: ("119", "121", "118", "120"), D5: ("120", "122", "119", "121"),
}
STEADY = {day: ("100", "105", "95", "100") for day in SESSIONS}


def desc(source="nse_archive", kind="udiff_cm", locator="https://nsearchives.nseindia.com/x") -> SourceDescriptor:
    return SourceDescriptor(source=source, kind=kind, locator=locator, fetched_at=FETCHED)


def ohlc_row(symbol, series, isin, prices, day, token):
    o, h, l, c = prices
    return kit.udiff_row(symbol, series, isin, o, h, l, c, trade_date=day, token=token)


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


def breeze_rows(code, prices_by_day, *, scale=Decimal(1), overrides=None):
    rows = []
    for day, prices in sorted(prices_by_day.items()):
        o, h, l, c = (str((Decimal(p) * scale).quantize(Decimal("0.0001"))) for p in prices)
        o, h, l, c = (overrides or {}).get(day, (o, h, l, c))
        rows.append(kit.breeze_row(code, f"{day.isoformat()} 00:00:00", o, h, l, c))
    return rows


def load_breeze(store, code, rows, start=START, end=END, locator=None):
    return ingest_breeze_response(
        store, desc("breeze_relay", "breeze_v2_1day", locator or f"relay/{code}/{start}"), kit.breeze_v2_json(rows),
        stock_code=code, requested_from=start, requested_to=end, convention=CONVENTION,
    )


def member(symbol, isin, code):
    return TargetMember(kind="nifty500", anchor_isin=isin, nse_symbol=symbol, stock_code=code, token=1,
                        company_name=symbol)


def make_targets(*members):
    return TargetUniverseResult(
        workspace="india", caveats=standard_caveats(), as_of=AS_OF, members=members, exclusions=(), etf_rejected=(),
        master_snapshot="m" * 64, nifty500_snapshot="n" * 64, target_sha256="t" * 64,
    )


CANBK_MEMBER = member("CANBK", NEW, "CANBAN")
STEADY_MEMBER = member("STEADYCO", STEADY_ISIN, "STEADY")
OTHER_MEMBER = member("OTHERCO", OTHER_ISIN, "OTHERC")


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


def load_bhavcopy(store, *, canbk_series=None):
    for day in SESSIONS:
        series_for_canbk = (canbk_series or {}).get(day, "EQ")
        rows = [
            ohlc_row("CANBK", series_for_canbk, OLD if day < D3 else NEW, CANBK[day], day, "11256"),
            ohlc_row("STEADYCO", "EQ", STEADY_ISIN, STEADY[day], day, "5"),
            ohlc_row("OTHERCO", "EQ", OTHER_ISIN, STEADY[day], day, "9"),
        ]
        ingest_udiff(store, desc(), kit.udiff_zip(day, rows), trade_date=day)
    ingest_pr_zip(
        store, desc(kind="pr_zip"),
        kit.pr_zip(D1, [kit.pd_index_row()], [kit.bc_row("EQ", "CANBK", "CANARA BANK", "FVSPLT FRM RS 10 TO RS 2",
                                                         ex_date=D3)], []),
        trade_date=D1,
    )
    derive_corporate_actions(store, workspace="india")
    mark_calendar(store, START, END, set(SESSIONS))


def run(store, targets, tolerances=None):
    return run_crosscheck(store, targets=targets, window_start=START, window_end=END, as_of=AS_OF,
                          tolerances=tolerances or CrossCheckTolerances(), workspace="india")


def member_report(report, code):
    return next(m for m in report.members if m.stock_code == code)


def reasons_for(store, code):
    return sorted(
        (reason, day) for reason, day in store.query(
            "SELECT reason_code, date_from FROM quarantine_records WHERE check_name = 'crosscheck' AND stock_code = ?",
            [code])
    )


def test_split_member_with_raw_breeze_is_accepted_against_the_isin_of_each_date(store):
    load_bhavcopy(store)
    load_breeze(store, "CANBAN", breeze_rows("CANBAN", CANBK))
    report = run(store, make_targets(CANBK_MEMBER))
    result = member_report(report, "CANBAN")
    assert result.status == "checked" and result.accepted_count == 5 and result.quarantined_by_reason == {}
    assert result.rawness_overall == "raw_confirmed"
    assert report.rawness_counts == {"raw_confirmed": 1} and report.accepted_total == 5
    assert [(b.trade_date, b.isin) for b in accepted_bars(store, report.run_id)] == [
        (D1, OLD), (D2, OLD), (D3, NEW), (D4, NEW), (D5, NEW)]
    assert [c.code for c in report.caveats] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS"]  # every member raw_confirmed


def test_an_adjusted_breeze_series_is_quarantined_and_detected(store):
    load_bhavcopy(store)
    adjusted = {day: tuple(str(Decimal(p) * Decimal("0.2")) for p in prices) if day < D3 else prices
                for day, prices in CANBK.items()}
    load_breeze(store, "CANBAN", breeze_rows("CANBAN", adjusted))
    report = run(store, make_targets(CANBK_MEMBER))
    result = member_report(report, "CANBAN")
    assert result.rawness_overall == "adjusted_detected" and report.rawness_counts == {"adjusted_detected": 1}
    assert result.quarantined_by_reason["close_tolerance"] == 2  # D1 and D2 differ by about 80 percent
    assert result.quarantined_by_reason["breeze_series_adjusted"] == 1
    rawness = store.query("SELECT reason_code, scope, date_from, date_to FROM quarantine_records WHERE check_name = 'rawness'")
    assert rawness == [("breeze_series_adjusted", "raw", START, D3 - timedelta(days=1))]
    assert [b.trade_date for b in accepted_bars(store, report.run_id)] == [D3, D4, D5]
    assert "BREEZE_RAW_UNVERIFIED" in [c.code for c in report.caveats]


def test_conflicting_duplicates_are_quarantined_and_identical_overlaps_count_once(store):
    load_bhavcopy(store)
    first = {day: STEADY[day] for day in (D1, D2, D3)}
    second = {day: STEADY[day] for day in (D2, D3, D4)}
    conflict = {D3: ("100", "105", "95", "100.2")}  # differs from the first response on D3
    load_breeze(store, "STEADY", breeze_rows("STEADY", first), D1, D3, "relay/a")
    load_breeze(store, "STEADY", breeze_rows("STEADY", second, overrides=conflict), D2, D4, "relay/b")
    report = run(store, make_targets(STEADY_MEMBER))
    got = member_report(report, "STEADY")
    assert got.quarantined_by_reason["breeze_duplicate_conflict"] == 1
    assert [b.trade_date for b in accepted_bars(store, report.run_id)] == [D1, D2, D4]
    view = breeze_bars_for(store, "STEADY", start=START, end=END)
    assert [c.trade_date for c in view.conflicts] == [D3] and len(view.bars) == 3


def test_a_bhavcopy_row_in_another_series_is_a_series_mismatch(store):
    load_bhavcopy(store, canbk_series={D4: "BE"})
    load_breeze(store, "CANBAN", breeze_rows("CANBAN", CANBK))
    report = run(store, make_targets(CANBK_MEMBER))
    row = store.query("SELECT reason_code, detail_json FROM quarantine_records WHERE reason_code = 'series_mismatch'")
    assert len(row) == 1 and json.loads(row[0][1]) == {"bhavcopy_series": "BE"}
    assert D4 not in [b.trade_date for b in accepted_bars(store, report.run_id)]


def test_unfetched_members_are_reported_not_counted_as_one_sided(store):
    load_bhavcopy(store)
    load_breeze(store, "STEADY", breeze_rows("STEADY", STEADY))
    report = run(store, make_targets(STEADY_MEMBER, OTHER_MEMBER))
    other = member_report(report, "OTHERC")
    assert (other.status, other.accepted_count, other.rawness_overall, other.lineage_sha256) == (
        "not_fetched", 0, None, None)
    assert reasons_for(store, "OTHERC") == []
    assert member_report(report, "STEADY").status == "checked"


def test_tolerance_and_one_sided_and_non_session_reasons(store):
    load_bhavcopy(store)
    prices = {day: STEADY[day] for day in (D1, D2, D3, D5)}  # no Breeze bar on D4
    rows = breeze_rows("STEADY", prices, overrides={D2: ("100", "105", "95", "101")})  # close 1 percent off
    rows.append(kit.breeze_row("STEADY", f"{SAT.isoformat()} 00:00:00", "100", "105", "95", "100"))
    load_breeze(store, "STEADY", rows)
    report = run(store, make_targets(STEADY_MEMBER))
    assert set(reasons_for(store, "STEADY")) == {
        ("close_tolerance", D2), ("one_sided_bhavcopy", D4), ("breeze_bar_on_non_session", SAT)}
    assert [b.trade_date for b in accepted_bars(store, report.run_id)] == [D1, D3, D5]


def test_an_unknown_calendar_day_fails_closed(tmp_path):
    with PilotDataStore(tmp_path / "gap", workspace="india") as gap:
        for day in SESSIONS:
            ingest_udiff(gap, desc(), kit.udiff_zip(day, [ohlc_row("STEADYCO", "EQ", STEADY_ISIN, STEADY[day], day, "5")]),
                         trade_date=day)
        mark_calendar(gap, START, D2, {D1, D2})  # nothing recorded from D3 onward
        load_breeze(gap, "STEADY", breeze_rows("STEADY", STEADY))
        with pytest.raises(PilotDataError) as caught:
            run(gap, make_targets(STEADY_MEMBER))
        assert caught.value.code == "calendar_unknown_dates"


def test_rerun_is_deterministic_and_tolerances_change_the_run_id(store):
    load_bhavcopy(store)
    load_breeze(store, "CANBAN", breeze_rows("CANBAN", CANBK))
    load_breeze(store, "STEADY", breeze_rows("STEADY", STEADY, overrides={D2: ("100", "105", "95", "100.4")}))
    targets = make_targets(CANBK_MEMBER, STEADY_MEMBER)
    first = run(store, targets)
    quarantines = store.query("SELECT count(*) FROM quarantine_records")[0][0]
    accepted_rows = store.query("SELECT count(*) FROM crosscheck_accepted")[0][0]
    second = run(store, targets)
    assert second.run_id == first.run_id and second.report_sha256 == first.report_sha256
    assert store.query("SELECT count(*) FROM quarantine_records")[0][0] == quarantines
    assert store.query("SELECT count(*) FROM crosscheck_accepted")[0][0] == accepted_rows
    tight = run(store, targets, CrossCheckTolerances(close_max_rel=Decimal("0.001")))
    assert tight.run_id != first.run_id
    assert tight.totals_by_reason.get("close_tolerance", 0) == 1 and first.totals_by_reason == {}


def test_accepted_bars_exclude_dates_carrying_prior_raw_or_both_quarantines(store):
    load_bhavcopy(store)
    load_breeze(store, "STEADY", breeze_rows("STEADY", STEADY))
    base = dict(workspace="india", series="EQ", isin=STEADY_ISIN, nse_symbol="STEADYCO", stock_code="STEADY")
    store.record_quarantine([
        QuarantineRecord(check="append_conflict", reason_code="conflicting_reingest", scope="raw", date_from=D1,
                         date_to=D1, **base),
        QuarantineRecord(check="bhavcopy_consistency", reason_code="udiff_cm_mismatch", scope="raw", date_from=D2,
                         date_to=D2, **{**base, "stock_code": None}),
        QuarantineRecord(check="history_gap", reason_code="internal_gap", scope="both", date_from=D3, date_to=D3,
                         **base),
        QuarantineRecord(check="identity", reason_code="isin_unresolved", scope="both", date_from=D4, date_to=D4,
                         **{**base, "isin": None}),
        QuarantineRecord(check="corporate_action", reason_code="ca_unresolved_rights", scope="adjusted",
                         date_from=D5, date_to=D5, **base),  # adjusted scope never excludes raw acceptance
    ])
    report = run(store, make_targets(STEADY_MEMBER))
    assert [b.trade_date for b in accepted_bars(store, report.run_id)] == [D5]
    assert member_report(report, "STEADY").excluded_by_prior_quarantine == 4


def test_unverified_caveat_follows_rawness_and_cli_prints_caveats_first(tmp_path, capsys):
    root = tmp_path / "cli"
    with PilotDataStore(root, workspace="india") as store:
        load_bhavcopy(store)
        load_breeze(store, "STEADY", breeze_rows("STEADY", STEADY))
        ensure_target_tables(store)
        targets = make_targets(STEADY_MEMBER)
        store.append_rows(
            "target_universe_snapshots",
            [{"target_sha256": "t" * 64, "workspace": "india", "as_of": AS_OF,
              "built_at_utc": utc_naive(datetime.now(timezone.utc)),
              "payload_json": json.dumps(targets.model_dump(mode="json"), sort_keys=True),
              "source_sha256": "m" * 64, "row_sha256": "t" * 64}],
            check="target_universe",
        )
    code = main(["run", "--root", str(root), "--workspace", "india", "--as-of", AS_OF.isoformat(),
                 "--window-start", START.isoformat(), "--window-end", END.isoformat()])
    printed = json.loads(capsys.readouterr().out)
    assert code == 0 and list(printed)[0] == "caveats" and printed["summary"]["accepted_total"] == 5
    # no split inside the window: nothing proves rawness, so the unverified caveat stays
    assert "BREEZE_RAW_UNVERIFIED" in [c["code"] for c in printed["caveats"]]
    assert main(["run", "--root", str(tmp_path / "empty"), "--workspace", "india", "--as-of", "2025-03-07",
                 "--window-start", "2025-03-03", "--window-end", "2025-03-09"]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "target_universe_missing"

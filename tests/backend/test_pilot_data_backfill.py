"""Probe, relay master refresh and the paced, capped, resumable backfill, driven end to end through
Phase 61's real RelayClient over a fake growin-relay/1 server. Nothing here reaches a network."""

import dataclasses
import fcntl
import io
import json
import os
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone

import duckdb
import pytest
from brokers.breeze_gateway import RelayClient

from pilot_data import backfill as bf
from pilot_data.breeze_bars import breeze_bars_for
from pilot_data.core import PilotDataError
from pilot_data.targets import TargetMember

import pilot_data_testkit as kit
from test_pilot_data_relay_contract import (  # noqa: F401  (store is a fixture)
    CALENDAR_END,
    FakeRelay,
    TamperSource,
    calendar_template,
    default_bars_body,
    err_response,
    is_session,
    relay,
    sessions,
    store,
)

from brokers.breeze_gateway import (
    BudgetExhausted,
    NoSession,
    UpstreamError,
    UpstreamTimeout,
)

TODAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
WINDOW = (date(2023, 6, 1), date(2026, 10, 14))  # about 860 sessions: one answer, under the row cap
CODES = ("RELIND", "NIFBEE", "TATMOT", "CANBAN", "JAIBAL")


class Time:
    """A fake monotonic clock that only the sleeper advances; every sleep is recorded."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.at_calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock_time():
    return Time()


def kwargs(clock_time: Time, **extra):
    return {"clock": lambda: NOW, "monotonic": clock_time.monotonic, "sleeper": clock_time.sleep, **extra}


def refresh(store, relay, **extra):
    return bf.refresh_security_master(store, relay.client(), workspace="india", clock=lambda: NOW, **extra)


def probe(store, relay, clock_time, window=WINDOW, **extra):
    return bf.run_probe(
        store, relay.client(), window_start=window[0], window_end=window[1], today_ist=TODAY,
        workspace="india", **kwargs(clock_time), **extra,
    )


def members(codes=CODES) -> list[TargetMember]:
    return [
        TargetMember(
            kind="nifty500", anchor_isin=f"INE{index:06d}A01018", nse_symbol=code, stock_code=code, token=1000 + index,
            company_name=code,
        )
        for index, code in enumerate(codes)
    ]


def engine(store, relay, clock_time, source=None, **extra):
    return bf.BackfillEngine(
        store, source if source is not None else relay.client(), workspace="india", **kwargs(clock_time), **extra
    )


def ready(store, relay, clock_time, codes=CODES, **engine_extra):
    """Master fetched through the relay, a conclusive probe, a stored plan: the state before `run`."""
    refresh(store, relay)
    record = probe(store, relay, clock_time)
    assert record.conclusive and record.strategy == "two_windows", record.reasons
    eng = engine(store, relay, clock_time, **engine_extra)
    plan = eng.build_plan(members(codes), window_start=WINDOW[0], window_end=WINDOW[1])
    relay.bars_requests.clear()
    relay.requests.clear()
    clock_time.sleeps.clear()
    return eng, plan


def lock_held(eng) -> bool:
    """True when some open file description holds the engine's flock (the OS is the lock, not the file)."""
    fd = os.open(eng._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def count(store, sql, params=()):
    """First cell of a query; a table that was never created holds zero rows."""
    try:
        return store.query(sql, list(params))[0][0]
    except duckdb.CatalogException:
        assert sql.lstrip().upper().startswith("SELECT COUNT(*)"), sql
        return 0


# ================================================================ probe


def test_probe_makes_three_ledgered_calls_and_records_the_convention(store, relay, clock_time):
    record = probe(store, relay, clock_time)
    assert len(relay.bars_requests) == 3
    assert [r["stock_code"] for r in relay.bars_requests] == ["RELIND", "RELIND", "NIFBEE"]
    assert record.conclusive and record.strategy == "two_windows" and record.reasons == ()
    assert record.allowed_times == ("00:00:00",)
    assert record.convention().allowed_times == ("00:00:00",)
    assert count(store, "SELECT count(*) FROM relay_call_ledger WHERE endpoint = 'historical'") == 3
    assert count(store, "SELECT count(*) FROM relay_call_outcomes WHERE outcome = 'ok'") == 3
    assert count(store, "SELECT count(*) FROM relay_provenance") == 3
    assert count(store, "SELECT count(*) FROM breeze_probe_records WHERE conclusive") == 1


def test_probe_ingests_the_full_window_body_when_conclusive(store, relay, clock_time):
    probe(store, relay, clock_time)
    assert count(store, "SELECT count(*) FROM breeze_bars_raw WHERE stock_code = 'RELIND'") > 800
    assert count(store, "SELECT DISTINCT adjustment_basis FROM breeze_bars_raw") == "as_traded_claimed"


def test_probe_is_paced(store, relay, clock_time):
    probe(store, relay, clock_time)
    assert len(clock_time.sleeps) == 2 and all(abs(s - bf.PACE_SECONDS) < 1e-9 for s in clock_time.sleeps)


def test_a_capped_probe_answer_is_truncation_and_is_not_ingested(store, relay, clock_time):
    record = probe(store, relay, clock_time, window=(date(2021, 9, 28), date(2026, 10, 14)))
    assert record.truncation_detected and record.full_rows == 1000
    assert "truncation_detected" in record.reasons and record.strategy == "yearly"
    assert record.conclusive  # the convention is still learned; the strategy is what changes
    assert count(store, "SELECT count(*) FROM breeze_bars_raw") == 0


def test_a_depth_limited_probe_answer_picks_the_yearly_strategy(store, relay, clock_time):
    relay.depth_cutoff = date(2024, 6, 3)
    record = probe(store, relay, clock_time)
    assert record.depth_limited and "depth_limited" in record.reasons and record.strategy == "yearly"
    assert count(store, "SELECT count(*) FROM breeze_bars_raw") == 0


def test_a_bar_on_a_non_session_makes_the_probe_inconclusive(store, relay, clock_time):
    def script(index, params):
        rows = json.loads(default_bars_body(params))["Success"]
        if index == 0:
            rows.append({**rows[0], "datetime": "2026-10-03 00:00:00"})  # a Saturday
        return kit.breeze_v2_json(
            [kit.breeze_row(r["stock_code"], r["datetime"], str(r["open"]), str(r["high"]), str(r["low"]),
                            str(r["close"]), volume=r["volume"]) for r in rows]
        )

    relay.bars_script = script
    record = probe(store, relay, clock_time)
    assert not record.conclusive and "bar_on_non_session" in record.reasons
    assert bf.latest_conclusive_probe(store) is None
    with pytest.raises(PilotDataError) as info:
        record.convention()
    assert info.value.code == "probe_inconclusive"


def test_two_times_of_day_make_the_probe_inconclusive(store, relay, clock_time):
    def script(index, params):
        rows = json.loads(default_bars_body(params))["Success"]
        if index == 1:
            rows[0]["datetime"] = rows[0]["datetime"].replace("00:00:00", "09:15:00")
        return kit.breeze_v2_json(
            [kit.breeze_row(r["stock_code"], r["datetime"], str(r["open"]), str(r["high"]), str(r["low"]),
                            str(r["close"]), volume=r["volume"]) for r in rows]
        )

    relay.bars_script = script
    record = probe(store, relay, clock_time)
    assert not record.conclusive and "multiple_times_of_day" in record.reasons
    assert count(store, "SELECT count(*) FROM breeze_bars_raw") == 0


def test_missing_recent_sessions_make_the_probe_inconclusive(store, relay, clock_time):
    def script(index, params):
        rows = json.loads(default_bars_body(params))["Success"]
        if index == 1:
            rows = rows[: len(rows) // 2]
        return kit.breeze_v2_json(
            [kit.breeze_row(r["stock_code"], r["datetime"], str(r["open"]), str(r["high"]), str(r["low"]),
                            str(r["close"]), volume=r["volume"]) for r in rows]
        )

    relay.bars_script = script
    record = probe(store, relay, clock_time)
    assert not record.conclusive and "recent_sessions_missing" in record.reasons


def test_low_coverage_of_the_full_window_makes_the_probe_inconclusive(store, relay, clock_time):
    def script(index, params):
        rows = json.loads(default_bars_body(params))["Success"]
        if index == 0:
            rows = rows[::2]
        return kit.breeze_v2_json(
            [kit.breeze_row(r["stock_code"], r["datetime"], str(r["open"]), str(r["high"]), str(r["low"]),
                            str(r["close"]), volume=r["volume"]) for r in rows]
        )

    relay.bars_script = script
    record = probe(store, relay, clock_time)
    assert not record.conclusive and "coverage_below_99_percent" in record.reasons


def test_an_empty_probe_call_is_refused(store, relay, clock_time):
    relay.bars_script = lambda index, params: kit.breeze_v2_json([])
    with pytest.raises(PilotDataError) as info:
        probe(store, relay, clock_time)
    assert info.value.code == "breeze_empty_response"
    assert count(store, "SELECT count(*) FROM relay_call_outcomes WHERE outcome = 'empty'") == 1


def test_a_relay_error_during_the_probe_stops_it_with_the_classified_code(store, relay, clock_time):
    relay.bars_script = lambda index, params: err_response(503, "NO_SESSION")
    with pytest.raises(PilotDataError) as info:
        probe(store, relay, clock_time)
    assert info.value.code == "breeze_session_missing" and len(relay.bars_requests) == 1


def test_a_stored_probe_that_no_longer_matches_its_hash_is_refused(store, relay, clock_time):
    probe(store, relay, clock_time)
    assert bf.latest_conclusive_probe(store) is not None
    stored = json.loads(store.query("SELECT payload_json FROM breeze_probe_records")[0][0])
    stored["full_rows"] += 1
    store._con.execute("UPDATE breeze_probe_records SET payload_json = ?", [json.dumps(stored, sort_keys=True)])
    with pytest.raises(PilotDataError) as info:
        bf.latest_conclusive_probe(store)
    assert info.value.code == "probe_record_corrupt"


def test_probe_rejects_a_foreign_workspace_and_a_backwards_window(store, relay, clock_time):
    with pytest.raises(PilotDataError) as info:
        bf.run_probe(store, relay.client(), window_start=WINDOW[1], window_end=WINDOW[0], today_ist=TODAY,
                     workspace="india", **kwargs(clock_time))
    assert info.value.code == "window_invalid"
    with pytest.raises(PilotDataError) as info:
        bf.run_probe(store, relay.client(), window_start=WINDOW[0], window_end=WINDOW[1], today_ist=TODAY,
                     workspace="uk", **kwargs(clock_time))  # type: ignore[arg-type]
    assert info.value.code == "workspace_mismatch"


# ================================================================ security master


def test_refresh_ingests_a_relay_fetched_snapshot_dated_by_the_fetch_in_ist(store, relay, clock_time):
    outcome = refresh(store, relay)
    assert outcome.row_count == len(kit.MASTER_SAMPLE_ROWS)
    rows = store.query("SELECT snapshot_date, snapshot_date_basis FROM security_master_snapshots")
    assert rows == [(TODAY, "relay_fetch")]
    assert count(store, "SELECT count(*) FROM relay_provenance") == 1
    assert count(store, "SELECT count(*) FROM relay_call_ledger WHERE endpoint = 'security_master'") == 1


def test_refresh_rejects_a_tampered_payload_and_stores_nothing(store, relay, clock_time):
    source = TamperSource(
        relay.client(), master=lambda p: dataclasses.replace(p, body=p.body + b"X")
    )
    with pytest.raises(PilotDataError) as info:
        bf.refresh_security_master(store, source, workspace="india", clock=lambda: NOW)
    assert info.value.code == "relay_hash_mismatch"
    assert not store.table_exists("security_master_snapshots") or count(
        store, "SELECT count(*) FROM security_master_snapshots") == 0
    assert count(store, "SELECT outcome FROM relay_call_outcomes") == "rejected"


def test_refresh_does_not_count_against_the_daily_historical_cap(store, relay, clock_time):
    refresh(store, relay)
    ledger = bf._CallLedger(
        store, workspace="india", clock=lambda: NOW, monotonic=clock_time.monotonic, sleeper=clock_time.sleep,
        pace_seconds=1.2, daily_call_cap=3500,
    )
    assert ledger.used_today(TODAY) == 0


# ================================================================ plan


def test_two_windows_split_the_range_in_half_with_a_pad_on_both_sides():
    windows = bf.plan_windows(date(2023, 6, 1), date(2026, 10, 14), "two_windows")
    assert len(windows) == 2
    first_end = windows[0][1] - timedelta(days=bf.PAD_DAYS)
    second_start = windows[1][0] + timedelta(days=bf.PAD_DAYS)
    assert windows[0][0] == date(2023, 6, 1) - timedelta(days=3)
    assert windows[1][1] == date(2026, 10, 14) + timedelta(days=3)
    assert second_start == first_end + timedelta(days=1)
    assert all((e - s).days + 1 <= bf.MAX_WINDOW_CALENDAR_DAYS for s, e in windows)


def test_yearly_windows_cover_the_range_without_gaps():
    windows = bf.plan_windows(date(2021, 9, 28), date(2026, 10, 14), "yearly", pad_days=0)
    assert windows[0][0] == date(2021, 9, 28) and windows[-1][1] == date(2026, 10, 14)
    for (_, end), (start, _) in zip(windows, windows[1:]):
        assert start == end + timedelta(days=1)
    assert len(windows) == 6


def test_a_window_over_the_size_limit_is_refused():
    with pytest.raises(PilotDataError) as info:
        bf.plan_windows(date(2020, 1, 1), date(2026, 10, 14), "two_windows")
    assert info.value.code == "window_too_large"


@pytest.mark.parametrize("pad", [-1, True, 1.5])
def test_pad_days_must_be_a_non_negative_integer(pad):
    with pytest.raises(PilotDataError):
        bf.plan_windows(date(2023, 6, 1), date(2024, 6, 1), "yearly", pad_days=pad)


def test_plan_tamper_is_caught_by_its_hash(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time)
    body = plan.model_dump(mode="json")
    body["stock_codes"] = ["RELIND"]
    with pytest.raises(ValueError):
        bf.BackfillPlan.model_validate(body)
    assert bf.load_plan(store, plan.plan_sha256) == plan
    assert bf.load_plan(store) == plan


def test_building_a_plan_needs_a_conclusive_probe_and_targets(store, relay, clock_time):
    refresh(store, relay)
    eng = engine(store, relay, clock_time)
    with pytest.raises(PilotDataError) as info:
        eng.build_plan(members(), window_start=WINDOW[0], window_end=WINDOW[1])
    assert info.value.code == "probe_missing"
    probe(store, relay, clock_time)
    with pytest.raises(PilotDataError) as info:
        eng.build_plan([], window_start=WINDOW[0], window_end=WINDOW[1])
    assert info.value.code == "targets_empty"


def test_run_refuses_a_plan_that_was_not_stored(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time)
    other = bf.BackfillPlan.create(
        workspace="india", window_start=plan.window_start, window_end=plan.window_end, strategy=plan.strategy,
        pad_days=plan.pad_days, windows=plan.windows, stock_codes=("RELIND",), probe_sha256=plan.probe_sha256,
        targets_sha256=None,
    )
    with pytest.raises(PilotDataError) as info:
        eng.run(other, today_ist=TODAY)
    assert info.value.code == "plan_missing" and relay.requests == []


# ================================================================ preflight


def refused(eng, code, relay):
    with pytest.raises(PilotDataError) as info:
        eng.preflight(today_ist=TODAY)
    assert info.value.code == code
    assert relay.bars_requests == []
    assert not lock_held(eng), "the lock must be released when preflight fails"


def test_preflight_passes_when_everything_is_good_and_holds_the_lock(store, relay, clock_time):
    eng, _ = ready(store, relay, clock_time)
    eng.preflight(today_ist=TODAY)
    assert lock_held(eng)
    eng.release_lock()
    assert not lock_held(eng)


@pytest.mark.parametrize(
    "state,egress,code",
    [("none", True, "breeze_session_missing"), ("expired", True, "breeze_session_stale"),
     ("active", False, "egress_ip_unverified"), ("suspended_egress", False, "egress_ip_unverified")],
)
def test_preflight_refuses_an_unhealthy_session(store, relay, clock_time, state, egress, code):
    eng, _ = ready(store, relay, clock_time)
    relay.health_state, relay.egress_ok = state, egress
    refused(eng, code, relay)


def test_preflight_refuses_an_exhausted_or_misdated_budget(store, relay, clock_time):
    eng, _ = ready(store, relay, clock_time)
    relay.remaining = 0
    refused(eng, "relay_budget_day", relay)
    relay.remaining = 100
    relay.ist_date = "2026-10-20"
    refused(eng, "relay_clock_mismatch", relay)


def test_preflight_refuses_without_a_security_master_or_with_a_stale_one(store, relay, clock_time):
    eng = engine(store, relay, clock_time)
    refused(eng, "security_master_missing", relay)
    refresh(store, relay)
    stale = bf.BackfillEngine(store, relay.client(), workspace="india", clock=lambda: NOW + timedelta(days=1),
                              monotonic=clock_time.monotonic, sleeper=clock_time.sleep)
    relay.ist_date = "2026-10-22"
    with pytest.raises(PilotDataError) as info:
        stale.preflight(today_ist=TODAY + timedelta(days=1))
    assert info.value.code == "security_master_stale"


def test_preflight_refuses_a_master_that_was_not_fetched_through_the_relay(store, relay, clock_time):
    from pilot_data.core import SourceDescriptor
    from pilot_data.security_master import ingest_security_master

    body = kit.security_master_zip(kit.MASTER_SAMPLE_ROWS)
    descriptor = SourceDescriptor(
        source="local_file", kind="security_master_zip", locator="fixture:master", fetched_at=NOW
    )
    ingest_security_master(store, descriptor, body, snapshot_date=TODAY, snapshot_date_basis="test_fixture")
    refused(engine(store, relay, clock_time), "security_master_not_relay_fetched", relay)


def test_preflight_refuses_without_a_conclusive_probe(store, relay, clock_time):
    refresh(store, relay)
    refused(engine(store, relay, clock_time), "probe_missing", relay)


def test_a_stale_owner_record_blocks_a_run_until_break_lock_clears_it(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time)
    eng._lock_path.write_text("123 stale\n")  # a run that died without releasing: no flock holds it
    with pytest.raises(PilotDataError) as info:
        eng.run(plan, today_ist=TODAY)
    assert info.value.code == "backfill_lock_stale" and relay.bars_requests == [] and not lock_held(eng)
    report = eng.run(plan, today_ist=TODAY, break_lock=True)
    assert report.status == "complete" and not lock_held(eng)


def test_the_lock_is_released_after_a_stopped_run(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time)
    relay.bars_script = lambda index, params: err_response(503, "NO_SESSION")
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "stopped_fail_closed" and not lock_held(eng)


def test_engine_construction_enforces_pacing_and_cap_bounds(store, relay, clock_time):
    with pytest.raises(PilotDataError) as info:
        engine(store, relay, clock_time, pace_seconds=0.5)
    assert info.value.code == "pace_too_fast"
    for cap in (0, bf.DAILY_CALL_CAP + 1):
        with pytest.raises(PilotDataError) as info:
            engine(store, relay, clock_time, daily_call_cap=cap)
        assert info.value.code == "cap_invalid"
    with pytest.raises(PilotDataError):
        engine(store, relay, clock_time, max_consecutive_failures=0)


# ================================================================ run: happy path


def test_a_full_run_fetches_every_window_once_and_stores_raw_bars(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time)
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "complete" and report.stop_code is None
    assert report.windows_total == 10 == len(relay.bars_requests) and report.windows_remaining == 0
    assert report.counts == {"pending": 0, "done": 10, "empty_flagged": 0, "failed": 0, "stored_unverified": 0, "stored_with_gaps": 0}
    assert report.calls_this_run == 10 and report.calls_used_today == 13
    assert report.relay_remaining_today == 4000
    assert report.caveats and any("raw" in c.text.lower() or "adjust" in c.text.lower() for c in report.caveats)
    assert count(store, "SELECT count(DISTINCT stock_code) FROM breeze_bars_raw") == 5
    assert count(store, "SELECT count(*) FROM breeze_bars_raw WHERE adjustment_basis != 'as_traded_claimed'") == 0
    assert count(store, "SELECT count(*) FROM backfill_events WHERE status = 'done'") == 10


def test_overlap_pad_rows_are_counted_once(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("TATMOT",))
    eng.run(plan, today_ist=TODAY)
    distinct = count(store, "SELECT count(DISTINCT trade_date) FROM breeze_bars_raw WHERE stock_code = 'TATMOT'")
    by_row = count(store, "SELECT count(DISTINCT row_sha256) FROM breeze_bars_raw WHERE stock_code = 'TATMOT'")
    assert distinct == by_row == len(sessions(date(2023, 5, 29), date(2026, 10, 17)))
    # the overlap days sit in two responses, as identical rows, so they collapse by row hash
    assert count(store, "SELECT count(*) FROM breeze_bars_raw WHERE stock_code = 'TATMOT'") > distinct


def test_prices_are_stored_raw_whatever_the_ratio_between_days(store, relay, clock_time):
    """A 1:10 split shows up as a close ratio of 0.1 and must not be smoothed or adjusted."""
    def script(index, params):
        days = sessions(date.fromisoformat(params["from"]), date.fromisoformat(params["to"]))
        rows = [
            kit.breeze_row(params["stock_code"], f"{d.isoformat()} 00:00:00", "100.00" if d < date(2024, 5, 15) else "10.00",
                           "101.00" if d < date(2024, 5, 15) else "10.10", "99.00" if d < date(2024, 5, 15) else "9.90",
                           "100.00" if d < date(2024, 5, 15) else "10.00")
            for d in days
        ]
        return kit.breeze_v2_json(rows)

    eng, plan = ready(store, relay, clock_time, codes=("TATMOT",))
    relay.bars_script = script
    assert eng.run(plan, today_ist=TODAY).status == "complete"
    before = count(store, "SELECT close FROM breeze_bars_raw WHERE stock_code = 'TATMOT' AND trade_date = '2024-05-14' LIMIT 1")
    after = count(store, "SELECT close FROM breeze_bars_raw WHERE stock_code = 'TATMOT' AND trade_date = '2024-05-15' LIMIT 1")
    assert (str(before), str(after)) == ("100.0000", "10.0000")


def test_provenance_row_exists_for_every_stored_response(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    eng.run(plan, today_ist=TODAY)
    rows = store.query(
        "SELECT contract, route, upstream_api, upstream_interval, breeze_status, empty, request_id "
        "FROM relay_provenance WHERE route = '/v1/bars/daily' AND query_json LIKE '%2023-05-29%'"
    )
    assert rows and rows[0][:6] == ("growin-relay/1", "/v1/bars/daily", "breeze-historicalcharts-v2", "1day", 200, False)
    assert len(rows[0][6]) == 16
    sources = count(store, "SELECT count(*) FROM source_files WHERE kind = 'breeze_v2_daily'")
    prov = count(store, "SELECT count(*) FROM relay_provenance WHERE route = '/v1/bars/daily'")
    assert prov >= sources >= 2


# ================================================================ run: pacing, ledger, cap


def test_calls_are_spaced_by_at_least_the_pace(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time)
    stamps: list[float] = []
    relay.on_bars = lambda params: stamps.append(clock_time.now)
    eng.run(plan, today_ist=TODAY)
    assert len(stamps) == 10
    assert all(b - a >= bf.PACE_SECONDS - 1e-9 for a, b in zip(stamps, stamps[1:]))
    assert len(clock_time.sleeps) == 9


def test_the_ledger_row_is_written_before_the_call_goes_out(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    seen: list[tuple[int, int]] = []

    def on_bars(params):
        reserved = count(store, "SELECT count(*) FROM relay_call_ledger")
        finished = count(store, "SELECT count(*) FROM relay_call_outcomes")
        seen.append((reserved, finished))

    relay.on_bars = on_bars
    eng.run(plan, today_ist=TODAY)
    # the master refresh and the 3 probe calls left 4 reserved and 4 finished rows behind: each window
    # sees one more reservation than finished outcome while its call is in flight
    assert seen == [(5, 4), (6, 5)], seen


def test_a_failed_ledger_write_stops_the_run_before_any_call(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    real = store.append_rows

    def broken(table, rows, *, check):
        if table == "relay_call_ledger":
            raise PilotDataError("ledger_write_failed", "disk full")
        return real(table, rows, check=check)

    store.append_rows = broken  # type: ignore[method-assign]
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "stopped_fail_closed" and report.stop_code == "ledger_write_failed"
    assert relay.bars_requests == []


def test_the_daily_cap_counts_the_ledger_and_resumes_the_next_ist_day(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, daily_call_cap=8)  # the probe already used 3
    first = eng.run(plan, today_ist=TODAY)
    assert first.status == "daily_cap_reached" and first.stop_code == "daily_cap_reached"
    assert first.calls_this_run == 5 == len(relay.bars_requests) and first.calls_used_today == 8
    assert first.counts["done"] == 5 and first.windows_remaining == 5

    again = eng.run(plan, today_ist=TODAY)
    assert again.status == "daily_cap_reached" and again.calls_this_run == 0
    assert len(relay.bars_requests) == 5, "no call may go out on the same IST date after the cap"

    tomorrow = NOW + timedelta(days=1)
    relay.ist_date, relay.fetched_at = "2026-10-22", "2026-10-22T04:30:00.000Z"
    next_eng = bf.BackfillEngine(store, relay.client(), workspace="india", clock=lambda: tomorrow,
                                 monotonic=clock_time.monotonic, sleeper=clock_time.sleep, daily_call_cap=8)
    bf.refresh_security_master(store, relay.client(), workspace="india", clock=lambda: tomorrow)
    resumed = next_eng.run(plan, today_ist=TODAY + timedelta(days=1))
    assert resumed.status == "complete" and resumed.calls_this_run == 5 and len(relay.bars_requests) == 10
    assert resumed.counts["done"] == 10
    assert count(store, "SELECT count(*) FROM relay_call_ledger WHERE ist_date = '2026-10-22' "
                        "AND endpoint = 'historical'") == 5


def test_the_cap_can_not_be_raised_past_the_local_maximum(store, relay, clock_time):
    assert bf.DAILY_CALL_CAP < 4500


def test_a_clock_that_disagrees_with_the_run_date_stops_before_any_call(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    with pytest.raises(PilotDataError):
        eng.run(plan, today_ist=TODAY - timedelta(days=1))  # budget says today, run date says yesterday
    assert relay.bars_requests == []


# ================================================================ run: resume


def test_a_stopped_run_resumes_without_refetching_done_windows(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND", "NIFBEE"))
    relay.bars_script = lambda index, params: err_response(503, "NO_SESSION") if index == 2 else None
    first = eng.run(plan, today_ist=TODAY)
    assert first.status == "stopped_fail_closed" and first.stop_code == "breeze_session_missing"
    assert first.counts["done"] == 2 and first.windows_remaining == 2 and len(relay.bars_requests) == 3
    done_before = {(r["stock_code"], r["from"]) for r in relay.bars_requests[:2]}

    relay.bars_script = None
    relay.bars_requests.clear()
    second = eng.run(plan, today_ist=TODAY)
    assert second.status == "complete" and second.counts["done"] == 4
    assert len(relay.bars_requests) == 2
    assert not done_before & {(r["stock_code"], r["from"]) for r in relay.bars_requests}


def test_status_reports_progress_without_calling_the_relay_for_bars(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    report = eng.status(plan)
    assert report.status == "incomplete" and report.windows_remaining == 2 and report.counts["pending"] == 2
    eng.run(plan, today_ist=TODAY)
    assert eng.status(plan).status == "complete" and relay.bars_requests.__len__() == 2


# ================================================================ run: empty, retry, failure


def _empty_body(params):
    return kit.breeze_v2_json([])


def test_an_empty_answer_is_retried_once_then_quarantined_not_stored_as_zero(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    relay.bars_script = lambda index, params: _empty_body(params) if index in (0, 1) else None
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "complete_with_flags" and report.counts["empty_flagged"] == 1 and report.counts["done"] == 1
    assert report.error_codes == {"breeze_empty_response": 1}
    assert len(relay.bars_requests) == 3  # two for the empty window, one for the other
    rows = store.query("SELECT check_name, reason_code, stock_code FROM quarantine_records "
                       "WHERE check_name = 'breeze_backfill'")
    assert rows == [("breeze_backfill", "breeze_empty_response", "RELIND")]
    assert count(store, "SELECT count(*) FROM backfill_events WHERE status = 'empty_flagged'") == 1


def test_an_empty_answer_that_returns_data_on_retry_is_done(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    relay.bars_script = lambda index, params: _empty_body(params) if index == 0 else None
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "complete" and report.counts["done"] == 2 and len(relay.bars_requests) == 3
    assert count(store, "SELECT count(*) FROM quarantine_records WHERE check_name = 'breeze_backfill'") == 0


def test_a_no_data_error_is_treated_like_an_empty_answer(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    relay.bars_script = lambda index, params: (
        err_response(502, "UPSTREAM_ERROR", "no_data", retryable=True) if index in (0, 1) else None
    )
    report = eng.run(plan, today_ist=TODAY)
    assert report.counts["empty_flagged"] == 1 and report.error_codes == {"breeze_no_data": 1}
    quarantined = store.query("SELECT reason_code FROM quarantine_records WHERE check_name = 'breeze_backfill'")
    assert quarantined == [("breeze_no_data",)]


def test_a_timeout_is_retried_once_and_a_second_timeout_fails_the_window(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    timeout = err_response(504, "UPSTREAM_TIMEOUT", "upstream_timeout", retryable=True)
    relay.bars_script = lambda index, params: timeout if index == 0 else None
    ok = eng.run(plan, today_ist=TODAY)
    assert ok.status == "complete" and len(relay.bars_requests) == 3

    relay.bars_requests.clear()
    relay.bars_script = lambda index, params: timeout
    eng2, plan2 = ready_again(store, relay, clock_time)
    report = eng2.run(plan2, today_ist=TODAY)
    assert report.counts["failed"] == 2 and report.status in ("stopped_consecutive_failures", "completed_with_failed_windows")
    assert report.error_codes == {"upstream_timeout": 2}


def ready_again(store, relay, clock_time):
    eng = engine(store, relay, clock_time)
    plan = eng.build_plan(members(("NIFBEE",)), window_start=WINDOW[0], window_end=WINDOW[1])
    relay.bars_requests.clear()
    return eng, plan


def test_a_minute_budget_error_sleeps_the_advertised_time_then_retries_once(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    limited = err_response(429, "BUDGET_EXHAUSTED", "budget_minute", retryable=True, retry_after=7)
    relay.bars_script = lambda index, params: limited if index == 0 else None
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "complete" and len(relay.bars_requests) == 3
    assert 7.0 in clock_time.sleeps


def test_an_upstream_shape_error_fails_only_that_window(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    relay.bars_script = lambda index, params: err_response(502, "UPSTREAM_ERROR", "upstream_shape") if index == 0 else None
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "completed_with_failed_windows" and report.counts == {
        "pending": 0, "done": 1, "empty_flagged": 0, "failed": 1, "stored_unverified": 0, "stored_with_gaps": 0}
    assert report.windows_remaining == 1 and report.error_codes == {"upstream_upstream_shape": 1}
    assert len(relay.bars_requests) == 2  # a shape error is not retried


def test_a_failed_window_is_retried_on_the_next_run_with_a_higher_attempt(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    relay.bars_script = lambda index, params: err_response(502, "UPSTREAM_ERROR", "upstream_shape") if index == 0 else None
    eng.run(plan, today_ist=TODAY)
    relay.bars_script, relay.bars_requests = None, []
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "complete" and len(relay.bars_requests) == 1
    attempts = store.query("SELECT attempt, status FROM backfill_events WHERE status IN ('failed', 'done') "
                           "AND window_from = ? ORDER BY attempt", [plan.windows[0][0]])
    assert attempts == [(1, "failed"), (2, "done")]


def test_three_failed_windows_in_a_row_stop_the_run(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=CODES)
    relay.bars_script = lambda index, params: err_response(502, "UPSTREAM_ERROR", "upstream_shape")
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "stopped_consecutive_failures" and report.stop_code == "upstream_upstream_shape"
    assert len(relay.bars_requests) == 3 and report.counts["failed"] == 3 and report.counts["pending"] == 7


def test_a_success_resets_the_consecutive_failure_count(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=CODES)
    fail = err_response(502, "UPSTREAM_ERROR", "upstream_shape")
    relay.bars_script = lambda index, params: fail if index % 3 != 2 else None
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "completed_with_failed_windows"
    assert len(relay.bars_requests) == 10 and report.counts["done"] == 3 and report.counts["failed"] == 7


@pytest.mark.parametrize(
    "response,code",
    [
        (err_response(503, "NO_SESSION"), "breeze_session_missing"),
        (err_response(503, "SESSION_EXPIRED", "deadline"), "breeze_session_stale"),
        (err_response(503, "EGRESS_MISMATCH", "ip_mismatch"), "egress_ip_unverified"),
        (err_response(429, "BUDGET_EXHAUSTED", "budget_day", retryable=True, retry_after=3600), "relay_budget_day"),
        (err_response(401, "UNAUTHORIZED"), "relay_unauthorized"),
        (err_response(422, "VALIDATION"), "relay_validation"),
        (err_response(502, "UPSTREAM_ERROR", "invalid_session"), "upstream_invalid_session"),
        (err_response(502, "UPSTREAM_ERROR", "brand_new_reason"), "upstream_unknown"),
    ],
)
def test_stop_class_errors_end_the_run_at_once(store, relay, clock_time, response, code):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    relay.bars_script = lambda index, params: response
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "stopped_fail_closed" and report.stop_code == code
    assert len(relay.bars_requests) == 1 and report.counts["done"] == 0


def test_a_capped_window_is_never_stored_as_complete(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    days = sessions(date(2021, 9, 1), date(2026, 10, 14))[-1000:]
    capped = kit.breeze_v2_json(
        [kit.breeze_row("RELIND", f"{d.isoformat()} 00:00:00", "100.00", "101.00", "99.00", "100.50") for d in days]
    )
    relay.bars_script = lambda index, params: capped if index == 0 else None
    report = eng.run(plan, today_ist=TODAY)
    assert report.counts["failed"] == 1 and report.error_codes == {"breeze_capped_window": 1}
    assert report.windows_remaining == 1
    assert count(store, "SELECT count(*) FROM relay_call_outcomes WHERE outcome = 'capped'") == 1
    assert count(store, "SELECT count(*) FROM breeze_bars_raw WHERE trade_date < ?", [days[0]]) == 0
    assert count(store, "SELECT count(*) FROM breeze_bars_raw WHERE source_sha256 IN "
                        "(SELECT source_sha256 FROM relay_call_outcomes WHERE outcome = 'capped')") == 0


def test_a_failed_parse_fails_the_window_with_the_parser_code(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    bad = kit.breeze_v2_json([kit.breeze_row("RELIND", "2024-01-02 09:15:00", "1", "2", "0.5", "1.5")])
    relay.bars_script = lambda index, params: bad if index == 0 else None
    report = eng.run(plan, today_ist=TODAY)
    assert report.counts["failed"] == 1 and report.counts["done"] == 1
    assert report.error_codes == {"breeze_no_usable_bars": 1}
    assert count(store, "SELECT count(*) FROM quarantine_records WHERE check_name = 'breeze_parse'") >= 1


# ================================================================ run: provenance re-validation


def tampered_run(store, relay, clock_time, tamper):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    eng._source = TamperSource(relay.client(), bars=tamper)
    return eng, plan, eng.run(plan, today_ist=TODAY)


@pytest.mark.parametrize(
    "name,tamper,code",
    [
        ("body", lambda p: dataclasses.replace(p, body=p.body + b" "), "relay_hash_mismatch"),
        ("sha", lambda p: dataclasses.replace(p, sha256="1" * 64), "relay_hash_mismatch"),
        ("contract", lambda p: dataclasses.replace(p, contract="growin-relay/2"), "relay_contract_mismatch"),
        ("naive_time", lambda p: dataclasses.replace(p, fetched_at_utc=p.fetched_at_utc.replace(tzinfo=None)),
         "relay_response_invalid"),
        ("request_id", lambda p: dataclasses.replace(p, request_id="XYZ"), "relay_response_invalid"),
        ("echo", lambda p: dataclasses.replace(p, upstream_request={**p.upstream_request, "stock_code": "NIFBEE"}),
         "relay_response_invalid"),
        ("window_echo", lambda p: dataclasses.replace(
            p, upstream_request={**p.upstream_request, "from_date": "2019-01-01T00:00:00.000Z"}),
         "relay_response_invalid"),
        ("query", lambda p: dataclasses.replace(p, query={**p.query, "stock_code": "NIFBEE"}), "relay_response_invalid"),
        ("api", lambda p: dataclasses.replace(p, upstream_api="icici-security-master-zip"), "relay_response_invalid"),
        ("interval", lambda p: dataclasses.replace(p, upstream_interval="1minute"), "relay_response_invalid"),
    ],
    ids=lambda v: v if isinstance(v, str) and v.islower() and " " not in v and len(v) < 14 else None,
)
def test_a_payload_that_fails_revalidation_stops_the_run_and_stores_nothing(store, relay, clock_time, name, tamper, code):
    eng, plan, report = tampered_run(store, relay, clock_time, tamper)
    assert report.status == "stopped_fail_closed" and report.stop_code == code, name
    assert report.counts["done"] == 0
    assert count(store, "SELECT count(*) FROM relay_call_outcomes WHERE outcome = 'rejected'") == 1
    # only the probe's rows exist; the rejected window added no bars, no provenance and no done event
    probe_bars = count(store, "SELECT count(*) FROM breeze_bars_raw WHERE stock_code = 'RELIND'")
    assert probe_bars > 0 and count(store, "SELECT count(*) FROM backfill_events WHERE status = 'done'") == 0
    assert len(relay.bars_requests) == 1


def test_a_rejected_payload_adds_no_bars_to_the_store(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("TATMOT",))
    bars_before = count(store, "SELECT count(*) FROM breeze_bars_raw")
    files_before = count(store, "SELECT count(*) FROM source_files WHERE kind = 'breeze_v2_daily'")
    eng._source = TamperSource(relay.client(), bars=lambda p: dataclasses.replace(p, body=p.body + b" "))
    eng.run(plan, today_ist=TODAY)
    assert count(store, "SELECT count(*) FROM breeze_bars_raw") == bars_before
    assert count(store, "SELECT count(*) FROM source_files WHERE kind = 'breeze_v2_daily'") == files_before


# ================================================================ report


def test_report_carries_the_raw_prices_caveat_and_no_secrets(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    report = eng.run(plan, today_ist=TODAY)
    dumped = json.dumps(report.model_dump(mode="json"))
    assert report.caveats
    for word in ("token", "apisession", "password", "secret", "http"):
        assert word not in dumped.lower(), word
    assert report.workspace == "india"


def test_the_report_class_is_a_caveated_result():
    from pilot_data.core import CaveatedResult

    assert issubclass(bf.BackfillRunReport, CaveatedResult)


# ================================================================ CLI


def cli(argv, relay=None, clock_time=None, **extra):
    clock_time = clock_time or Time()
    out = io.StringIO()
    source = relay.client() if relay is not None else None
    with redirect_stdout(out):
        code = bf.main(argv, source=source, **kwargs(clock_time, **extra))
    return code, json.loads(out.getvalue())


@pytest.fixture
def root(tmp_path, calendar_template):
    import shutil

    target = tmp_path / "cli_pilot"
    shutil.copytree(calendar_template, target)
    return target


def test_cli_has_no_url_host_or_port_option():
    parser = bf._build_parser()
    options = {
        action_string
        for sub in parser._subparsers._group_actions[0].choices.values()
        for action in sub._actions
        for action_string in action.option_strings
    }
    assert options and not {o for o in options if any(w in o for w in ("url", "host", "port", "token", "key"))}


def test_cli_health_prints_the_relay_state(root, relay):
    code, payload = cli(["health", "--root", str(root), "--workspace", "india"], relay)
    assert code == 0 and payload["result"]["health"]["state"] == "active"
    assert payload["result"]["budget"]["remaining_today"] == 4000 and payload["caveats"]


def test_cli_full_flow_probe_plan_run_status(root, relay):
    base = ["--root", str(root), "--workspace", "india"]
    clock_time = Time()
    assert cli(["refresh-master", *base], relay, clock_time)[0] == 0
    code, probed = cli(["probe", *base, "--window-start", WINDOW[0].isoformat(), "--window-end", WINDOW[1].isoformat()],
                       relay, clock_time)
    assert code == 0 and probed["result"]["conclusive"] and probed["result"]["strategy"] == "two_windows"
    assert len(relay.bars_requests) == 3


def test_cli_run_with_a_stop_returns_fail_closed_and_budget_day_returns_resume_later(store, relay, clock_time, root):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    store.close()
    cli_root = store.root
    relay.bars_script = lambda index, params: err_response(503, "NO_SESSION")
    code, payload = cli(["run", "--root", str(cli_root), "--workspace", "india"], relay, clock_time)
    assert code == 2 and payload["result"]["status"] == "stopped_fail_closed"
    relay.bars_script = lambda index, params: err_response(429, "BUDGET_EXHAUSTED", "budget_day", retryable=True,
                                                          retry_after=3600)
    code, payload = cli(["run", "--root", str(cli_root), "--workspace", "india"], relay, clock_time)
    assert code == 3 and payload["result"]["stop_code"] == "relay_budget_day"
    relay.bars_script = None
    code, payload = cli(["run", "--root", str(cli_root), "--workspace", "india"], relay, clock_time)
    assert code == 0 and payload["result"]["status"] == "complete"
    code, payload = cli(["status", "--root", str(cli_root), "--workspace", "india"], None, clock_time)
    assert code == 0 and payload["result"]["windows_remaining"] == 0


def test_cli_preflight_failures_print_a_code_and_exit_fail_closed(root, relay):
    code, payload = cli(["run", "--root", str(root), "--workspace", "india"], relay)
    assert code == 2 and payload["error_code"] in ("plan_missing", "security_master_missing")


def test_cli_refuses_an_unknown_workspace():
    with pytest.raises(SystemExit):
        bf.main(["health", "--root", "x", "--workspace", "uk"])


def test_cli_without_a_source_opens_the_phase_61_client(monkeypatch, root, relay):
    opened = []

    class Opened:
        def __enter__(self):
            opened.append(True)
            return relay.client()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(bf, "open_relay_source", lambda: Opened())
    out = io.StringIO()
    with redirect_stdout(out):
        code = bf.main(["health", "--root", str(root), "--workspace", "india"], **kwargs(Time()))
    assert code == 0 and opened == [True]


def test_cli_relay_error_prints_a_classified_code_only(root, relay):
    relay.health_response = err_response(503, "NO_SESSION")
    code, payload = cli(["health", "--root", str(root), "--workspace", "india"], relay)
    assert code == 2 and payload["error_code"] == "breeze_session_missing"
    assert set(payload) == {"caveats", "error_code"}


# ================================================================ review round 2

from brokers.breeze_gateway import RelayUnavailable  # noqa: E402

from pilot_data.models import DailyBarConvention  # noqa: E402

MIDNIGHT_IST = datetime(2026, 10, 21, 18, 31, tzinfo=timezone.utc)  # 00:01 IST on 22 October


def bar_row(code, day, stamp="00:00:00"):
    return kit.breeze_row(code, f"{day.isoformat()} {stamp}", "100.00", "101.00", "99.00", "100.50")


def bars_body(params, *, skip=(), extra=()):
    """Every session in the window as one 00:00:00 bar, minus `skip`, plus any `extra` rows."""
    days = sessions(date.fromisoformat(params["from"]), date.fromisoformat(params["to"]))
    rows = [bar_row(params["stock_code"], day) for day in days if day not in skip]
    return kit.breeze_v2_json([*rows, *extra])


def newest_only(params):
    day = sessions(date.fromisoformat(params["from"]), date.fromisoformat(params["to"]))[-1]
    return kit.breeze_v2_json([bar_row(params["stock_code"], day)])


class FlakySource:
    """Wraps a real client; `script(call_index)` returns an exception to raise instead of the call."""

    def __init__(self, inner, script):
        self.inner, self.script, self.calls = inner, script, 0

    def health(self):
        return self.inner.health()

    def budget(self):
        return self.inner.budget()

    def security_master(self):
        return self.inner.security_master()

    def daily_bars(self, *args, **kwargs):
        index, self.calls = self.calls, self.calls + 1
        failure = self.script(index)
        if failure is not None:
            raise failure
        return self.inner.daily_bars(*args, **kwargs)


def flaky_run(store, relay, clock_time, script, codes=("RELIND",), **engine_extra):
    eng, plan = ready(store, relay, clock_time, codes=codes, **engine_extra)
    source = FlakySource(relay.client(), script)
    eng._source = source
    return eng, plan, source


# ---------------------------------------------------------------- 1. completeness evidence


def no_calendar(monkeypatch):
    """Every window looks like one the stored calendar does not cover."""

    def unknown(store, start, end):
        raise PilotDataError("calendar_unknown_dates", "the calendar does not cover these dates")

    monkeypatch.setattr(bf, "sessions_between", unknown)


def stored_bars(store, code, start, end):
    return count(store, "SELECT count(*) FROM breeze_bars_raw WHERE stock_code = ? AND trade_date >= ? "
                        "AND trade_date <= ?", [code, start, end])


def window_source_bars(store, code, start, day=None):
    """Bars stored from the answer a window's latest event points at (the probe's own bars are left out)."""
    sha = store.query("SELECT source_sha256 FROM backfill_events WHERE stock_code = ? AND window_from = ? "
                      "AND status <> 'pending' ORDER BY at_utc DESC, rowid DESC LIMIT 1", [code, start])[0][0]
    if day is None:
        return count(store, "SELECT count(*) FROM breeze_bars_raw WHERE source_sha256 = ?", [sha])
    return count(store, "SELECT count(*) FROM breeze_bars_raw WHERE source_sha256 = ? AND trade_date = ?", [sha, day])


def covered(store, code, start):
    return count(store, "SELECT count(*) FROM breeze_responses WHERE stock_code = ? AND requested_from = ?",
                 [code, start])


def covers(store, code, day):
    """What a backtest asks: does a coverage row say the store knows this day?"""
    return breeze_bars_for(store, code, start=day, end=day).covers(day)


def test_a_window_answered_with_only_the_newest_bar_is_stored_with_gaps_never_done(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    relay.bars_script = lambda index, params: newest_only(params) if index in (0, 1) else None
    report = eng.run(plan, today_ist=TODAY)
    assert report.counts == {"pending": 0, "done": 1, "empty_flagged": 0, "failed": 0, "stored_unverified": 0,
                             "stored_with_gaps": 1}
    assert report.error_codes == {"breeze_window_gaps": 1} and report.windows_remaining == 0
    assert report.status == "complete_with_flags" and bf._exit_for_report(report) == 4
    assert len(relay.bars_requests) == 3  # one retry for the gappy window, then the second window
    assert report.flagged_windows == [{
        "stock_code": "RELIND", "window_from": plan.windows[0][0].isoformat(),
        "window_to": plan.windows[0][1].isoformat(), "status": "stored_with_gaps", "error_code": "breeze_window_gaps"}]
    assert count(store, "SELECT count(*) FROM relay_call_outcomes WHERE outcome = 'incomplete'") == 1
    assert window_source_bars(store, "RELIND", plan.windows[0][0]) == 1  # the one bar that came back
    assert covered(store, "RELIND", plan.windows[0][0]) == 0  # a window with gaps claims no coverage
    first_day = sessions(*plan.windows[0])[0]
    assert not covers(store, "RELIND", first_day)  # a bar-less session is never read as covered
    assert count(store, "SELECT count(*) FROM quarantine_records WHERE reason_code = 'breeze_window_gaps'") == 1

    relay.bars_script, relay.bars_requests = None, []
    again = eng.run(plan, today_ist=TODAY)  # terminal: never retried automatically
    assert again.calls_this_run == 0 and relay.bars_requests == [] and again.status == "complete_with_flags"
    fixed = eng.run(plan, today_ist=TODAY, retry_flagged=True)
    assert len(relay.bars_requests) == 1 and fixed.status == "complete" and fixed.counts["done"] == 2
    assert covered(store, "RELIND", plan.windows[0][0]) == 1 and covers(store, "RELIND", first_day)  # claimed once whole


def test_a_missing_interior_session_is_a_recorded_gap_not_done_and_no_bar_is_invented(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    gap = date(2024, 3, 13)
    assert is_session(gap)
    relay.bars_script = lambda index, params: bars_body(params, skip=(gap,)) if index in (0, 1) else None
    report = eng.run(plan, today_ist=TODAY)
    assert report.counts["stored_with_gaps"] == 1 and report.error_codes == {"breeze_window_gaps": 1}
    detail = store.query("SELECT detail_json FROM quarantine_records WHERE reason_code = 'breeze_window_gaps'")
    assert "gap_ranges" in detail[0][0] and gap.isoformat() in detail[0][0]
    assert window_source_bars(store, "RELIND", plan.windows[0][0], gap) == 0  # no zero-volume stand-in either
    assert window_source_bars(store, "RELIND", plan.windows[0][0]) == len(sessions(*plan.windows[0])) - 1


def test_a_quarantined_date_is_a_gap_even_when_the_day_has_a_good_bar(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    day = date(2024, 3, 13)
    odd = bar_row("RELIND", day, stamp="09:15:00")  # a second, off-convention row for the same day
    relay.bars_script = lambda index, params: bars_body(params, extra=(odd,)) if index in (0, 1) else None
    report = eng.run(plan, today_ist=TODAY)
    assert report.counts["stored_with_gaps"] == 1 and report.error_codes == {"breeze_window_gaps": 1}
    assert count(store, "SELECT count(*) FROM quarantine_records WHERE reason_code = 'timestamp_unexpected'") >= 1
    detail = store.query("SELECT detail_json FROM quarantine_records WHERE reason_code = 'breeze_window_gaps'")
    assert "quarantined_dates" in detail[0][0]


CONVENTION = DailyBarConvention(allowed_times=("00:00:00",), source="test")


def parse_days(code, days):
    return bf.parse_breeze_v2_daily(
        kit.breeze_v2_json([bar_row(code, day) for day in days]), stock_code=code, convention=CONVENTION
    )


def test_where_the_calendar_does_not_reach_the_window_nothing_is_ever_complete(store, relay, clock_time):
    eng, _ = ready(store, relay, clock_time, codes=("RELIND",))
    window = (date(2021, 5, 3), date(2021, 6, 30))  # before the stored calendar: every day is unknown
    days = sessions(*window)
    # a full answer, an answer without its newest seven sessions, one with only the newest bar: none is proof
    for answer in (days, days[:-7], days[-1:]):
        assessed = eng._assess(parse_days("RELIND", answer), window, None, None)
        assert assessed.verdict == "unverified" and assessed.detail["reason"] == "calendar_unknown_dates"


def test_an_unverified_window_is_stored_but_never_done_and_claims_no_coverage(store, relay, clock_time, monkeypatch):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    no_calendar(monkeypatch)
    last_seven = lambda index, params: bars_body(  # noqa: E731
        params, skip=tuple(sessions(date.fromisoformat(params["to"]) - timedelta(days=12),
                                    date.fromisoformat(params["to"]))))
    relay.bars_script = last_seven
    report = eng.run(plan, today_ist=TODAY)
    assert report.counts["done"] == 0 and report.counts["stored_unverified"] == 2
    assert report.status == "complete_with_flags" and bf._exit_for_report(report) == 4
    assert report.error_codes == {"breeze_window_unverified": 2} and len(relay.bars_requests) == 2  # no retry
    assert window_source_bars(store, "RELIND", plan.windows[0][0]) > 100  # bars and provenance are kept
    assert covered(store, "RELIND", plan.windows[0][0]) == 0 and covered(store, "RELIND", plan.windows[1][0]) == 0
    assert count(store, "SELECT count(*) FROM relay_provenance") > 3
    relay.bars_requests.clear()
    assert eng.run(plan, today_ist=TODAY, retry_flagged=True).calls_this_run == 0  # verify's job, not a refetch
    assert relay.bars_requests == []


def test_verify_promotes_an_unverified_window_once_the_calendar_covers_it_without_a_breeze_call(
    store, relay, clock_time, monkeypatch
):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND", "NIFBEE"))
    gap = date(2024, 3, 13)
    with monkeypatch.context() as patch:
        no_calendar(patch)
        relay.bars_script = lambda index, params: (
            bars_body(params, skip=(gap,)) if params["stock_code"] == "NIFBEE" else None
        )
        report = eng.run(plan, today_ist=TODAY)
    assert report.counts["stored_unverified"] == 4 and report.counts["done"] == 0
    relay.bars_requests.clear()
    with monkeypatch.context() as patch:
        no_calendar(patch)
        assert eng.verify(plan, today_ist=TODAY).counts["stored_unverified"] == 4  # calendar still unknown

    verified = eng.verify(plan, today_ist=TODAY)
    assert relay.bars_requests == [] and verified.calls_this_run == 0
    assert verified.counts["done"] == 3 and verified.counts["stored_with_gaps"] == 1
    assert verified.counts["stored_unverified"] == 0
    assert verified.status == "complete_with_flags" and bf._exit_for_report(verified) == 4
    assert [row["stock_code"] for row in verified.flagged_windows] == ["NIFBEE"]
    assert covered(store, "RELIND", plan.windows[0][0]) == 1  # coverage is claimed only once verified complete
    assert covered(store, "NIFBEE", plan.windows[0][0]) == 0  # a window with a gap claims nothing
    assert covers(store, "RELIND", gap) and not covers(store, "NIFBEE", gap)
    assert eng.verify(plan, today_ist=TODAY).counts["done"] == 3  # idempotent


def test_verify_leaves_windows_the_calendar_still_can_not_establish(store, relay, clock_time, monkeypatch):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    no_calendar(monkeypatch)
    eng.run(plan, today_ist=TODAY)
    report = eng.verify(plan, today_ist=TODAY)
    assert report.counts["stored_unverified"] == 2 and report.counts["done"] == 0
    assert covered(store, "RELIND", plan.windows[0][0]) == 0 and bf._exit_for_report(report) == 4


def test_cli_verify_makes_no_relay_call_and_promotes_a_checked_window(store, relay, clock_time, monkeypatch):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    with monkeypatch.context() as patch:
        no_calendar(patch)
        assert eng.run(plan, today_ist=TODAY).counts["stored_unverified"] == 2
    store.close()
    relay.bars_requests.clear()
    relay.requests.clear()
    code, payload = cli(["verify", "--root", str(store.root), "--workspace", "india"], None, clock_time)
    assert "error_code" not in payload, payload
    assert code == 0 and payload["result"]["status"] == "complete" and payload["result"]["counts"]["done"] == 2
    assert relay.requests == [] and payload["result"]["calls_this_run"] == 0


# ---------------------------------------------------------------- 1b. legitimate gaps do not stall the backfill


def listed(row, day):
    return {**row, "DateOfListing": day}


def only_from(params, first, *, skip=()):
    """Bars for the window's sessions on or after `first`, minus `skip`."""
    return bars_body(params, skip=tuple(day for day in sessions(date.fromisoformat(params["from"]),
                                                              date.fromisoformat(params["to"]))
                                        if day < first or day in skip))


LISTING = date(2025, 6, 2)
SUSPENSION = tuple(sessions(date(2025, 9, 1), date(2025, 9, 19)))  # about three weeks, NIFBEE only


def scenario(store, relay, clock_time, listing_raw="2025-06-02"):
    relay.master_rows = [kit.MASTER_RELIND, kit.MASTER_NIFBEE, listed(kit.MASTER_JAIBAL, listing_raw)]
    eng, plan = ready(store, relay, clock_time, codes=("RELIND", "NIFBEE", "JAIBAL"))

    def script(index, params):
        code = params["stock_code"]
        if code == "JAIBAL":
            return only_from(params, LISTING)
        if code == "NIFBEE":
            return bars_body(params, skip=SUSPENSION)
        return None

    relay.bars_script = script
    return eng, plan


def window_states(store):
    return dict(((code, start), status) for code, start, status in store.query(
        "SELECT stock_code, window_from, status FROM backfill_events WHERE status <> 'pending' "
        "ORDER BY at_utc, rowid"))


def test_a_mid_range_listing_and_a_three_week_suspension_end_in_terminal_statuses_and_stop_costing_calls(
    store, relay, clock_time
):
    eng, plan = scenario(store, relay, clock_time)
    early, late = plan.windows
    report = eng.run(plan, today_ist=TODAY)
    states = window_states(store)
    assert states == {
        ("RELIND", early[0]): "done", ("RELIND", late[0]): "done",
        ("NIFBEE", early[0]): "done", ("NIFBEE", late[0]): "stored_with_gaps",
        ("JAIBAL", early[0]): "done",  # listed after this window ended: expected nothing, Breeze said nothing
        ("JAIBAL", late[0]): "done",   # listed inside it: sessions before the listing date are not expected
    }
    assert report.counts == {"pending": 0, "done": 5, "empty_flagged": 0, "failed": 0, "stored_unverified": 0,
                             "stored_with_gaps": 1}
    assert report.status == "complete_with_flags" and bf._exit_for_report(report) == 4
    assert [(row["stock_code"], row["status"]) for row in report.flagged_windows] == [("NIFBEE", "stored_with_gaps")]
    assert len(relay.bars_requests) == 2 + 1 + 2 + 1 + 1  # RELIND 2, NIFBEE 1 + 2 (one retry), JAIBAL 1 + 1
    # stored bar counts: nothing invented for the suspension, nothing stored for JAIBAL before it was listed
    assert window_source_bars(store, "NIFBEE", late[0]) == len(sessions(*late)) - len(SUSPENSION)
    assert not any(stored_bars(store, "NIFBEE", day, day) for day in SUSPENSION)
    assert window_source_bars(store, "JAIBAL", late[0]) == len(sessions(LISTING, late[1]))
    assert stored_bars(store, "JAIBAL", early[0], early[1]) == 0
    assert window_source_bars(store, "RELIND", late[0]) == len(sessions(*late))
    detail = store.query("SELECT detail_json FROM quarantine_records WHERE reason_code = 'breeze_window_gaps'")
    assert len(detail) == 1 and "2025-09-01..2025-09-19" in detail[0][0]
    # coverage rows: only the windows judged complete. NIFBEE's gap window has bars but claims nothing, and the
    # pre-listing JAIBAL window (done, nothing stored) claims nothing either.
    assert {(code, start): covered(store, code, start) for code, start in states} == {
        ("RELIND", early[0]): 1, ("RELIND", late[0]): 1, ("NIFBEE", early[0]): 1, ("NIFBEE", late[0]): 0,
        ("JAIBAL", early[0]): 0, ("JAIBAL", late[0]): 1,
    }
    assert covered(store, "JAIBAL", early[0]) == 0
    assert not covers(store, "NIFBEE", SUSPENSION[0]) and not covers(store, "NIFBEE", late[1])
    assert covers(store, "RELIND", SUSPENSION[0]) and covers(store, "JAIBAL", LISTING)

    relay.bars_requests.clear()
    second = eng.run(plan, today_ist=TODAY)  # nothing is retried on its own, not even the gap window
    assert relay.bars_requests == [] and second.calls_this_run == 0
    assert second.status == "complete_with_flags" and bf._exit_for_report(second) == 4
    assert window_states(store) == states


def test_retry_flagged_requeues_only_the_gap_window_and_finishes_once_the_data_is_whole(store, relay, clock_time):
    eng, plan = scenario(store, relay, clock_time)
    eng.run(plan, today_ist=TODAY)
    relay.bars_requests.clear()
    relay.bars_script = lambda index, params: only_from(params, LISTING) if params["stock_code"] == "JAIBAL" else None
    fixed = eng.run(plan, today_ist=TODAY, retry_flagged=True)
    assert [(r["stock_code"], r["from"]) for r in relay.bars_requests] == [("NIFBEE", plan.windows[1][0].isoformat())]
    assert fixed.status == "complete" and fixed.counts["done"] == 6 and bf._exit_for_report(fixed) == 0
    assert covered(store, "NIFBEE", plan.windows[1][0]) == 1  # the retry came back whole: coverage is written now


@pytest.mark.parametrize("raw", ["", "not a date"])
def test_a_listing_date_that_is_blank_or_unreadable_leaves_the_window_strict(store, relay, clock_time, raw):
    eng, plan = scenario(store, relay, clock_time, listing_raw=raw)
    report = eng.run(plan, today_ist=TODAY)
    # the sessions before the listing count as gaps, and the empty pre-listing window is flagged, not waved through
    assert {(r["stock_code"], r["status"]) for r in report.flagged_windows} == {
        ("NIFBEE", "stored_with_gaps"), ("JAIBAL", "stored_with_gaps"), ("JAIBAL", "empty_flagged")}
    assert bf._exit_for_report(report) == 4


def test_a_wrong_late_listing_date_does_not_hide_missing_sessions_before_the_first_bar(store, relay, clock_time):
    eng, _ = ready(store, relay, clock_time, codes=("RELIND",))
    window = (date(2024, 1, 2), date(2024, 3, 28))
    days = sessions(*window)
    parsed = parse_days("RELIND", days[10:20] + days[21:])  # first bar on the 11th session, one hole after it
    late_listing = days[40]  # the master claims it listed much later than the first bar: evidently wrong
    assessed = eng._assess(parsed, window, None, late_listing)
    assert assessed.verdict == "gaps" and assessed.gap_days == (*days[:10], days[20])
    assert assessed.detail["first_gap"] == days[0].isoformat()
    assert assessed.detail["gap_ranges"] == f"{days[0].isoformat()}..{days[9].isoformat()},{days[20].isoformat()}"
    # the same late date over a gap-free tail is still gaps: the leading sessions are not waved through
    leading = eng._assess(parse_days("RELIND", days[10:]), window, None, late_listing)
    assert leading.verdict == "gaps" and leading.gap_days == tuple(days[:10])
    assert leading.detail["gap_ranges"] == f"{days[0].isoformat()}..{days[9].isoformat()}"
    # a consistent listing date (on or before the first bar) is still trusted as the floor
    assert eng._assess(parse_days("RELIND", days[10:]), window, None, days[10]).verdict == "complete"


def test_a_wrong_late_listing_date_stores_the_window_with_gaps_and_no_coverage(store, relay, clock_time):
    relay.master_rows = [kit.MASTER_RELIND, listed(kit.MASTER_NIFBEE, "2099-01-01"), kit.MASTER_JAIBAL]
    eng, plan = ready(store, relay, clock_time, codes=("NIFBEE",))
    first = sessions(*plan.windows[0])
    relay.bars_script = lambda index, params: only_from(params, first[10])
    report = eng.run(plan, today_ist=TODAY)
    assert window_states(store)[("NIFBEE", plan.windows[0][0])] == "stored_with_gaps"
    assert covered(store, "NIFBEE", plan.windows[0][0]) == 0 and not covers(store, "NIFBEE", first[10])
    detail = store.query("SELECT detail_json FROM quarantine_records WHERE reason_code = 'breeze_window_gaps'")
    assert first[0].isoformat() in detail[0][0]
    assert bf._exit_for_report(report) == 4


def test_out_of_window_bars_with_a_late_listing_and_a_quarantined_row_are_never_complete(store, relay, clock_time):
    """The Grok repro: listing after the window, one bar after it, a quarantined row inside it."""
    eng, _ = ready(store, relay, clock_time, codes=("RELIND",))
    window = (date(2024, 1, 2), date(2024, 3, 28))
    odd = bar_row("RELIND", date(2024, 2, 1), stamp="09:15:00")  # off-convention: the parser quarantines it
    parsed = bf.parse_breeze_v2_daily(
        kit.breeze_v2_json([bar_row("RELIND", date(2024, 6, 3)), odd]), stock_code="RELIND", convention=CONVENTION
    )
    assert [bar.trade_date for bar in parsed.bars] == [date(2024, 6, 3)] and parsed.parse_quarantine_inputs
    assessed = eng._assess(parsed, window, None, date(2024, 6, 1))
    assert assessed.verdict == "gaps" and assessed.verdict != "complete"
    assert date(2024, 2, 1) in assessed.gap_days and assessed.detail["reason"] == "no_bars_in_window"


def test_a_window_with_no_good_bar_in_it_is_never_complete(store, relay, clock_time):
    eng, _ = ready(store, relay, clock_time, codes=("RELIND",))
    window = (date(2024, 1, 2), date(2024, 3, 28))
    out_of_window = parse_days("RELIND", [date(2024, 6, 3)])
    for listing in (None, date(2024, 1, 1), date(2024, 2, 1)):  # a listing inside or before the window
        assessed = eng._assess(out_of_window, window, None, listing)
        assert assessed.verdict == "gaps" and assessed.detail["reason"] == "no_bars_in_window", listing


def test_out_of_window_bars_for_a_stock_listed_after_the_window_are_before_listing_and_store_nothing(
    store, relay, clock_time
):
    relay.master_rows = [kit.MASTER_RELIND, kit.MASTER_NIFBEE, listed(kit.MASTER_JAIBAL, "2026-10-01")]
    eng, plan = ready(store, relay, clock_time, codes=("JAIBAL",))
    late_bar = sessions(date(2026, 10, 1), date(2026, 10, 14))[0]
    relay.bars_script = lambda index, params: kit.breeze_v2_json([bar_row("JAIBAL", late_bar)])
    eng.run(plan, today_ist=TODAY)
    states = window_states(store)
    assert states[("JAIBAL", plan.windows[0][0])] == "done"  # listed after this window: expected nothing
    assert covered(store, "JAIBAL", plan.windows[0][0]) == 0
    assert not any(count(store, "SELECT count(*) FROM breeze_bars_raw WHERE source_sha256 = ? AND trade_date < ?",
                         [sha, late_bar]) for (sha,) in store.query(
        "SELECT source_sha256 FROM backfill_events WHERE stock_code = 'JAIBAL' AND status = 'done'"))
    assert not covers(store, "JAIBAL", plan.windows[0][1])


def test_before_listing_is_done_but_claims_no_coverage_and_stores_no_bars(store, relay, clock_time):
    relay.master_rows = [kit.MASTER_RELIND, kit.MASTER_NIFBEE, listed(kit.MASTER_JAIBAL, "2026-10-01")]
    eng, plan = ready(store, relay, clock_time, codes=("JAIBAL",))
    relay.bars_script = lambda index, params: _empty_body(params) if index == 0 else None
    eng.run(plan, today_ist=TODAY)
    assert window_states(store)[("JAIBAL", plan.windows[0][0])] == "done"
    assert covered(store, "JAIBAL", plan.windows[0][0]) == 0
    assert window_source_bars(store, "JAIBAL", plan.windows[0][0]) == 0  # the probe's own bars are left out
    assert not covers(store, "JAIBAL", plan.windows[0][0])


def test_a_mid_window_listing_with_an_empty_answer_is_flagged_not_done(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("JAIBAL",))
    mid = sessions(*plan.windows[0])[40]
    eng._listing_dates = {"JAIBAL": mid}  # listed inside the window: an empty answer proves nothing
    relay.bars_script = lambda index, params: _empty_body(params) if index in (0, 1) else None
    report = eng.run(plan, today_ist=TODAY)
    assert window_states(store)[("JAIBAL", plan.windows[0][0])] == "empty_flagged"
    assert report.error_codes == {"breeze_empty_response": 1} and len(relay.bars_requests) == 3
    assert covered(store, "JAIBAL", plan.windows[0][0]) == 0


def test_listing_dates_parse_in_the_master_formats_and_nothing_else():
    assert bf._parse_listing_date("2025-06-02") == date(2025, 6, 2)
    assert bf._parse_listing_date("02-Jun-2025") == date(2025, 6, 2)
    assert bf._parse_listing_date("02/06/2025") == date(2025, 6, 2)
    assert bf._parse_listing_date("") is None and bf._parse_listing_date("soon") is None


def test_days_before_the_probed_depth_are_not_expected(store, relay, clock_time):
    eng, _ = ready(store, relay, clock_time, codes=("RELIND",))
    window = (date(2023, 5, 29), date(2024, 12, 31))
    floor = date(2024, 6, 3)
    parsed = parse_days("RELIND", sessions(floor, window[1]))
    assert eng._assess(parsed, window, None, None).verdict == "gaps"
    assert eng._assess(parsed, window, floor, None).verdict == "complete"


# ---------------------------------------------------------------- 2. flagged windows are not complete


def test_a_run_of_only_flagged_windows_is_complete_with_flags_not_complete_and_can_be_requeued(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    relay.bars_script = lambda index, params: _empty_body(params)
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "complete_with_flags" and report.counts["empty_flagged"] == 2
    assert report.windows_remaining == 0 and bf._exit_for_report(report) == 4
    assert eng.status(plan).status == "complete_with_flags"

    relay.bars_requests.clear()
    same = eng.run(plan, today_ist=TODAY)  # the plan hash is deterministic: flagged windows are not refetched
    assert same.status == "complete_with_flags" and same.calls_this_run == 0 and relay.bars_requests == []

    relay.bars_script = None
    fixed = eng.run(plan, today_ist=TODAY, retry_flagged=True)
    assert fixed.status == "complete" and fixed.counts["done"] == 2 and fixed.counts["empty_flagged"] == 0
    assert len(relay.bars_requests) == 2 and bf._exit_for_report(fixed) == 0
    attempts = store.query("SELECT attempt, status FROM backfill_events WHERE window_from = ? "
                           "AND status IN ('empty_flagged', 'done') ORDER BY attempt", [plan.windows[0][0]])
    assert attempts == [(1, "empty_flagged"), (2, "done")]


def test_retry_flagged_leaves_failed_windows_and_done_windows_alone_unless_they_are_due(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    eng.run(plan, today_ist=TODAY)
    relay.bars_requests.clear()
    assert eng.run(plan, today_ist=TODAY, retry_flagged=True).calls_this_run == 0  # nothing flagged, nothing done again
    assert relay.bars_requests == []


def test_cli_exit_4_for_flagged_windows_and_retry_flagged_clears_them(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    store.close()
    base = ["--root", str(store.root), "--workspace", "india"]
    relay.bars_script = lambda index, params: _empty_body(params)
    code, payload = cli(["run", *base], relay, clock_time)
    assert code == 4 and payload["result"]["status"] == "complete_with_flags"
    assert cli(["status", *base], None, clock_time)[0] == 4
    relay.bars_script = None
    assert cli(["run", *base], relay, clock_time)[0] == 4  # without the flag nothing is retried
    code, payload = cli(["run", *base, "--retry-flagged"], relay, clock_time)
    assert code == 0 and payload["result"]["status"] == "complete"
    assert cli(["status", *base], None, clock_time)[0] == 0


# ---------------------------------------------------------------- 3. the envelope comes first


def test_a_status_500_answer_with_an_empty_header_is_an_upstream_error_never_empty(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    body = kit.breeze_v2_json([], status=500, error="Something went wrong")
    relay.bars_script = lambda index, params: relay.bars_ok(params, body) if index == 0 else None
    assert relay.bars_ok({"stock_code": "RELIND", "from": "2024-01-01", "to": "2024-01-31"}, body).headers[
        "X-Growin-Empty"] == "true"
    report = eng.run(plan, today_ist=TODAY)
    assert report.counts == {"pending": 0, "done": 1, "empty_flagged": 0, "failed": 1, "stored_unverified": 0, "stored_with_gaps": 0}
    assert report.error_codes == {"breeze_upstream_error": 1} and len(relay.bars_requests) == 2  # not retried as empty
    assert count(store, "SELECT count(*) FROM quarantine_records WHERE check_name = 'breeze_backfill'") == 0
    assert count(store, "SELECT count(*) FROM relay_call_outcomes WHERE outcome = 'empty'") == 0
    assert count(store, "SELECT count(*) FROM relay_call_outcomes WHERE error_code = 'breeze_upstream_error'") == 1
    import hashlib

    assert count(store, "SELECT count(*) FROM source_files WHERE source_sha256 = ?",
                 [hashlib.sha256(body).hexdigest()]) == 0


def test_an_empty_header_that_disagrees_with_the_body_stops_the_run(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    relay.bars_script = lambda index, params: relay.bars_ok(params, bars_body(params), **{"X-Growin-Empty": "true"})
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "stopped_fail_closed" and report.stop_code == "relay_response_invalid"
    assert len(relay.bars_requests) == 1 and report.counts["done"] == 0


def test_a_status_500_probe_answer_is_refused_not_read_as_empty(store, relay, clock_time):
    refresh(store, relay)
    body = kit.breeze_v2_json([], status=500, error="Something went wrong")
    relay.bars_script = lambda index, params: relay.bars_ok(params, body)
    with pytest.raises(PilotDataError) as info:
        probe(store, relay, clock_time)
    assert info.value.code == "breeze_upstream_error"


# ---------------------------------------------------------------- 4. pace, then reserve, then dispatch


def rollover_clock(clock_time):
    state = {"now": NOW}

    def sleeper(seconds):
        clock_time.sleep(seconds)
        state["now"] = MIDNIGHT_IST  # the pacing sleep crosses IST midnight

    return {"clock": lambda: state["now"], "sleeper": sleeper}


def test_a_pacing_sleep_across_ist_midnight_sends_no_next_day_call_and_stops_resumable(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND", "NIFBEE"))
    crossing = rollover_clock(clock_time)
    late = bf.BackfillEngine(store, relay.client(), workspace="india", monotonic=clock_time.monotonic, **crossing)
    report = late.run(plan, today_ist=TODAY)
    assert report.status == "stopped_ist_rollover" and report.stop_code == "ist_date_rollover"
    assert len(relay.bars_requests) == 1 and report.calls_this_run == 1 and report.counts["done"] == 1
    assert count(store, "SELECT count(*) FROM relay_call_ledger WHERE ist_date = '2026-10-22'") == 0
    assert bf._exit_for_report(report) == 3 and not lock_held(late)


def test_no_call_is_dispatched_after_the_clock_crossed_ist_midnight(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND", "NIFBEE"))
    state = {"now": NOW}
    seen = []

    def sleeper(seconds):
        clock_time.sleep(seconds)
        state["now"] = MIDNIGHT_IST

    relay.on_bars = lambda params: seen.append(state["now"])  # the clock at the moment the relay got the call
    late = bf.BackfillEngine(store, relay.client(), workspace="india", clock=lambda: state["now"],
                             monotonic=clock_time.monotonic, sleeper=sleeper)
    report = late.run(plan, today_ist=TODAY)
    assert seen == [NOW], "a call reached the relay after IST midnight while charged to the old date"
    assert report.status == "stopped_ist_rollover"
    assert count(store, "SELECT count(*) FROM relay_call_ledger WHERE ist_date = ? AND endpoint = 'historical'",
                 [TODAY]) == 3 + 1  # the probe's three and the one call that went out before midnight


def test_cli_ist_rollover_exits_3_not_2(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    store.close()
    code, payload = cli(["run", "--root", str(store.root), "--workspace", "india"], relay, clock_time,
                        **rollover_clock(clock_time))
    assert code == 3 and payload["result"]["stop_code"] == "ist_date_rollover"


def test_a_clock_before_the_run_date_is_still_a_clock_mismatch_and_fail_closed(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    behind = bf.BackfillEngine(store, relay.client(), workspace="india", monotonic=clock_time.monotonic,
                               sleeper=clock_time.sleep, clock=lambda: NOW - timedelta(days=1))
    report = behind.run(plan, today_ist=TODAY)
    assert report.status == "stopped_fail_closed" and report.stop_code == "relay_clock_mismatch"
    assert relay.bars_requests == [] and bf._exit_for_report(report) == 2


def test_the_ledger_row_is_reserved_after_the_pacing_sleep_not_before_it(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    seen = []
    base_sleep = clock_time.sleep

    def sleeper(seconds):
        seen.append(count(store, "SELECT count(*) FROM relay_call_ledger"))
        base_sleep(seconds)

    late = bf.BackfillEngine(store, relay.client(), workspace="india", clock=lambda: NOW,
                             monotonic=clock_time.monotonic, sleeper=sleeper)
    late.run(plan, today_ist=TODAY)
    assert seen == [5], seen  # master + 3 probe calls + the first window; the second window's row comes after the sleep


# ---------------------------------------------------------------- 6. relay unavailable


@pytest.mark.parametrize("reason,code", [("closed", "relay_closed"), ("tunnel_restart_failed", "relay_tunnel_restart_failed"),
                                         ("tunnel_down", "relay_tunnel_down"), ("brand_new", "relay_unavailable_unknown")])
def test_closed_and_failed_restart_stop_at_once_without_retry_or_failed_windows(store, relay, clock_time, reason, code):
    eng, plan, source = flaky_run(store, relay, clock_time, lambda index: RelayUnavailable(reason=reason))
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "stopped_fail_closed" and report.stop_code == code and bf._exit_for_report(report) == 2
    assert source.calls == 1, "no retry"
    assert report.counts == {"pending": 2, "done": 0, "empty_flagged": 0, "failed": 0, "stored_unverified": 0, "stored_with_gaps": 0}
    assert clock_time.sleeps == []


def test_a_refused_before_send_call_is_recorded_not_sent_and_does_not_count_toward_the_cap(store, relay, clock_time):
    # the probe used 3 of 4; a refused call that counted would leave no room for the retry
    eng, plan, source = flaky_run(
        store, relay, clock_time, lambda index: RelayUnavailable(reason="tunnel_restart_backoff") if index == 0 else None,
        daily_call_cap=4,
    )
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "daily_cap_reached" and report.counts["done"] == 1  # the retry went out, then the cap
    assert clock_time.sleeps == [60.0, bf.PACE_SECONDS]
    assert count(store, "SELECT count(*) FROM relay_call_outcomes WHERE outcome = 'not_sent' "
                        "AND error_code = 'relay_tunnel_restart_backoff'") == 1
    assert count(store, "SELECT count(*) FROM relay_call_ledger WHERE endpoint = 'historical'") == 3 + 2
    assert report.calls_used_today == 4 and report.calls_this_run == 1


def test_a_call_the_relay_received_before_the_client_reported_closed_counts_toward_the_cap(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    client = relay.client()
    eng._source = client

    def retire_under_the_request(params):
        client.close()  # the pool is retired while the request is already on the wire

    relay.on_bars = retire_under_the_request
    relay.bars_script = lambda index, params: RuntimeError("client closed under the request")
    report = eng.run(plan, today_ist=TODAY)
    assert len(relay.bars_requests) == 1, "the relay did receive the call"
    assert report.status == "stopped_fail_closed" and report.stop_code == "relay_closed"
    assert count(store, "SELECT count(*) FROM relay_call_outcomes WHERE outcome = 'not_sent'") == 0
    assert count(store, "SELECT count(*) FROM relay_call_outcomes WHERE outcome = 'error' "
                        "AND error_code = 'relay_closed'") == 1
    assert report.calls_this_run == 1 and report.calls_used_today == 3 + 1


def test_the_backoff_wait_is_one_bounded_sleep_then_one_retry_then_a_stop(store, relay, clock_time):
    eng, plan, source = flaky_run(
        store, relay, clock_time, lambda index: RelayUnavailable(reason="tunnel_restart_backoff")
    )
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "stopped_fail_closed" and report.stop_code == "relay_tunnel_restart_backoff"
    assert source.calls == 2 and 60.0 in clock_time.sleeps and sum(clock_time.sleeps) <= 60.0 + 2 * bf.PACE_SECONDS
    assert report.counts["failed"] == 0 and report.calls_used_today == 3  # both refusals were not sent
    assert count(store, "SELECT count(*) FROM relay_call_outcomes WHERE outcome = 'not_sent'") == 2


def test_a_transport_blip_twice_stops_the_run_instead_of_failing_windows(store, relay, clock_time):
    eng, plan, source = flaky_run(
        store, relay, clock_time, lambda index: RelayUnavailable(reason="connect_error"), codes=CODES
    )
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "stopped_fail_closed" and report.stop_code == "relay_unavailable"
    assert source.calls == 2 and report.counts["failed"] == 0 and 2.0 in clock_time.sleeps
    assert report.calls_this_run == 2  # a connect error may have reached the relay, so both calls count


def test_a_single_blip_is_retried_after_a_pause_and_the_run_completes(store, relay, clock_time):
    eng, plan, source = flaky_run(
        store, relay, clock_time, lambda index: RelayUnavailable(reason="connect_error") if index == 0 else None
    )
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "complete" and source.calls == 3 and 2.0 in clock_time.sleeps


def test_a_stopped_transport_run_resumes_to_complete(store, relay, clock_time):
    eng, plan, source = flaky_run(store, relay, clock_time, lambda index: RelayUnavailable(reason="closed"))
    assert eng.run(plan, today_ist=TODAY).status == "stopped_fail_closed"
    eng._source = relay.client()
    assert eng.run(plan, today_ist=TODAY).status == "complete"


# ---------------------------------------------------------------- 7. the lock


def test_break_lock_never_displaces_a_live_owner_and_the_owner_keeps_its_record(store, relay, clock_time):
    owner, _ = ready(store, relay, clock_time, codes=("RELIND",))
    owner.preflight(today_ist=TODAY)
    record = owner._lock_path.read_bytes()
    assert record.strip()
    intruder = engine(store, relay, clock_time)
    for flag in (False, True):
        with pytest.raises(PilotDataError) as info:
            intruder.preflight(today_ist=TODAY, break_lock=flag)
        assert info.value.code == "backfill_locked"
    assert lock_held(owner) and owner._lock_path.read_bytes() == record
    owner.release_lock()
    assert not lock_held(owner)
    intruder.preflight(today_ist=TODAY)  # free once the owner is done
    assert lock_held(intruder)
    intruder.release_lock()


def test_a_second_run_during_a_run_is_refused_even_with_break_lock(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    other = engine(store, relay, clock_time)
    outcomes = []

    def while_running(params):
        if outcomes:  # one attempt, on the first call; a lock that fails open would otherwise recurse here
            return
        outcomes.append("tried")
        try:
            other.run(plan, today_ist=TODAY, break_lock=True)
        except PilotDataError as exc:
            outcomes.append(exc.code)

    relay.on_bars = while_running
    report = eng.run(plan, today_ist=TODAY)
    assert report.status == "complete" and outcomes == ["tried", "backfill_locked"]
    assert len(relay.bars_requests) == 2, "the intruder made no call"


def test_release_clears_only_its_own_owner_record(store, relay, clock_time):
    eng, _ = ready(store, relay, clock_time, codes=("RELIND",))
    eng.preflight(today_ist=TODAY)
    eng._lock_path.write_text("999 successor-token\n")  # someone else's record replaced ours
    eng.release_lock()
    assert eng._lock_path.read_text() == "999 successor-token\n"
    assert not lock_held(eng)


def test_the_lock_file_is_kept_so_a_late_opener_locks_the_same_file(store, relay, clock_time):
    eng, plan = ready(store, relay, clock_time, codes=("RELIND",))
    eng.run(plan, today_ist=TODAY)
    assert eng._lock_path.exists() and eng._lock_path.read_bytes() == b""


# ---------------------------------------------------------------- 8. windows end on a completed day


def test_no_window_ends_after_the_last_completed_trading_day(store, relay, clock_time):
    refresh(store, relay)
    probe(store, relay, clock_time)
    eng = engine(store, relay, clock_time)
    plan = eng.build_plan(members(("TATMOT",)), window_start=date(2025, 1, 1), window_end=date(2026, 10, 19))
    assert plan.windows[-1][1] == date(2026, 10, 20) and eng._last_completed_day() == date(2026, 10, 20)
    assert all(end <= date(2026, 10, 20) for _, end in plan.windows)
    relay.bars_requests.clear()
    assert eng.run(plan, today_ist=TODAY).status == "complete"
    assert max(r["to"] for r in relay.bars_requests) == "2026-10-20"
    assert count(store, "SELECT max(requested_to) FROM breeze_responses WHERE stock_code = 'TATMOT'") == date(2026, 10, 20)


def test_the_last_completed_day_skips_a_weekend_and_today(store, relay, clock_time):
    monday = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)
    eng = bf.BackfillEngine(store, relay.client(), workspace="india", clock=lambda: monday,
                            monotonic=clock_time.monotonic, sleeper=clock_time.sleep)
    assert eng._last_completed_day() == date(2026, 10, 16)  # Sunday is no session, Monday is today


def test_plan_windows_clamps_and_refuses_a_window_wholly_in_the_future():
    windows = bf.plan_windows(date(2026, 1, 1), date(2026, 10, 19), "two_windows", last_completed=date(2026, 10, 20))
    assert windows[-1][1] == date(2026, 10, 20)
    unclamped = bf.plan_windows(date(2026, 1, 1), date(2026, 10, 19), "two_windows")
    assert unclamped[-1][1] == date(2026, 10, 22)
    with pytest.raises(PilotDataError) as info:
        bf.plan_windows(date(2026, 10, 19), date(2026, 10, 25), "two_windows", last_completed=date(2026, 10, 10))
    assert info.value.code == "window_invalid"

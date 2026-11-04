"""Part 1 and Part 2 report builders and CLI on synthetic stores."""
# ruff: noqa: F811  (imported pytest fixtures are re-declared as test parameters)

import json
import shutil
import stat
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import duckdb

from pilot_data import backfill as bf
from pilot_data import report as report_mod
from pilot_data.bhavcopy import ingest_pr_zip
from pilot_data.core import PilotDataError, SourceDescriptor, standard_caveats
from pilot_data.corporate_actions import derive_corporate_actions
from pilot_data.crosscheck import run_crosscheck
from pilot_data.dataset import build_dataset_snapshot
from pilot_data.models import CrossCheckTolerances
from pilot_data.security_master import ingest_security_master
from pilot_data.report import (
    Part2Report, build_part2_report, latest_session, main, run_part1, write_part2_report, write_report,
)
from pilot_data.store import PilotDataStore
from pilot_data.targets import TargetUniverseResult, ensure_target_tables
from pilot_data.core import utc_naive

import pilot_data_testkit as kit
from test_pilot_data_universe import D, SESSIONS, World, make_isin, steady, surveil
from test_pilot_data_backfill import NOW, TODAY, Time, clock_time, kwargs, ready  # noqa: F401  (clock_time is a fixture)
from test_pilot_data_relay_contract import calendar_template, relay  # noqa: F401  (fixtures)
from test_pilot_data_crosscheck import (
    AS_OF,
    CANBK,
    CANBK_MEMBER,
    D3 as CC_D3,
    END as CC_END,
    START as CC_START,
    STEADY,
    STEADY_MEMBER,
    breeze_rows,
    load_bhavcopy,
    load_breeze,
    make_targets,
    run as cc_run,
)

FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
WINDOW_START = SESSIONS[0]
RESEARCH = SESSIONS[64]


def persist_targets(store, world):
    result = TargetUniverseResult(
        workspace="india", caveats=standard_caveats(), as_of=D, members=world.members(D),
        exclusions=(), etf_rejected=(), master_snapshot="m" * 64, nifty500_snapshot="n" * 64,
        target_sha256="t" * 64,
    )
    ensure_target_tables(store)
    store.append_rows(
        "target_universe_snapshots",
        [{"target_sha256": result.target_sha256, "workspace": "india", "as_of": D,
          "built_at_utc": utc_naive(datetime.now(timezone.utc)),
          "payload_json": json.dumps(result.model_dump(mode="json"), sort_keys=True),
          "source_sha256": "m" * 64, "row_sha256": result.target_sha256}],
        check="target_universe",
    )


def build_world(store, *, snapshots=True, skip_marks=()):
    specs = {
        "OKAY": steady(),
        "CAEXP": steady(),
        "ROT": lambda day: {"close": "100.00", "value": "100000000.00", "series": "EQ",
                            "isin": make_isin(800 if day <= SESSIONS[40] else 801), "token": "55"},
        "GAPPY": lambda day: None if SESSIONS[50] <= day <= SESSIONS[51] else
        {"close": "100.00", "value": "100000000.00", "series": "EQ"},
    }
    world = World(store, specs)
    world.isins["ROT"] = make_isin(801)
    world.load(SESSIONS, skip_marks=set(skip_marks))
    ingest_pr_zip(
        store, SourceDescriptor(source="nse_archive", kind="pr_zip", locator="https://nsearchives.nseindia.com/pr",
                                fetched_at=FETCHED),
        kit.pr_zip(SESSIONS[10], [kit.pd_index_row()],
                   [kit.bc_row("EQ", "CAEXP", "CA EXP", "RIGHTS 1:1 @ PRM RS 3/-", ex_date=SESSIONS[30])], []),
        trade_date=SESSIONS[10],
    )
    derive_corporate_actions(store, workspace="india")
    if snapshots:
        surveil(store, D)
    persist_targets(store, world)
    return world


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


def run(store):
    return run_part1(store, as_of=D, window_start=WINDOW_START, research_date=RESEARCH, workspace="india")


def test_report_counts_on_a_synthetic_store(store):
    world = build_world(store)
    report = run(store)
    assert [c.code for c in report.caveats][:2] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS"]
    assert report.sessions_by_year == {"2025": 75} and report.holidays == 0 and report.unknown_days == 0
    assert report.pr_missing_sessions == 75  # the fetch log records no PR download in this synthetic store
    assert report.files_by_kind == {"pr_zip": 1, "udiff": 75}
    assert report.targets_mapped == 4 and report.liquid_etfs == 0 and report.targets_excluded_by_reason == {}
    assert report.lineages_multi_segment == 0  # the unexplained ISIN change stops the walk, so one segment remains
    assert [(u["stock_code"], u["reason"]) for u in report.lineages_unresolved] == [("CROT", "isin_change_unexplained")]
    assert [(u["stock_code"], u["kind"], u["reason"]) for u in report.ca_unresolved] == [
        ("CCAEXP", "rights", "not_adjustable")]
    assert report.ca_events_by_kind == {"rights": 1}
    assert report.short_history_count >= 1 and report.gap_range_count >= 1
    assert report.quarantines_by_check_reason["history_gap/internal_gap"] == 1
    assert report.quarantines_by_check_reason["identity/isin_unresolved"] == 1
    assert report.quarantines_by_check_reason["corporate_action/ca_unresolved_rights"] == 1
    pilot, research = report.universe_pilot, report.universe_research
    assert pilot.surveillance_applied and pilot.error_code is None and pilot.eligible_count >= 1
    assert not research.surveillance_applied and research.as_of == RESEARCH  # before the first snapshot date
    assert report.smallcap_counts == {"unclassified": 4}
    assert world.isins["OKAY"]  # the world is built from the same members the report used


def test_rerun_gives_the_same_hash_and_no_new_quarantine_rows(store):
    build_world(store)
    first = run(store)
    count = store.query("SELECT count(*) FROM quarantine_records")[0][0]
    factor_sets = store.query("SELECT count(*) FROM adjustment_factor_sets")[0][0]
    second = run(store)
    assert second.report_sha256 == first.report_sha256 and len(first.report_sha256) == 64
    assert store.query("SELECT count(*) FROM quarantine_records")[0][0] == count
    assert store.query("SELECT count(*) FROM adjustment_factor_sets")[0][0] == factor_sets


def test_missing_pilot_surveillance_is_recorded_not_hidden(store):
    build_world(store, snapshots=False)
    report = run(store)
    pilot = report.universe_pilot
    assert (pilot.eligible_count, pilot.surveillance_applied, pilot.result_sha256) == (0, False, None)
    assert pilot.error_code == "surveillance_snapshot_missing"
    assert not report.universe_research.surveillance_applied  # no snapshot stored at all: research skips with the caveat


def test_an_unknown_day_in_the_window_fails_before_any_report(store):
    build_world(store, skip_marks={SESSIONS[20]})
    with pytest.raises(PilotDataError) as caught:
        run(store)
    assert caught.value.code == "calendar_unknown_dates"
    assert not store.table_exists("adjustment_factor_sets")  # nothing was computed or stored


def test_target_universe_is_required(store):
    with pytest.raises(PilotDataError) as caught:
        run(store)
    assert caught.value.code == "target_universe_missing"


def test_latest_session_and_the_part1_cli_write_a_read_only_report(tmp_path, capsys):
    root = tmp_path / "root"
    with PilotDataStore(root, workspace="india") as store:
        build_world(store)
        assert latest_session(store) == D
    common = ["--root", str(root), "--workspace", "india"]
    assert main(["latest-session", *common]) == 0
    assert capsys.readouterr().out.strip() == D.isoformat()
    assert main(["part1", *common, "--as-of", D.isoformat(), "--window-start", WINDOW_START.isoformat(),
                 "--research-date", RESEARCH.isoformat()]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert list(printed)[0] == "caveats" and printed["summary"]["unknown_days"] == 0
    path = root / "reports" / printed["summary"]["report_path"].rsplit("/", 1)[1]
    assert path.name == f"part1-{D.isoformat()}-{printed['summary']['report_sha256'][:12]}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    body = json.loads(path.read_text())
    assert list(body)[0] == "caveats" and [c["code"] for c in body["caveats"]][:2] == [
        "SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS"]
    assert main(["part1", *common, "--as-of", D.isoformat(), "--window-start", WINDOW_START.isoformat(),
                 "--research-date", RESEARCH.isoformat()]) == 0  # a re-run reuses the same file
    capsys.readouterr()
    assert len(list((root / "reports").glob("part1-*.json"))) == 1


def test_cli_reports_errors_with_exit_two(tmp_path, capsys):
    root = tmp_path / "empty"
    assert main(["latest-session", "--root", str(root), "--workspace", "india"]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "no_sessions"
    assert main(["part1", "--root", str(root), "--workspace", "india", "--as-of", "2025-06-27",
                 "--window-start", "2025-01-01", "--research-date", "2025-03-01"]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "target_universe_missing"


def test_write_report_is_idempotent(store, tmp_path):
    build_world(store)
    report = run(store)
    first = write_report(tmp_path / "out", report)
    assert write_report(tmp_path / "out", report) == first


# ================================================================ Part 2

CODES2 = ("RELIND", "NIFBEE")
NEXT_DAY = NOW + timedelta(days=1)


@pytest.fixture
def relay_store(calendar_template, tmp_path):
    """A store with the NSE calendar known to 2026-10-20, the shape every Part 2 stage runs against."""
    root = tmp_path / "relay_pilot"
    shutil.copytree(calendar_template, root)
    with PilotDataStore(root, workspace="india") as opened:
        yield opened


def empty_answer(index, params):
    """The first window's answer and its one retry come back empty: that window ends empty_flagged."""
    return kit.breeze_v2_json([]) if index in (0, 1) else None


def backfill_two_days(store, relay, clock_time):
    """Master, probe and plan on IST day 1. The cap of 7 stops the run after the probe (3 calls), the empty window
    (2 calls) and two good windows; day 2 sends the last window."""
    eng, plan = ready(store, relay, clock_time, codes=CODES2, daily_call_cap=7)
    relay.bars_script = empty_answer
    first = eng.run(plan, today_ist=TODAY)
    assert first.status == "daily_cap_reached" and first.counts["done"] == 2 and first.counts["empty_flagged"] == 1
    relay.bars_script = None
    return eng, plan


def finish_on_day_two(store, relay, clock_time, plan):
    relay.ist_date, relay.fetched_at = "2026-10-22", "2026-10-22T04:30:00.000Z"
    relay.master_rows = [row for row in relay.master_rows if row is not kit.MASTER_JAIBAL]  # one row leaves the master
    bf.refresh_security_master(store, relay.client(), workspace="india", clock=lambda: NEXT_DAY)
    nxt = bf.BackfillEngine(store, relay.client(), workspace="india", clock=lambda: NEXT_DAY,
                            monotonic=clock_time.monotonic, sleeper=clock_time.sleep, daily_call_cap=7)
    resumed = nxt.run(plan, today_ist=TODAY + timedelta(days=1))
    assert resumed.status == "complete_with_flags" and resumed.counts["empty_flagged"] == 1
    return nxt


def part2(store, run_id=None, dataset_dir=None, *, as_of=AS_OF, start=CC_START, end=CC_END):
    return build_part2_report(store, as_of=as_of, window_start=start, window_end=end, crosscheck_run_id=run_id,
                              dataset_dir=dataset_dir, workspace="india")


def crosscheck_steady_and_canbk(store):
    load_bhavcopy(store)
    load_breeze(store, "CANBAN", breeze_rows("CANBAN", CANBK))
    load_breeze(store, "STEADY", breeze_rows("STEADY", STEADY))
    return cc_run(store, make_targets(CANBK_MEMBER, STEADY_MEMBER))


def publish(store, check, export_root):
    manifest = build_dataset_snapshot(store, crosscheck_run_id=check.run_id, as_of=AS_OF, workspace="india",
                                      export_root=export_root)
    return export_root / manifest.dataset_sha256, manifest


def full_world(store, relay, clock_time, tmp_path):
    eng, plan = backfill_two_days(store, relay, clock_time)
    nxt = finish_on_day_two(store, relay, clock_time, plan)
    check = crosscheck_steady_and_canbk(store)
    dataset_dir, manifest = publish(store, check, tmp_path / "datasets")
    return {"engine": nxt, "plan": plan, "check": check, "dataset_dir": dataset_dir, "manifest": manifest}


def test_part2_fills_every_field_from_a_store_loaded_through_the_relay(relay_store, relay, clock_time, tmp_path):
    world = full_world(relay_store, relay, clock_time, tmp_path)
    report = part2(relay_store, dataset_dir=world["dataset_dir"])
    probe = report.probe
    assert (probe.strategy, probe.convention_times, probe.truncation_detected, probe.depth_limited) == (
        "two_windows", ("00:00:00",), False, False)
    assert probe.probe_sha256 == bf.latest_conclusive_probe(relay_store).probe_sha256()
    master = report.master_refresh
    assert master.snapshot_date == TODAY + timedelta(days=1) and master.changes_by_kind == {"row_removed": 1}
    back = report.backfill
    assert back.plan_sha256 == world["plan"].plan_sha256
    assert back.windows_by_status == {"pending": 0, "done": 3, "empty_flagged": 1, "failed": 0, "failed_exhausted": 0,
                                      "stored_unverified": 0, "stored_with_gaps": 0}
    assert back.calls_by_ist_date == {"2026-10-21": 7, "2026-10-22": 1}
    check = report.crosscheck
    assert check.run_id == world["check"].run_id and check.accepted_total == world["check"].accepted_total == 10
    assert check.rawness_counts == {"raw_confirmed": 1, "unverified": 1}
    assert (check.members_checked, check.members_not_fetched) == (2, 0)
    assert check.totals_by_reason == world["check"].totals_by_reason
    data = report.dataset
    assert data.dataset_sha256 == world["manifest"].dataset_sha256 and data.row_count == world["manifest"].row_count
    assert data.export_path == str(world["dataset_dir"].resolve()) and report.dataset_refusal_code is None
    assert [c.code for c in report.caveats][:3] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS", "BREEZE_RAW_UNVERIFIED"]
    assert (report.as_of, report.window_start, report.window_end) == (AS_OF, CC_START, CC_END)
    assert isinstance(report, Part2Report) and len(report.report_sha256) == 64


def test_max_calls_one_day_is_the_busiest_day_and_matches_the_engines_own_count(relay_store, relay, clock_time):
    eng, plan = backfill_two_days(relay_store, relay, clock_time)
    nxt = finish_on_day_two(relay_store, relay, clock_time, plan)
    back = part2(relay_store).backfill
    assert back.calls_by_ist_date == {"2026-10-21": 7, "2026-10-22": 1}
    assert back.max_calls_one_day == 7  # not the last day (1) and not the sum (8)
    # the report counts a day the way the daily cap does
    assert nxt._ledger.used_today(TODAY) == back.calls_by_ist_date["2026-10-21"]
    assert nxt._ledger.used_today(TODAY + timedelta(days=1)) == back.calls_by_ist_date["2026-10-22"]


def test_a_call_the_client_refused_before_sending_is_not_counted(relay_store, relay, clock_time):
    eng, plan = backfill_two_days(relay_store, relay, clock_time)
    before = part2(relay_store).backfill.calls_by_ist_date
    ledger = bf._CallLedger(relay_store, workspace="india", clock=lambda: NOW, monotonic=clock_time.monotonic,
                            sleeper=clock_time.sleep, pace_seconds=1.2, daily_call_cap=3500)
    refused = ledger.reserve(endpoint="historical", ist_date=TODAY, stock_code="RELIND")
    ledger.finish(refused, "not_sent", "relay_unavailable", None)
    sent = ledger.reserve(endpoint="historical", ist_date=TODAY, stock_code="RELIND")
    ledger.finish(sent, "error", "upstream_timeout", None)
    after = part2(relay_store).backfill
    assert after.calls_by_ist_date["2026-10-21"] == before["2026-10-21"] + 1  # only the sent call
    assert after.max_calls_one_day == after.calls_by_ist_date["2026-10-21"]


def test_only_a_relay_fetched_master_counts_as_the_refresh(relay_store, relay, clock_time):
    bf.refresh_security_master(relay_store, relay.client(), workspace="india", clock=lambda: NOW)
    fetched = part2(relay_store).master_refresh
    declared = [row for row in relay.master_rows if row is not kit.MASTER_JAIBAL]  # a later, hand-declared file
    ingest_security_master(
        relay_store,
        SourceDescriptor(source="local_file", kind="security_master", locator="operator/NSEScripMaster.txt",
                         fetched_at=datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)),
        kit.security_master_bytes(declared), snapshot_date=date(2026, 10, 22), snapshot_date_basis="operator_declared",
    )
    assert relay_store.query("SELECT count(*) FROM security_master_snapshots")[0][0] == 2
    assert part2(relay_store).master_refresh == fetched and fetched.changes_by_kind == {}


def test_an_unfinished_backfill_shows_its_pending_windows(relay_store, relay, clock_time):
    backfill_two_days(relay_store, relay, clock_time)
    back = part2(relay_store).backfill
    assert back.windows_by_status["done"] == 2 and back.windows_by_status["pending"] == 1
    assert back.windows_by_status["empty_flagged"] == 1
    assert back.calls_by_ist_date == {"2026-10-21": 7}


def test_a_store_with_no_stage_run_reports_every_section_as_absent(tmp_path):
    with PilotDataStore(tmp_path / "bare", workspace="india") as bare:
        tables_before = sorted(row[0] for row in bare.query("SELECT table_name FROM information_schema.tables"))
        report = part2(bare)
        tables_after = sorted(row[0] for row in bare.query("SELECT table_name FROM information_schema.tables"))
    assert (report.probe, report.master_refresh, report.backfill, report.crosscheck, report.dataset) == (
        None, None, None, None, None)
    assert report.dataset_refusal_code == "crosscheck_missing"
    assert [c.code for c in report.caveats] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS", "BREEZE_RAW_UNVERIFIED"]
    assert tables_before == tables_after, "a diagnostic report must not create tables"


def test_the_report_creates_no_table_in_a_store_that_has_only_some_stages(relay_store, relay, clock_time):
    bf.refresh_security_master(relay_store, relay.client(), workspace="india", clock=lambda: NOW)
    def names():
        return sorted(r[0] for r in relay_store.query("SELECT table_name FROM information_schema.tables"))

    before = names()
    report = part2(relay_store)
    assert report.master_refresh is not None and report.probe is None and report.backfill is None
    assert names() == before


def test_an_adjusted_run_refuses_publication_even_without_a_dataset_dir(relay_store):
    load_bhavcopy(relay_store)
    adjusted = {day: tuple(str(Decimal(p) * Decimal("0.2")) for p in prices) if day < CC_D3 else prices
                for day, prices in CANBK.items()}
    load_breeze(relay_store, "CANBAN", breeze_rows("CANBAN", adjusted))
    check = cc_run(relay_store, make_targets(CANBK_MEMBER))
    assert check.rawness_counts == {"adjusted_detected": 1}
    report = part2(relay_store, run_id=check.run_id)
    assert report.dataset is None and report.dataset_refusal_code == "breeze_adjusted_detected"
    assert report.crosscheck.rawness_counts == {"adjusted_detected": 1}
    assert "BREEZE_RAW_UNVERIFIED" in [c.code for c in report.caveats]
    # a dataset path never overrides the refusal: nothing was ever published for this run
    elsewhere = part2(relay_store, run_id=check.run_id, dataset_dir=relay_store.root / "datasets" / ("a" * 64))
    assert elsewhere.dataset is None and elsewhere.dataset_refusal_code == "breeze_adjusted_detected"


def test_raw_confirmed_everywhere_drops_the_unverified_caveat(relay_store, tmp_path):
    load_bhavcopy(relay_store)
    load_breeze(relay_store, "CANBAN", breeze_rows("CANBAN", CANBK))
    check = cc_run(relay_store, make_targets(CANBK_MEMBER))
    assert check.rawness_counts == {"raw_confirmed": 1}
    report = part2(relay_store)
    assert [c.code for c in report.caveats] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS"]
    assert report.dataset is None and report.dataset_refusal_code == "dataset_not_built"
    # one member that is not raw_confirmed brings the caveat back
    load_breeze(relay_store, "STEADY", breeze_rows("STEADY", STEADY))
    mixed = cc_run(relay_store, make_targets(CANBK_MEMBER, STEADY_MEMBER))
    assert "BREEZE_RAW_UNVERIFIED" in [c.code for c in part2(relay_store, run_id=mixed.run_id).caveats]


def test_exactly_one_of_dataset_and_refusal_is_set_in_every_state(relay_store, tmp_path):
    load_bhavcopy(relay_store)
    load_breeze(relay_store, "CANBAN", breeze_rows("CANBAN", CANBK))
    states = [part2(relay_store)]  # no cross-check yet
    check = cc_run(relay_store, make_targets(CANBK_MEMBER))
    states.append(part2(relay_store))  # run, no dataset dir
    dataset_dir, _ = publish(relay_store, check, tmp_path / "datasets")
    states.append(part2(relay_store, dataset_dir=dataset_dir))  # published
    states.append(part2(relay_store, dataset_dir=tmp_path / "datasets" / ("b" * 64)))  # directory gone
    assert [(s.dataset is not None, s.dataset_refusal_code) for s in states] == [
        (False, "crosscheck_missing"), (False, "dataset_not_built"), (True, None), (False, "dataset_integrity")]
    assert all((s.dataset is None) != (s.dataset_refusal_code is None) for s in states)


def test_a_tampered_dataset_is_reported_as_a_refusal_not_as_published(relay_store, tmp_path):
    load_bhavcopy(relay_store)
    load_breeze(relay_store, "CANBAN", breeze_rows("CANBAN", CANBK))
    check = cc_run(relay_store, make_targets(CANBK_MEMBER))
    dataset_dir, manifest = publish(relay_store, check, tmp_path / "datasets")
    copy = tmp_path / "copy" / manifest.dataset_sha256
    shutil.copytree(dataset_dir, copy)
    copy.chmod(0o755)
    parquet = copy / "rows.parquet"
    parquet.chmod(0o644)
    raw = bytearray(parquet.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    parquet.write_bytes(bytes(raw))
    report = part2(relay_store, dataset_dir=copy)
    assert report.dataset is None and report.dataset_refusal_code == "dataset_integrity"
    assert part2(relay_store, dataset_dir=dataset_dir).dataset.dataset_sha256 == manifest.dataset_sha256


def test_a_dataset_built_from_another_run_is_not_reported_for_this_one(relay_store, tmp_path):
    load_bhavcopy(relay_store)
    load_breeze(relay_store, "CANBAN", breeze_rows("CANBAN", CANBK))
    load_breeze(relay_store, "STEADY", breeze_rows("STEADY", STEADY))
    run_a = cc_run(relay_store, make_targets(CANBK_MEMBER))
    run_b = cc_run(relay_store, make_targets(CANBK_MEMBER, STEADY_MEMBER))
    assert run_a.run_id != run_b.run_id
    dir_a, _ = publish(relay_store, run_a, tmp_path / "datasets")
    mismatch = part2(relay_store, run_id=run_b.run_id, dataset_dir=dir_a)
    assert mismatch.dataset is None and mismatch.dataset_refusal_code == "dataset_run_mismatch"
    assert part2(relay_store, run_id=run_a.run_id, dataset_dir=dir_a).dataset is not None


def test_the_newest_universe_run_for_this_window_is_used_and_other_runs_are_skipped(relay_store):
    load_bhavcopy(relay_store)
    load_breeze(relay_store, "CANBAN", breeze_rows("CANBAN", CANBK))
    load_breeze(relay_store, "STEADY", breeze_rows("STEADY", STEADY))
    first = cc_run(relay_store, make_targets(CANBK_MEMBER))
    second = cc_run(relay_store, make_targets(CANBK_MEMBER, STEADY_MEMBER))
    assert part2(relay_store).crosscheck.run_id == second.run_id
    # a later single-member run (no `members` key) is not a universe run
    relay_store.append_rows(
        "crosscheck_runs",
        [{"run_id": "9" * 64, "workspace": "india", "started_at_utc": datetime(2099, 1, 1), "params_json": "{}",
          "result_json": json.dumps({"window": "x"}), "result_sha256": "8" * 64, "source_sha256": "9" * 64,
          "row_sha256": "8" * 64}],
        check="crosscheck_run",
    )
    assert part2(relay_store).crosscheck.run_id == second.run_id
    with pytest.raises(PilotDataError) as unsupported:
        part2(relay_store, run_id="9" * 64)
    assert unsupported.value.code == "crosscheck_run_unsupported"
    # another as-of or window is never picked up silently
    other_window = part2(relay_store, end=CC_END + timedelta(days=1))
    assert other_window.crosscheck is None and other_window.dataset_refusal_code == "crosscheck_missing"
    with pytest.raises(PilotDataError) as mismatch:
        part2(relay_store, run_id=first.run_id, end=CC_END + timedelta(days=1))
    assert mismatch.value.code == "crosscheck_window_mismatch"
    with pytest.raises(PilotDataError) as missing:
        part2(relay_store, run_id="f" * 64)
    assert missing.value.code == "crosscheck_run_missing"


def test_a_cross_check_record_that_no_longer_matches_its_hash_is_refused(relay_store):
    load_bhavcopy(relay_store)
    load_breeze(relay_store, "CANBAN", breeze_rows("CANBAN", CANBK))
    check = cc_run(relay_store, make_targets(CANBK_MEMBER))
    body = relay_store.query("SELECT result_json FROM crosscheck_runs WHERE run_id = ?", [check.run_id])[0][0]
    relay_store.append_rows(
        "crosscheck_runs",
        [{"run_id": "7" * 64, "workspace": "india", "started_at_utc": datetime(2099, 1, 1), "params_json": "{}",
          "result_json": body, "result_sha256": "0" * 64, "source_sha256": "7" * 64, "row_sha256": "0" * 64}],
        check="crosscheck_run",
    )
    with pytest.raises(PilotDataError) as caught:
        part2(relay_store)
    assert caught.value.code == "crosscheck_record_corrupt"


def test_the_hash_is_stable_and_follows_the_content(relay_store, relay, clock_time, tmp_path):
    world = full_world(relay_store, relay, clock_time, tmp_path)
    first = part2(relay_store, dataset_dir=world["dataset_dir"])
    second = part2(relay_store, dataset_dir=world["dataset_dir"])
    assert first.report_sha256 == second.report_sha256 and first == second
    refused = part2(relay_store)  # same store, no dataset dir
    assert refused.report_sha256 != first.report_sha256 and refused.dataset_refusal_code == "dataset_not_built"


def test_argument_errors_fail_closed(relay_store):
    with pytest.raises(PilotDataError) as backwards:
        part2(relay_store, start=CC_END, end=CC_START)
    assert backwards.value.code == "window_invalid"
    with pytest.raises(PilotDataError) as foreign:
        build_part2_report(relay_store, as_of=AS_OF, window_start=CC_START, window_end=CC_END,
                           crosscheck_run_id=None, dataset_dir=None, workspace="uk")
    assert foreign.value.code == "workspace_mismatch"


def test_part2_report_is_a_caveated_result_and_rejects_dropped_caveats():
    assert issubclass(Part2Report, report_mod.CaveatedResult)
    with pytest.raises(ValueError):
        Part2Report(workspace="india", caveats=(), as_of=AS_OF, window_start=CC_START, window_end=CC_END, probe=None,
                    master_refresh=None, backfill=None, crosscheck=None, dataset=None,
                    dataset_refusal_code="crosscheck_missing", report_sha256="0" * 64)


def test_the_part2_cli_writes_a_read_only_report_and_leaves_the_store_alone(tmp_path, capsys, monkeypatch):
    root = tmp_path / "root"
    with PilotDataStore(root, workspace="india") as store:
        load_bhavcopy(store)
        load_breeze(store, "CANBAN", breeze_rows("CANBAN", CANBK))
        check = cc_run(store, make_targets(CANBK_MEMBER))
        dataset_dir, manifest = publish(store, check, root / "datasets")
        tables_before = sorted(r[0] for r in store.query("SELECT table_name FROM information_schema.tables"))
    opened = []
    real = report_mod.PilotDataStore

    def spy(*args, **kwargs_):
        opened.append(kwargs_.get("read_only"))
        return real(*args, **kwargs_)

    monkeypatch.setattr(report_mod, "PilotDataStore", spy)
    common = ["part2", "--root", str(root), "--workspace", "india", "--as-of", AS_OF.isoformat(),
              "--window-start", CC_START.isoformat(), "--window-end", CC_END.isoformat()]
    assert main([*common, "--run-id", check.run_id, "--dataset-dir", str(dataset_dir)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert opened == [True], "part2 must open the store read-only"
    assert list(printed)[0] == "caveats" and printed["summary"]["dataset_sha256"] == manifest.dataset_sha256
    assert printed["summary"]["dataset_refusal_code"] is None and printed["summary"]["crosscheck_run_id"] == check.run_id
    path = root / "reports" / printed["summary"]["report_path"].rsplit("/", 1)[1]
    assert path.name == f"part2-{AS_OF.isoformat()}-{printed['summary']['report_sha256'][:12]}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    body = json.loads(path.read_text())
    assert list(body)[0] == "caveats" and [c["code"] for c in body["caveats"]][:2] == [
        "SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS"]
    assert body["dataset"]["dataset_sha256"] == manifest.dataset_sha256 and body["dataset_refusal_code"] is None
    assert main([*common, "--run-id", check.run_id, "--dataset-dir", str(dataset_dir)]) == 0  # same file reused
    capsys.readouterr()
    assert len(list((root / "reports").glob("part2-*.json"))) == 1
    with PilotDataStore(root, workspace="india", read_only=True) as store:
        assert sorted(r[0] for r in store.query("SELECT table_name FROM information_schema.tables")) == tables_before


def test_the_part2_cli_records_a_refusal_with_exit_zero_and_reports_errors_with_exit_two(tmp_path, capsys):
    root = tmp_path / "root"
    with PilotDataStore(root, workspace="india") as store:
        load_bhavcopy(store)
        adjusted = {day: tuple(str(Decimal(p) * Decimal("0.2")) for p in prices) if day < CC_D3 else prices
                    for day, prices in CANBK.items()}
        load_breeze(store, "CANBAN", breeze_rows("CANBAN", adjusted))
        check = cc_run(store, make_targets(CANBK_MEMBER))
    common = ["--workspace", "india", "--as-of", AS_OF.isoformat(), "--window-start", CC_START.isoformat(),
              "--window-end", CC_END.isoformat()]
    assert main(["part2", "--root", str(root), *common, "--run-id", check.run_id]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["summary"]["dataset_refusal_code"] == "breeze_adjusted_detected"
    assert printed["summary"]["dataset_sha256"] is None
    assert main(["part2", "--root", str(root), *common, "--run-id", "f" * 64]) == 2
    failed = json.loads(capsys.readouterr().out)
    assert failed["error_code"] == "crosscheck_run_missing" and list(failed)[0] == "caveats"
    assert main(["part2", "--root", str(tmp_path / "nowhere"), *common]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "store_missing"
    assert not (tmp_path / "nowhere").exists()
    assert len(list((root / "reports").glob("part2-*.json"))) == 1  # the refusal was recorded; the errors wrote nothing


def test_write_part2_report_is_idempotent(relay_store, tmp_path):
    report = part2(relay_store)
    first = write_part2_report(tmp_path / "out", report)
    assert write_part2_report(tmp_path / "out", report) == first and first.name.startswith("part2-")


def rewrite_manifest(dataset_dir, copy_root, **edits):
    """A copy of a published dataset whose manifest.json carries other values (rows.parquet untouched)."""
    copy = copy_root / dataset_dir.name
    shutil.copytree(dataset_dir, copy)
    copy.chmod(0o755)
    manifest = copy / "manifest.json"
    manifest.chmod(0o644)
    body = json.loads(manifest.read_text())
    body.update(edits)
    manifest.write_text(json.dumps(body, sort_keys=True, indent=2))
    return copy


def test_a_valid_export_built_for_another_as_of_is_refused(relay_store, tmp_path):
    load_bhavcopy(relay_store)
    load_breeze(relay_store, "STEADY", breeze_rows("STEADY", STEADY))
    check = cc_run(relay_store, make_targets(STEADY_MEMBER))
    later = AS_OF + timedelta(days=1)
    other_run = run_crosscheck(relay_store, targets=make_targets(STEADY_MEMBER), window_start=CC_START,
                               window_end=CC_END, as_of=later, tolerances=CrossCheckTolerances(), workspace="india")
    assert other_run.run_id != check.run_id
    built = build_dataset_snapshot(relay_store, crosscheck_run_id=other_run.run_id, as_of=later, workspace="india",
                                   export_root=tmp_path / "datasets")
    assert built.as_of == later
    refused = part2(relay_store, dataset_dir=tmp_path / "datasets" / built.dataset_sha256)  # verifies, but not ours
    assert refused.dataset is None and refused.dataset_refusal_code == "dataset_run_mismatch"
    fitting, _ = publish(relay_store, check, tmp_path / "datasets")
    assert part2(relay_store, dataset_dir=fitting).dataset is not None


@pytest.mark.parametrize(
    "edits",
    [{"as_of": "2025-03-08"}, {"window_start": "2025-03-04"}, {"window_end": "2025-03-08"}],
    ids=["as_of", "window_start", "window_end"],
)
def test_a_dataset_manifest_for_the_same_run_but_another_as_of_or_window_is_refused(relay_store, tmp_path, edits):
    load_bhavcopy(relay_store)
    load_breeze(relay_store, "STEADY", breeze_rows("STEADY", STEADY))
    check = cc_run(relay_store, make_targets(STEADY_MEMBER))
    dataset_dir, _ = publish(relay_store, check, tmp_path / "datasets")
    edited = rewrite_manifest(dataset_dir, tmp_path / "copy", **edits)  # still verifies: rows are untouched
    refused = part2(relay_store, dataset_dir=edited)
    assert refused.dataset is None and refused.dataset_refusal_code == "dataset_window_mismatch"


@pytest.mark.parametrize("field", ["report_sha256", "targets_sha256"])
def test_a_dataset_manifest_naming_another_report_or_targets_is_refused(relay_store, tmp_path, field):
    load_bhavcopy(relay_store)
    load_breeze(relay_store, "CANBAN", breeze_rows("CANBAN", CANBK))
    check = cc_run(relay_store, make_targets(CANBK_MEMBER))
    dataset_dir, _ = publish(relay_store, check, tmp_path / "datasets")
    edited = rewrite_manifest(dataset_dir, tmp_path / "copy", **{field: "e" * 64})
    refused = part2(relay_store, dataset_dir=edited)
    assert refused.dataset is None and refused.dataset_refusal_code == "dataset_run_mismatch"


def _edit_stored_run(root, run_id, edit):
    """Change a stored cross-check's JSON in place and leave both stored hashes alone."""
    con = duckdb.connect(str(root / "pilot_india.duckdb"))
    try:
        body = json.loads(con.execute("SELECT result_json FROM crosscheck_runs WHERE run_id = ?", [run_id]).fetchone()[0])
        edit(body)
        con.execute("UPDATE crosscheck_runs SET result_json = ? WHERE run_id = ?", [json.dumps(body, sort_keys=True), run_id])
    finally:
        con.close()


@pytest.mark.parametrize(
    "edit",
    [
        lambda body: body.update(rawness_counts={"raw_confirmed": 5}),
        lambda body: body.update(totals_by_reason={"close_tolerance": 1}),
        lambda body: body["members"][0].update(rawness_overall="unverified"),
        lambda body: body.update(accepted_total=body["accepted_total"] + 100),
    ],
    ids=["rawness_counts", "totals_by_reason", "member_rawness", "accepted_total"],
)
def test_a_cross_check_edited_in_place_with_its_hashes_untouched_is_refused(tmp_path, edit):
    root = tmp_path / "root"
    with PilotDataStore(root, workspace="india") as store:
        load_bhavcopy(store)
        load_breeze(store, "CANBAN", breeze_rows("CANBAN", CANBK))
        check = cc_run(store, make_targets(CANBK_MEMBER))
        assert part2(store).crosscheck.run_id == check.run_id  # sound before the edit
    stored = (check.report_sha256,)
    _edit_stored_run(root, check.run_id, edit)
    with PilotDataStore(root, workspace="india") as store:
        row = store.query("SELECT result_sha256 FROM crosscheck_runs WHERE run_id = ?", [check.run_id])[0]
        assert row == stored  # the stored hash is exactly what it was
        with pytest.raises(PilotDataError) as caught:
            part2(store)
        assert caught.value.code == "crosscheck_record_corrupt"
        with pytest.raises(PilotDataError) as named:
            part2(store, run_id=check.run_id)
        assert named.value.code == "crosscheck_record_corrupt"


def test_a_report_file_that_already_exists_is_checked_not_trusted(relay_store, tmp_path):
    report = part2(relay_store)
    first = write_part2_report(tmp_path / "out", report)
    good = first.read_text()
    assert write_part2_report(tmp_path / "out", report) == first  # identical and 0444: reused

    first.chmod(0o644)  # a writable leftover
    with pytest.raises(PilotDataError) as writable:
        write_part2_report(tmp_path / "out", report)
    assert writable.value.code == "report_file_writable" and first.read_text() == good

    first.write_text(good.replace("crosscheck_missing", "dataset_published"))  # altered content, then read-only again
    first.chmod(0o444)
    with pytest.raises(PilotDataError) as altered:
        write_part2_report(tmp_path / "out", report)
    assert altered.value.code == "report_file_altered" and "dataset_published" in first.read_text()  # not overwritten


def test_the_part1_writer_shares_the_same_check(store, tmp_path):
    build_world(store)
    report = run(store)
    path = write_report(tmp_path / "out", report)
    path.chmod(0o644)
    path.write_text("{}")
    path.chmod(0o444)
    with pytest.raises(PilotDataError) as altered:
        write_report(tmp_path / "out", report)
    assert altered.value.code == "report_file_altered" and path.read_text() == "{}"


def test_the_part2_cli_fails_closed_on_a_writable_existing_report(tmp_path, capsys):
    root = tmp_path / "root"
    with PilotDataStore(root, workspace="india") as store:
        load_bhavcopy(store)
        load_breeze(store, "CANBAN", breeze_rows("CANBAN", CANBK))
        cc_run(store, make_targets(CANBK_MEMBER))
    argv = ["part2", "--root", str(root), "--workspace", "india", "--as-of", AS_OF.isoformat(),
            "--window-start", CC_START.isoformat(), "--window-end", CC_END.isoformat()]
    assert main(argv) == 0
    path = next((root / "reports").glob("part2-*.json"))
    capsys.readouterr()
    path.chmod(0o644)
    assert main(argv) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "report_file_writable"


def test_a_cross_check_stored_under_another_run_id_is_refused(relay_store):
    load_bhavcopy(relay_store)
    load_breeze(relay_store, "CANBAN", breeze_rows("CANBAN", CANBK))
    check = cc_run(relay_store, make_targets(CANBK_MEMBER))
    body = relay_store.query("SELECT result_json FROM crosscheck_runs WHERE run_id = ?", [check.run_id])[0][0]
    relay_store.append_rows(  # the content and its hash are both genuine; only the row's key differs
        "crosscheck_runs",
        [{"run_id": "6" * 64, "workspace": "india", "started_at_utc": datetime(2099, 1, 1), "params_json": "{}",
          "result_json": body, "result_sha256": check.report_sha256, "source_sha256": "6" * 64,
          "row_sha256": check.report_sha256}],
        check="crosscheck_run",
    )
    with pytest.raises(PilotDataError) as caught:
        part2(relay_store, run_id="6" * 64)
    assert caught.value.code == "crosscheck_record_corrupt"

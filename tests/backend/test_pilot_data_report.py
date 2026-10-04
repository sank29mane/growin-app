"""Part 1 report builder and CLI on a synthetic store."""

import json
import stat
from datetime import date, datetime, timezone

import pytest

from pilot_data.bhavcopy import ingest_pr_zip
from pilot_data.core import PilotDataError, SourceDescriptor, standard_caveats
from pilot_data.corporate_actions import derive_corporate_actions
from pilot_data.report import latest_session, main, run_part1, write_report
from pilot_data.store import PilotDataStore
from pilot_data.targets import TargetUniverseResult, ensure_target_tables
from pilot_data.core import utc_naive

import pilot_data_testkit as kit
from test_pilot_data_universe import D, SESSIONS, World, make_isin, steady, surveil

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

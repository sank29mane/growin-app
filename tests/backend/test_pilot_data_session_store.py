"""G-1a: seed an isolated session store from research-store blobs (64-08 Task 2, decision D-33)."""

import ast
import hashlib
import json
import os
import shutil
import socket
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from pilot_data import session_store
from pilot_data.bhavcopy import check_same_date_consistency, ingest_pr_zip
from pilot_data.breeze_bars import breeze_bars_for
from pilot_data.core import PilotDataError, canonical_sha256, standard_caveats
from pilot_data.corporate_actions import derive_corporate_actions
from pilot_data.crosscheck import run_crosscheck
from pilot_data.dataset import build_dataset_snapshot
from pilot_data.models import CrossCheckTolerances, QuarantineRecord
from pilot_data.nse_http import NseHttp
from pilot_data.price_bands import band_on, record_band_convention
from pilot_data.security_master import ingest_security_master
from pilot_data.session_store import (
    SEED_TABLES, SeedReport, load_seed_report, main as seed_main, seed_session_store,
)
from pilot_data.sessions import day_status
from pilot_data.store import DB_FILE_NAME, PilotDataStore
from pilot_data.surveillance import ingest_surveillance
from pilot_data.targets import TargetUniverseResult, ensure_target_tables

import pilot_data_testkit as kit
import test_pilot_data_crosscheck as cc
import test_pilot_data_price_bands as pb

D1, D2, D3, D4, D5 = cc.SESSIONS  # Mon 3 March to Fri 7 March 2025
HOLIDAY = D5 + timedelta(days=3)  # Mon 10 March: no file of any kind
EARLY = date(2025, 2, 28)  # a PR zip dated before the window
WINDOW = (D1, HOLIDAY)
BAND_OFFSET = 2


# --------------------------------------------------------------------------- builders
def put_bands(store):
    stock = {**kit.SEC_LIST_RELIANCE, "Symbol": "STEADYCO"}
    canbk_free, canbk_capped = kit.SEC_LIST_CANBK, {**kit.SEC_LIST_CANBK, "Band": "20"}
    record_band_convention(store, pb.FIXTURE, workspace="india")
    for filed in (EARLY, D1, D2, D3, D4, D5):
        capped = filed >= D3
        pb.put_list(store, filed, rows=[canbk_capped if capped else canbk_free, stock])
        change = [{**kit.BAND_CHANGE_ANANDRATHI, "Symbol": "CANBK", "From": "No Band", "To": "20"}] if filed == D3 else []
        pb.put_changes(store, filed, rows=change)


def put_reference(store):
    ingest_security_master(store, cc.desc("nse_archive", "security_master"), kit.security_master_bytes(kit.MASTER_SAMPLE_ROWS),
                           snapshot_date=D1, snapshot_date_basis="test_fixture")
    ingest_surveillance(store, cc.desc("nse_api", "surveillance_asm"),
                        kit.asm_json([kit.asm_row("STEADYCO", cc.STEADY_ISIN, when="04-Mar-2025")], []), list_name="asm")
    ingest_surveillance(store, cc.desc("nse_api", "surveillance_asm"),
                        kit.asm_json([kit.asm_row("OTHERCO", cc.OTHER_ISIN, when="20-Feb-2025")], []), list_name="asm")
    ingest_surveillance(store, cc.desc("nse_api", "surveillance_gsm"),
                        kit.gsm_json([kit.gsm_row("CANBK", cc.NEW, when="05-Mar-2025 08:00:00")]), list_name="gsm")


def put_targets(store, result):
    ensure_target_tables(store)
    newer = result.model_copy(update={"target_sha256": "u" * 64})
    for built, snapshot in ((datetime(2026, 1, 1), result), (datetime(2026, 6, 1), newer)):
        store.append_rows(
            "target_universe_snapshots",
            [{"target_sha256": snapshot.target_sha256, "workspace": "india", "as_of": snapshot.as_of,
              "built_at_utc": built, "payload_json": json.dumps(snapshot.model_dump(mode="json"), sort_keys=True),
              "source_sha256": "m" * 64, "row_sha256": snapshot.target_sha256}],
            check="target_universe",
        )


def build_research(base: Path, end: date = HOLIDAY) -> dict:
    """A research store with a split, an unresolved right, a Breeze conflict, a holiday, a weekend, a band chain,
    a PR zip dated before the window and a Breeze source whose span starts before the window."""
    root, export = base / "research", base / "export"
    targets = cc.make_targets(cc.CANBK_MEMBER, cc.STEADY_MEMBER)
    with PilotDataStore(root, workspace="india") as store:
        cc.load_bhavcopy(store)
        ingest_pr_zip(
            store, cc.desc(kind="pr_zip"),
            kit.pr_zip(EARLY, [kit.pd_index_row()],
                       [kit.bc_row("EQ", "CANBK", "CANARA BANK", "FVSPLT FRM RS 10 TO RS 2", ex_date=D3)], []),
            trade_date=EARLY)
        ingest_pr_zip(
            store, cc.desc(kind="pr_zip"),
            kit.pr_zip(D2, [kit.pd_index_row()],
                       [kit.bc_row("EQ", "STEADYCO", "STEADY CO", "RIGHTS 1:1 @ PRM RS 3/-", ex_date=D4)], []),
            trade_date=D2)
        derive_corporate_actions(store, workspace="india")
        for day in cc.SESSIONS:  # what ingest_day does after each primary file
            check_same_date_consistency(store, day, workspace="india")
        cc.mark_calendar(store, date(2025, 3, 1), date(2025, 3, 2), set())
        cc.mark_calendar(store, HOLIDAY, HOLIDAY, set())
        before = date(2025, 3, 1)  # the span starts two days before the window
        cc.load_breeze(store, "CANBAN", cc.breeze_rows("CANBAN", cc.CANBK), start=before, end=HOLIDAY,
                       locator="relay/CANBAN/full")
        cc.load_breeze(store, "STEADY", cc.breeze_rows("STEADY", cc.STEADY), start=before, end=HOLIDAY,
                       locator="relay/STEADY/full")
        shifted = cc.breeze_rows("STEADY", {D4: cc.STEADY[D4]}, overrides={D4: ("100", "106", "95", "101")})
        cc.load_breeze(store, "STEADY", shifted, start=D4, end=D4, locator="relay/STEADY/second")
        store.record_quarantine([QuarantineRecord(  # an upstream hold no re-ingest regenerates, so the seed has to copy it
            workspace="india", check="manual_review", reason_code="operator_hold", scope="raw", stock_code="STEADY",
            date_from=D2, date_to=D2)])
        put_bands(store)
        put_reference(store)
        put_targets(store, targets)
        report = run_crosscheck(store, targets=targets, window_start=cc.START, window_end=end, as_of=cc.AS_OF,
                                tolerances=CrossCheckTolerances(), workspace="india")
        manifest = build_dataset_snapshot(store, crosscheck_run_id=report.run_id, as_of=cc.AS_OF, workspace="india",
                                          export_root=export)
    return {"root": root, "dataset": export / manifest.dataset_sha256, "manifest": manifest, "targets": targets}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return build_research(tmp_path_factory.mktemp("research_world"))


@pytest.fixture
def research(world, tmp_path):
    """A private copy, safe to mutate."""
    root = tmp_path / "research_copy"
    shutil.copytree(world["root"], root)
    return {**world, "root": root}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network used in a G-1a test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(NseHttp, "__init__", refuse)


def seed(world, session_root, **overrides):
    arguments = {"dataset_dir": world["dataset"], "window_start": WINDOW[0], "window_end": WINDOW[1],
                 "workspace": "india", "band_parity_offset_days": BAND_OFFSET}
    arguments.update(overrides)
    return seed_session_store(session_root, world["root"], **arguments)


def tree_state(root: Path) -> dict:
    return {str(path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(path.stat().st_mode))
            for path in sorted(root.rglob("*")) if path.is_file()}


def parity_names(report):
    return {row.reader: row for row in report.parity}


# --------------------------------------------------------------------------- 3: parity on the full fixture
def test_the_parity_table_is_green_on_the_full_fixture(world, tmp_path):
    report = seed(world, tmp_path / "session")
    rows = parity_names(report)
    assert set(rows) == {"day_status", "primary_bars_on", "breeze_bars_for", "events_for_symbol", "snapshot_for",
                         "band_on", "latest_snapshot", "upstream_quarantine_ids", "source_files"}
    assert rows["day_status"].compared == 8 and rows["primary_bars_on"].compared == 8
    assert rows["band_on"].compared > 0 and rows["source_files"].compared > 0
    assert [c.code for c in report.caveats] == [c.code for c in standard_caveats()]
    assert report.targets.target_sha256 == world["manifest"].targets_sha256 == "t" * 64
    assert report.dataset_sha256 == world["manifest"].dataset_sha256
    assert load_seed_report(tmp_path / "session") == report
    with PilotDataStore(tmp_path / "session", workspace="india", read_only=True) as seeded:
        assert day_status(seeded, HOLIDAY) == "holiday" and day_status(seeded, D5 + timedelta(days=1)) == "weekend_no_session"
        covered = breeze_bars_for(seeded, "CANBAN", start=D1, end=HOLIDAY).covered_dates
        assert covered == ((D1, HOLIDAY),)  # the research span started on 1 March; the seed clipped it
        assert breeze_bars_for(seeded, "STEADY", start=D1, end=HOLIDAY).conflict_on(D4) is not None
        observed = band_on(seeded, isin=cc.NEW, session=D4, workspace="india")
        assert (observed.status, str(observed.percent)) == ("fixed", "20.00"), observed


# --------------------------------------------------------------------------- 1: the research store is never writable
def test_the_research_store_is_only_ever_opened_read_only_and_its_bytes_do_not_move(world, tmp_path, monkeypatch):
    opens = []
    real = duckdb.connect

    def recording(database=":memory:", *args, **kwargs):
        read_only = kwargs.get("read_only", args[1] if len(args) > 1 else False)
        opens.append((str(database), bool(read_only)))
        return real(database, *args, **kwargs)

    monkeypatch.setattr("pilot_data.store.duckdb.connect", recording)
    before = tree_state(world["root"])
    seed(world, tmp_path / "session")
    research_db = str(world["root"] / DB_FILE_NAME)
    mine = [read_only for path, read_only in opens if path == research_db]
    assert mine and all(mine), opens
    assert tree_state(world["root"]) == before
    assert not any(path.startswith(str(world["root"])) and not read_only for path, read_only in opens)


# --------------------------------------------------------------------------- 2: hashes, sizes and first-fetch times survive
def test_every_seeded_source_keeps_its_hash_size_and_first_fetch_time(world, tmp_path):
    report = seed(world, tmp_path / "session")
    seeded_shas = sorted({sha for shas in report.sources_by_kind.values() for sha in shas})
    assert len(seeded_shas) >= 15
    columns = "source_sha256, source, kind, locator, first_fetched_at_utc, byte_size, for_date"
    with PilotDataStore(world["root"], workspace="india", read_only=True) as research, \
            PilotDataStore(tmp_path / "session", workspace="india", read_only=True) as seeded:
        for sha in seeded_shas:
            query = f"SELECT {columns} FROM source_files WHERE source_sha256 = ?"
            assert seeded.query(query, [sha]) == research.query(query, [sha]) != [], sha
            assert seeded.read_blob(sha) == research.read_blob(sha)
        stamps = {row[0] for row in seeded.query("SELECT first_fetched_at_utc FROM source_files")}
        assert stamps == {cc.FETCHED.replace(tzinfo=None)}  # the fixture's fetch time, not the seed's wall clock


# --------------------------------------------------------------------------- 4: refusals write nothing
def test_overlap_window_workspace_and_non_empty_refusals_write_nothing(world, tmp_path):
    research_root, dataset = world["root"], world["dataset"]
    link = tmp_path / "link_to_research"
    link.symlink_to(research_root, target_is_directory=True)
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "note.txt").write_text("x")
    a_file = tmp_path / "a_file"
    a_file.write_text("x")
    cases = {
        "equal": (research_root, "seed_root_overlap"),
        "inside_research": (research_root / "inner", "seed_root_overlap"),
        "contains_research": (research_root.parent, "seed_root_overlap"),
        "symlink_to_research": (link, "seed_root_overlap"),
        "symlink_below_research": (link / "inner", "seed_root_overlap"),
        "inside_dataset": (dataset / "inner", "seed_root_overlap"),
        "non_empty": (busy, "seed_root_not_empty"),
        "a_file": (a_file, "seed_root_not_empty"),
    }
    before = (tree_state(research_root), tree_state(dataset), sorted(p.name for p in tmp_path.iterdir()))
    for name, (target, code) in cases.items():
        existed = target.exists()
        with pytest.raises(PilotDataError) as caught:
            seed(world, target)
        assert caught.value.code == code, name
        assert target.exists() == existed, name
    with pytest.raises(PilotDataError) as order:
        seed(world, tmp_path / "fresh", window_start=D5, window_end=D1)
    with pytest.raises(PilotDataError) as space:
        seed(world, tmp_path / "fresh", workspace="uk")
    assert (order.value.code, space.value.code) == ("seed_window_invalid", "workspace_invalid")
    assert not (tmp_path / "fresh").exists()
    assert (tree_state(research_root), tree_state(dataset), sorted(p.name for p in tmp_path.iterdir())) == before
    assert (busy / "note.txt").read_text() == "x" and [p.name for p in busy.iterdir()] == ["note.txt"]


def test_a_failed_verify_dataset_and_a_missing_research_store_stop_before_any_write(world, tmp_path):
    broken = tmp_path / "dataset_copy"
    shutil.copytree(world["dataset"], broken)
    rows = broken / "rows.parquet"
    os.chmod(rows, 0o644)
    rows.write_bytes(rows.read_bytes() + b"x")
    with pytest.raises(PilotDataError) as tampered:
        seed(world, tmp_path / "one", dataset_dir=broken)
    with pytest.raises(PilotDataError) as missing:
        seed_session_store(tmp_path / "two", tmp_path / "no_such_store", dataset_dir=world["dataset"],
                           window_start=D1, window_end=HOLIDAY, workspace="india")
    assert tampered.value.code == "dataset_integrity" and missing.value.code in {"store_missing", "seed_research_unavailable"}
    assert not (tmp_path / "one").exists() and not (tmp_path / "two").exists()


# --------------------------------------------------------------------------- 5: every table is classified
def test_every_research_table_has_a_seed_method_and_every_skip_has_a_reason(world, tmp_path):
    named = {"pilot_store_identity", "source_files", "fetch_events", "target_universe_snapshots", "quarantine_records",
             "nse_fetch_log", "bhavcopy_bars", "breeze_bars_raw", "breeze_responses", "price_band_files",
             "security_master_rows", "surveillance_entries"}
    tables_sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name"
    with PilotDataStore(world["root"], workspace="india", read_only=True) as research:
        present = {row[0] for row in research.query(tables_sql)}
    ca_tables = {name for name in present if name.startswith("corporate_action_")}
    assert named <= present and len(ca_tables) == 2, present
    assert present <= set(SEED_TABLES), sorted(present - set(SEED_TABLES))
    assert set(SEED_TABLES.values()) <= {"reingest", "derive", "copy", "pinned", "own", "not_seeded"}
    assert all(SEED_TABLES[name] == "derive" for name in ca_tables)
    skipped = {name for name, method in SEED_TABLES.items() if method == "not_seeded"}
    assert skipped == set(session_store.NOT_SEEDED_REASONS) and all(session_store.NOT_SEEDED_REASONS.values())
    report = seed(world, tmp_path / "session")
    assert report.table_methods == dict(sorted(SEED_TABLES.items()))
    with PilotDataStore(tmp_path / "session", workspace="india", read_only=True) as seeded:
        assert {row[0] for row in seeded.query(tables_sql)} <= set(SEED_TABLES)


# --------------------------------------------------------------------------- 6: no network, no HTTP or relay import
def test_no_network_is_used_and_the_module_imports_no_http_or_relay_code(world, tmp_path):
    seed(world, tmp_path / "session")  # the autouse fixture bans socket connects and NseHttp for the whole test
    tree = ast.parse(Path(session_store.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
            imported |= {alias.name for alias in node.names}
    banned = {"httpx", "requests", "urllib", "urllib3", "socket", "nse_http", "NseHttp", "relay", "relay_client", "subprocess"}
    assert not banned & {part for name in imported for part in name.replace(".", " ").split()}, imported


# --------------------------------------------------------------------------- 7: coverage honesty
def test_a_span_the_session_calendar_cannot_support_is_refused_not_papered_over(research, tmp_path):
    source = Path(session_store.__file__).read_text(encoding="utf-8")
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)]
    assert not [node for node in calls if getattr(node.func, "id", None) == "WindowAssessment"]
    assert "complete_over" not in source
    connection = duckdb.connect(str(research["root"] / DB_FILE_NAME))
    connection.execute("DELETE FROM nse_fetch_log WHERE trade_date = ?", [D3])
    connection.close()
    with pytest.raises(PilotDataError) as caught:
        seed(research, tmp_path / "session")
    assert caught.value.code == "seed_coverage_span" and "CANBAN" in str(caught.value)


# --------------------------------------------------------------------------- 8: the frozen dataset's targets, not the newest
def test_targets_are_the_datasets_pin_never_the_newest_snapshot(research, tmp_path):
    with PilotDataStore(research["root"], workspace="india", read_only=True) as store:
        rows = store.query("SELECT target_sha256 FROM target_universe_snapshots ORDER BY built_at_utc")
    assert [row[0] for row in rows] == ["t" * 64, "u" * 64]  # the newer snapshot is not the dataset's
    report = seed(research, tmp_path / "session")
    assert report.targets.target_sha256 == report.targets_sha256 == research["manifest"].targets_sha256 == "t" * 64
    assert report.targets == research["targets"]
    connection = duckdb.connect(str(research["root"] / DB_FILE_NAME))
    connection.execute("DELETE FROM target_universe_snapshots WHERE target_sha256 = ?", ["t" * 64])
    connection.close()
    with pytest.raises(PilotDataError) as caught:
        seed(research, tmp_path / "second")
    assert caught.value.code == "seed_targets_missing" and not (tmp_path / "second").exists()


# --------------------------------------------------------------------------- the report and the CLI
def test_the_report_is_pinned_read_only_and_tamper_evident_and_the_cli_prints_caveats_first(world, tmp_path, capsys):
    report = seed(world, tmp_path / "session")
    path = tmp_path / "session" / "seed_report.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert report.report_sha256 == report.expected_sha256()
    with PilotDataStore(tmp_path / "session", workspace="india", read_only=True) as seeded:
        row = seeded.query("SELECT seed_report_sha256, dataset_sha256, window_start, window_end FROM session_store_seeds")
    assert row == [(report.report_sha256, report.dataset_sha256, WINDOW[0], WINDOW[1])]
    os.chmod(path, 0o644)
    document = json.loads(path.read_text())
    document["fetch_log_rows"] += 1
    path.write_text(json.dumps(document))
    with pytest.raises(PilotDataError) as caught:
        load_seed_report(tmp_path / "session")
    assert caught.value.code == "seed_report_tampered"
    code = seed_main(["seed", "--research-root", str(world["root"]), "--dataset", str(world["dataset"]),
                      "--session-root", str(tmp_path / "cli"), "--window-start", D1.isoformat(),
                      "--window-end", HOLIDAY.isoformat(), "--workspace", "india"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and list(out)[0] == "caveats" and out["seed_report_sha256"] == load_seed_report(tmp_path / "cli").report_sha256
    refused = seed_main(["seed", "--research-root", str(world["root"]), "--dataset", str(world["dataset"]),
                         "--session-root", str(world["root"]), "--window-start", D1.isoformat(),
                         "--window-end", HOLIDAY.isoformat(), "--workspace", "india"])
    failure = json.loads(capsys.readouterr().out)
    assert refused == 2 and list(failure)[0] == "caveats" and failure["error_code"] == "seed_root_overlap"

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
from pilot_data.constituents import index_members, ingest_index_list, latest_index_list
from pilot_data.core import PilotDataError, SourceDescriptor, canonical_sha256, standard_caveats
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
import test_pilot_data_reference_lists as rl

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


UTC = timezone.utc
CANBK_ROW, STEADY_ROW = ("CANBK", cc.NEW), ("STEADYCO", cc.STEADY_ISIN)
# (list, fetched_at, first filler member, named members). The window starts 3 March 2025 IST, so the cutoff is
# 00:00 IST on 4 March, which is 18:30 UTC on 3 March. "n_edge" is fetched at exactly that instant.
INDEX_LISTS = {
    "n_old": ("nifty500", datetime(2025, 2, 20, 10, 0, tzinfo=UTC), 1, (CANBK_ROW, STEADY_ROW)),
    "n_on": ("nifty500", datetime(2025, 3, 3, 12, 0, tzinfo=UTC), 3000, (CANBK_ROW, STEADY_ROW)),
    "n_edge": ("nifty500", datetime(2025, 3, 3, 18, 30, tzinfo=UTC), 6000, (CANBK_ROW, STEADY_ROW)),
    "n_late": ("nifty500", datetime(2026, 10, 2, 10, 0, tzinfo=UTC), 9000, (CANBK_ROW,)),
    "s_old": ("smallcap250", datetime(2025, 2, 20, 10, 0, tzinfo=UTC), 20000, (STEADY_ROW,)),
    "s_late": ("smallcap250", datetime(2025, 3, 6, 10, 0, tzinfo=UTC), 30000, (CANBK_ROW, STEADY_ROW)),
}
SEEDED_LISTS = ("n_old", "n_on", "s_old")
LATER_LISTS = ("n_edge", "n_late", "s_late")


def index_blob(key):
    name, _, start, named = INDEX_LISTS[key]
    count = {"nifty500": 500, "smallcap250": 250}[name]
    rows = rl.members(count - len(named), start) + [
        {"Company Name": symbol, "Industry": "Banks", "Symbol": symbol, "Series": "EQ", "ISIN Code": isin}
        for symbol, isin in named]
    return kit.index_list_csv(rows + [kit.DUMMY_INDEX_ROW])


def put_index_lists(store):
    for key, (name, fetched, _, _) in INDEX_LISTS.items():
        ingest_index_list(
            store, SourceDescriptor(source="nse_archive", kind=f"index_list_{name}", locator=f"archive/{key}",
                                    fetched_at=fetched), index_blob(key), list_name=name)


def blob_sha(key):
    return hashlib.sha256(index_blob(key)).hexdigest()


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
        put_index_lists(store)
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
                         "band_on", "latest_index_list", "latest_snapshot", "upstream_quarantine_ids",
                         "source_files"}
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
        stamps = {row[0] for row in seeded.query(
            "SELECT first_fetched_at_utc FROM source_files WHERE kind NOT LIKE 'index_list_%'")}
        assert stamps == {cc.FETCHED.replace(tzinfo=None)}  # the fixture's fetch time, not the seed's wall clock
        lists = {row[0]: row[1] for row in seeded.query(
            "SELECT source_sha256, first_fetched_at_utc FROM source_files WHERE kind LIKE 'index_list_%'")}
        assert lists == {blob_sha(key): INDEX_LISTS[key][1].replace(tzinfo=None) for key in SEEDED_LISTS}


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
    assert {name: SEED_TABLES[name] for name in ("index_list_snapshots", "index_list_members")} == {
        "index_list_snapshots": "reingest", "index_list_members": "reingest"}
    assert {"index_list_snapshots", "index_list_members"} <= present  # the fixture holds lists, so the row bites
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


# --------------------------------------------------------------------------- index lists (64-08 follow-up)
def snapshot_rows(store, shas=None):
    rows = store.query("SELECT source_sha256, list_name, fetched_at_utc, member_count, placeholders_json, row_sha256 "
                       "FROM index_list_snapshots ORDER BY source_sha256, list_name")
    return [row for row in rows if shas is None or row[0] in shas]


def test_lists_fetched_by_the_window_start_are_seeded_with_their_hash_fetch_time_and_members(world, tmp_path):
    report = seed(world, tmp_path / "session")
    wanted = {blob_sha(key) for key in SEEDED_LISTS}
    assert set(report.sources_by_kind["index_list"]) == wanted and len(wanted) == 3
    assert report.table_methods["index_list_snapshots"] == report.table_methods["index_list_members"] == "reingest"
    with PilotDataStore(world["root"], workspace="india", read_only=True) as research, \
            PilotDataStore(tmp_path / "session", workspace="india", read_only=True) as seeded:
        assert snapshot_rows(seeded) == snapshot_rows(research, wanted) and len(snapshot_rows(seeded)) == 3
        members_sql = ("SELECT source_sha256, list_name, isin, nse_symbol, series, company_name, industry, row_sha256 "
                       "FROM index_list_members ORDER BY source_sha256, list_name, isin")
        assert seeded.query(members_sql) == [row for row in research.query(members_sql) if row[0] in wanted]
        for key in SEEDED_LISTS:
            sha = blob_sha(key)
            sql = "SELECT * FROM source_files WHERE source_sha256 = ?"
            assert seeded.query(sql, [sha]) == research.query(sql, [sha]) != []
            assert seeded.read_blob(sha) == research.read_blob(sha) == index_blob(key)
            assert seeded.query("SELECT fetched_at_utc, placeholders_json FROM index_list_snapshots "
                                "WHERE source_sha256 = ?", [sha])[0] == (
                INDEX_LISTS[key][1].replace(tzinfo=None), "DUMMYHEG")  # fetch time and the dropped placeholder survive
    assert {row.reader for row in report.parity} >= {"latest_index_list"}
    assert load_seed_report(tmp_path / "session") == report and report.report_sha256 == report.expected_sha256()


def test_a_list_fetched_after_the_window_start_never_enters_the_session_store(world, tmp_path):
    seed(world, tmp_path / "session")
    cutoff = datetime(2025, 3, 3, 18, 30)  # 00:00 IST on 4 March, naive UTC
    with PilotDataStore(world["root"], workspace="india", read_only=True) as research, \
            PilotDataStore(tmp_path / "session", workspace="india", read_only=True) as seeded:
        assert research.query("SELECT count(*) FROM index_list_snapshots")[0][0] == 6  # the research store has them
        assert seeded.query("SELECT count(*) FROM index_list_snapshots WHERE fetched_at_utc >= ?", [cutoff])[0][0] == 0
        for key in LATER_LISTS:
            sha = blob_sha(key)
            assert seeded.query("SELECT count(*) FROM source_files WHERE source_sha256 = ?", [sha])[0][0] == 0, key
            assert seeded.query("SELECT count(*) FROM index_list_members WHERE source_sha256 = ?", [sha])[0][0] == 0, key
            assert seeded.query("SELECT count(*) FROM index_list_snapshots WHERE source_sha256 = ?", [sha])[0][0] == 0
        # the same read the other way round: the research store's newest lists are the late ones, the seeded store's are not
        assert latest_index_list(research, "nifty500").source_sha256 == blob_sha("n_late")
        assert latest_index_list(seeded, "nifty500").source_sha256 == blob_sha("n_on")
        assert latest_index_list(seeded, "smallcap250").source_sha256 == blob_sha("s_old")


def test_latest_index_list_fetched_by_the_window_start_finds_the_expected_list_on_the_seeded_store(world, tmp_path):
    seed(world, tmp_path / "session")
    with PilotDataStore(tmp_path / "session", workspace="india", read_only=True) as seeded:
        nifty = latest_index_list(seeded, "nifty500", fetched_by=WINDOW[0])
        small = latest_index_list(seeded, "smallcap250", fetched_by=WINDOW[0])
        assert (nifty.source_sha256, nifty.fetched_at, nifty.member_count, nifty.placeholders_excluded) == (
            blob_sha("n_on"), INDEX_LISTS["n_on"][1], 500, ("DUMMYHEG",))
        assert (small.source_sha256, small.fetched_at, small.member_count) == (
            blob_sha("s_old"), INDEX_LISTS["s_old"][1], 250)
        assert {m.isin for m in index_members(seeded, nifty)} >= {cc.NEW, cc.STEADY_ISIN}
        assert {m.isin for m in index_members(seeded, small)} >= {cc.STEADY_ISIN} and cc.NEW not in {
            m.isin for m in index_members(seeded, small)}
        # earlier decision days read the earlier list: the history is seeded, not only the newest
        assert latest_index_list(seeded, "nifty500", fetched_by=date(2025, 2, 28)).source_sha256 == blob_sha("n_old")
        assert latest_index_list(seeded, "nifty500", fetched_by=date(2025, 2, 19)) is None


def test_a_research_store_without_index_list_tables_seeds_and_the_seeded_store_reads_none(research, tmp_path):
    connection = duckdb.connect(str(research["root"] / DB_FILE_NAME))
    connection.execute("DROP TABLE index_list_members")
    connection.execute("DROP TABLE index_list_snapshots")
    connection.close()
    report = seed(research, tmp_path / "session")
    assert "index_list" not in report.sources_by_kind and "latest_index_list" not in parity_names(report)
    with PilotDataStore(tmp_path / "session", workspace="india", read_only=True) as seeded:
        assert latest_index_list(seeded, "nifty500", fetched_by=WINDOW[0]) is None  # a read, not a read-only refusal


def test_a_snapshot_whose_fetch_time_its_source_row_does_not_carry_is_refused(research, tmp_path):
    connection = duckdb.connect(str(research["root"] / DB_FILE_NAME))
    connection.execute("UPDATE index_list_snapshots SET fetched_at_utc = fetched_at_utc + INTERVAL 1 HOUR "
                       "WHERE source_sha256 = ?", [blob_sha("n_on")])
    connection.close()
    with pytest.raises(PilotDataError) as caught:
        seed(research, tmp_path / "session")
    assert caught.value.code == "seed_index_list_fetch_time"


# --------------------------------------------------------------------------- index list parity covers every snapshot
def tamper_seeded_lists(monkeypatch, *statements):
    """Run the real index list seed, then change the seeded store behind the seed's back with ``(sql, params)``."""
    real = session_store._seed_index_lists

    def seed_then_tamper(research, session, window):
        shas = real(research, session, window)
        for sql, params in statements:
            session._con.execute(sql, params)
        return shas

    monkeypatch.setattr(session_store, "_seed_index_lists", seed_then_tamper)


def drop_canbk(key):
    return ("DELETE FROM index_list_members WHERE source_sha256 = ? AND isin = ?", [blob_sha(key), cc.NEW])


def bump_count(key):
    return ("UPDATE index_list_snapshots SET member_count = member_count - 1 WHERE source_sha256 = ?", [blob_sha(key)])


SEEDED_SIDE_TAMPERS = {
    "member of the newest snapshot": drop_canbk("n_on"),
    "member of an older snapshot": drop_canbk("n_old"),
    "member of the only smallcap snapshot": ("DELETE FROM index_list_members WHERE source_sha256 = ?",
                                              [blob_sha("s_old")]),
    "member_count of the newest snapshot": bump_count("n_on"),
    "member_count of an older snapshot": bump_count("n_old"),
    "fetch time of an older snapshot": ("UPDATE index_list_snapshots SET fetched_at_utc = fetched_at_utc + "
                                        "INTERVAL 1 MINUTE WHERE source_sha256 = ?", [blob_sha("n_old")]),
    "a whole older snapshot": ("DELETE FROM index_list_snapshots WHERE source_sha256 = ?", [blob_sha("n_old")]),
}


@pytest.mark.parametrize("what", sorted(SEEDED_SIDE_TAMPERS))
def test_a_seeded_index_list_changed_after_the_seed_fails_the_parity_table(world, tmp_path, monkeypatch, what):
    tamper_seeded_lists(monkeypatch, SEEDED_SIDE_TAMPERS[what])
    with pytest.raises(PilotDataError) as caught:
        seed(world, tmp_path / "session")
    assert caught.value.code == "seed_parity_failed" and "reader latest_index_list" in str(caught.value), what


def test_a_member_deleted_from_a_seeded_older_snapshot_alone_is_refused_though_the_newest_lists_match(world, tmp_path,
                                                                                                    monkeypatch):
    tamper_seeded_lists(monkeypatch, drop_canbk("n_old"))
    real = session_store._parity
    seen = {}

    def newest_only_equal(research, session, **kwargs):
        # the research store and the seeded one still agree on every name's newest snapshot and its members
        for name in ("nifty500", "smallcap250"):
            mine, theirs = latest_index_list(session, name), latest_index_list(research, name, fetched_by=WINDOW[0])
            seen[name] = mine == theirs and index_members(session, mine) == index_members(research, theirs)
        return real(research, session, **kwargs)

    monkeypatch.setattr(session_store, "_parity", newest_only_equal)
    with pytest.raises(PilotDataError) as caught:
        seed(world, tmp_path / "session")
    assert seen == {"nifty500": True, "smallcap250": True}  # only the older snapshot differed
    assert caught.value.code == "seed_parity_failed"


@pytest.mark.parametrize("sql", [
    "DELETE FROM index_list_members WHERE source_sha256 = '{sha}' AND isin = '{isin}'",
    "UPDATE index_list_snapshots SET member_count = member_count + 1 WHERE source_sha256 = '{sha}'",
], ids=["member", "member_count"])
@pytest.mark.parametrize("key", ["n_old", "n_on"])
def test_a_research_index_list_row_the_blob_does_not_explain_fails_the_parity_table(research, tmp_path, key, sql):
    connection = duckdb.connect(str(research["root"] / DB_FILE_NAME))
    connection.execute(sql.format(sha=blob_sha(key), isin=cc.NEW))
    connection.close()
    with pytest.raises(PilotDataError) as caught:
        seed(research, tmp_path / "session")
    assert caught.value.code == "seed_parity_failed" and "reader latest_index_list" in str(caught.value)

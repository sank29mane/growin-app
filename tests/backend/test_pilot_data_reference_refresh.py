"""Daily reference refresh: step records, missed-run detection, launchd template and installer."""

import ast
import json
import os
import plistlib
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from pilot_data import reference_refresh as rr
from pilot_data.core import PilotDataError, SourceDescriptor, canonical_sha256
from pilot_data.nse_http import NseHttp
from pilot_data.nse_ingest import _log_attempt, pr_zip_url, udiff_url
from pilot_data.price_bands import BAND_CHANGES_URL, SEC_LIST_DATED_URL, ingest_band_list
from pilot_data.reference_refresh import (
    LAUNCHD_LABEL,
    continuity_report,
    install_launchd,
    main,
    render_launchd_plist,
    require_reference_freshness,
    run_daily,
)
from pilot_data.sessions import ensure_fetch_log
from pilot_data.store import PilotDataStore
from pilot_data.surveillance import ASM_URL, GSM_URL, ingest_surveillance

import pilot_data_testkit as kit

FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
LATER = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
D1, D2, D5, D6 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
BANDS = [kit.SEC_LIST_RELIANCE, kit.SEC_LIST_CANBK]
NSE_HOSTS = {"nsearchives.nseindia.com", "www.nseindia.com"}


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


def stamp(day):
    return f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"


def mark_calendar(store, start, end, data_days, skip=()):
    ensure_fetch_log(store)
    day = start
    while day <= end:
        if day in skip:
            pass
        elif day in data_days:
            _log_attempt(store, d=day, kind="udiff", outcome="ingested", http_status=200, error_code=None,
                         source_sha256=None, url="u", attempted_at=LATER)
        else:
            for kind in (("pr",) if day.weekday() >= 5 else ("pr", "udiff", "cm_legacy")):
                _log_attempt(store, d=day, kind=kind, outcome="no_file", http_status=404, error_code=None,
                             source_sha256=None, url="u", attempted_at=LATER)
        day += timedelta(days=1)


def snapshots(store, day):
    desc = lambda kind: SourceDescriptor(source="nse_api", kind=kind, locator="https://www.nseindia.com/api/x",
                                         fetched_at=FETCHED)
    ingest_surveillance(store, desc("surveillance_asm"), kit.asm_json([kit.asm_row("AAA", None, when=stamp(day))], []),
                        list_name="asm")
    ingest_surveillance(store, desc("surveillance_gsm"),
                        kit.gsm_json([kit.gsm_row("AAA", None, when=stamp(day) + " 08:00:00")]), list_name="gsm")
    ingest_band_list(
        store, SourceDescriptor(source="nse_archive", kind="price_band_list", locator="https://nsearchives.nseindia.com/b",
                                fetched_at=FETCHED, for_date=day),
        kit.sec_list_csv(BANDS), file_date=day, workspace="india",
    )


def record_success(store, ist_date):
    rr.ensure_refresh_tables(store)
    run_id = canonical_sha256({"seed": ist_date.isoformat()})
    for step in ("surveillance", "price_bands"):
        store.append_rows(
            "reference_refresh_runs",
            [{"run_id": run_id, "step": step, "ist_date": ist_date, "started_at_utc": datetime(2026, 1, 1),
              "finished_at_utc": datetime(2026, 1, 1), "outcome": "ok", "error_code": None, "detail_json": "{}",
              "row_sha256": canonical_sha256({"r": run_id, "s": step}), "source_sha256": run_id}],
            check="reference_refresh_run",
        )


# ------------------------------------------------------------------ missed-run detection
def gap_world(store, snapshot_days=(D1, D2, D6), skip=()):
    mark_calendar(store, D1, date(2026, 10, 7), {D1, D2, D5, D6}, skip=skip)
    for day in snapshot_days:
        snapshots(store, day)


def test_a_missed_session_is_a_gap_and_fails_the_gate(store):
    gap_world(store)
    report = continuity_report(store, today_ist=D6)
    assert report.first_collection_date == D1 and report.latest_session == D6
    assert report.missing_asm == report.missing_gsm == report.missing_band_list == (D5,)
    assert report.fresh is False and "reference_refresh_gap" in report.codes
    with pytest.raises(PilotDataError) as caught:
        require_reference_freshness(store, today_ist=D6)
    assert caught.value.code == "reference_refresh_gap" and "2026-10-05" in str(caught.value)


def test_a_calendar_day_with_no_outcome_is_listed_and_fails_closed(store):
    gap_world(store, snapshot_days=(D1, D2, D6), skip={D5})
    report = continuity_report(store, today_ist=D6)
    assert report.unknown_calendar_dates == (D5,)
    with pytest.raises(PilotDataError) as caught:
        require_reference_freshness(store, today_ist=D6)
    assert caught.value.code == "calendar_unknown_dates"


def test_staleness_needs_a_recent_successful_run(store):
    gap_world(store, snapshot_days=(D1, D2, D5, D6))
    assert continuity_report(store, today_ist=D6).missing_asm == ()
    with pytest.raises(PilotDataError) as stale:
        require_reference_freshness(store, today_ist=D6)
    assert stale.value.code == "reference_refresh_stale"
    record_success(store, D2)  # a run from last Friday is not recent enough on Tuesday
    with pytest.raises(PilotDataError):
        require_reference_freshness(store, today_ist=D6)
    record_success(store, D5)  # the previous weekday counts
    ok = require_reference_freshness(store, today_ist=D6)
    assert ok.fresh and ok.last_successful_run_ist_date == D5 and ok.codes == ()


def test_no_collection_at_all_is_stale(store):
    with pytest.raises(PilotDataError) as caught:
        require_reference_freshness(store, today_ist=D6)
    assert caught.value.code == "reference_refresh_stale"


# ------------------------------------------------------------------ run_daily
class Site:
    def __init__(self):
        self.routes = {}
        self.hosts = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        route = self.routes.get(str(request.url), 404)
        return httpx.Response(200, content=route) if isinstance(route, bytes) else httpx.Response(route, content=b"x")

    def http(self):
        return NseHttp(httpx.Client(transport=httpx.MockTransport(self.handler)), min_interval_seconds=0.0,
                       sleeper=lambda s: None)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def today_site(day=D5):
    site = Site()
    site.routes[udiff_url(day)] = kit.udiff_zip(day, [kit.udiff_row("RELIANCE", "EQ", "INE002A01018", "100", "101", "99",
                                                                   "100", trade_date=day)])
    site.routes[ASM_URL] = kit.asm_json([kit.asm_row("AAA", None, when=stamp(day))], [])
    site.routes[GSM_URL] = kit.gsm_json([kit.gsm_row("AAA", None, when=stamp(day) + " 08:00:00")])
    ddmmyyyy = f"{day.day:02d}{day.month:02d}{day.year}"
    site.routes[SEC_LIST_DATED_URL.format(ddmmyyyy=ddmmyyyy)] = kit.sec_list_csv(BANDS)
    site.routes[BAND_CHANGES_URL.format(ddmmyyyy=ddmmyyyy)] = kit.band_changes_csv([])
    return site


def test_run_daily_runs_every_step_in_order_and_never_asks_for_the_master(store):
    mark_calendar(store, date(2026, 10, 3), date(2026, 10, 4), set())
    site = today_site()
    report = run_daily(store, site.http(), today_ist=D5, workspace="india", clock=lambda: datetime(2026, 10, 5, 15, 15, tzinfo=timezone.utc))
    assert [(s.step, s.outcome) for s in report.steps] == [
        ("bhavcopy_day", "ok"), ("surveillance", "ok"), ("price_bands", "ok"), ("security_master", "skipped")]
    assert report.steps[3].error_code == "security_master_refresh_requires_relay"
    assert set(site.hosts) <= NSE_HOSTS  # no ICICI or Breeze host, and nothing for the master
    rows = store.query("SELECT step, outcome, ist_date FROM reference_refresh_runs ORDER BY started_at_utc, step")
    assert sorted(r[0] for r in rows) == ["bhavcopy_day", "price_bands", "security_master", "surveillance"]
    assert {r[2] for r in rows} == {D5}
    assert [c.code for c in report.caveats] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS", "SURVEILLANCE_HISTORY_UNAVAILABLE"]
    dumped = report.model_dump(mode="json")
    assert dumped["surveillance_history_recoverable"] is False
    assert dumped["surveillance_history_note"] == (
        "ASM and GSM are served for today only; this job cannot recover surveillance history from before "
        "collection began or for a missed day.")
    reports = list((store.root / "reports").glob("reference-refresh-2026-10-05-*.json"))
    assert len(reports) == 1 and stat.S_IMODE(reports[0].stat().st_mode) == 0o444
    assert list(json.loads(reports[0].read_text()))[0] == "caveats"


def test_one_failing_step_does_not_stop_the_others_and_the_cli_exits_two(tmp_path, capsys):
    site = today_site()
    site.routes[ASM_URL] = 403
    root = tmp_path / "root"
    code = main(["run-daily", "--root", str(root), "--workspace", "india", "--min-interval-seconds", "0"],
                client=site.client())
    printed = json.loads(capsys.readouterr().out)
    assert code == 2 and list(printed)[0] == "caveats"
    steps = {s["step"]: s for s in printed["steps"]}
    assert steps["surveillance"]["outcome"] == "failed" and steps["surveillance"]["error_code"] == "nse_fetch_refused"
    assert steps["security_master"]["outcome"] == "skipped"
    assert printed["surveillance_history_recoverable"] is False


def test_a_weekend_skips_the_band_step(store):
    saturday = date(2026, 10, 3)
    site = Site()
    site.routes[ASM_URL] = kit.asm_json([kit.asm_row("AAA", None, when=stamp(D2))], [])
    site.routes[GSM_URL] = kit.gsm_json([kit.gsm_row("AAA", None, when=stamp(D2) + " 08:00:00")])
    report = run_daily(store, site.http(), today_ist=saturday, workspace="india", clock=lambda: LATER)
    steps = {s.step: s for s in report.steps}
    assert (steps["price_bands"].outcome, steps["price_bands"].error_code) == ("skipped", "not_a_session")


# ------------------------------------------------------------------ plist rendering
def render(**overrides):
    values = dict(uv_bin=Path("/opt/bin/uv"), repo_root=Path("/Users/x/Growin App"),
                  data_root=Path("/Users/x/Growin App/backend/data/pilot_india"), hour=20, minute=45)
    values.update(overrides)
    return render_launchd_plist(**values)


def test_rendered_plist_matches_the_contract():
    parsed = plistlib.loads(render().encode("utf-8"))
    assert parsed["Label"] == LAUNCHD_LABEL == "com.growin.pilot-reference-refresh"
    assert parsed["ProgramArguments"] == [
        "/opt/bin/uv", "run", "--project", "/Users/x/Growin App/backend", "--no-sync", "python", "-m",
        "pilot_data.reference_refresh", "run-daily", "--root", "/Users/x/Growin App/backend/data/pilot_india",
        "--workspace", "india"]
    assert parsed["EnvironmentVariables"] == {"PYTHONPATH": "/Users/x/Growin App/backend"}
    assert parsed["WorkingDirectory"] == "/Users/x/Growin App"
    assert parsed["StartCalendarInterval"] == [{"Weekday": day, "Hour": 20, "Minute": 45} for day in range(1, 6)]
    assert parsed["StandardOutPath"].startswith("/Users/x/Growin App/backend/data/pilot_india/logs/")
    assert parsed["StandardErrorPath"].startswith("/Users/x/Growin App/backend/data/pilot_india/logs/")
    assert parsed["RunAtLoad"] is False and parsed["ProcessType"] == "Background"


def test_special_characters_are_escaped_and_bad_templates_are_rejected(monkeypatch, tmp_path):
    parsed = plistlib.loads(render(repo_root=Path("/Users/a&b/<odd>")).encode("utf-8"))
    assert parsed["WorkingDirectory"] == "/Users/a&b/<odd>"
    bad = tmp_path / "bad.template"
    bad.write_text(TEMPLATE_WITH("{{NOPE}}"))
    monkeypatch.setattr(rr, "TEMPLATE_PATH", bad)
    with pytest.raises(PilotDataError) as unknown:
        render()
    assert unknown.value.code == "launchd_template_invalid"
    stray = tmp_path / "stray.template"
    stray.write_text(TEMPLATE_WITH("{{ {{UV_BIN}}"))
    monkeypatch.setattr(rr, "TEMPLATE_PATH", stray)
    with pytest.raises(PilotDataError) as unfilled:
        render()
    assert unfilled.value.code == "launchd_template_invalid"


def TEMPLATE_WITH(extra: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>'
            + extra + "</string></dict></plist>")


# ------------------------------------------------------------------ installer
@pytest.fixture
def install_env(tmp_path):
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text("#!/bin/sh\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    return dict(uv_bin=uv, repo_root=repo, data_root=tmp_path / "data", hour=20, minute=45, home=tmp_path / "home",
                system_timezone="Asia/Kolkata")


def test_install_writes_only_the_plist_and_returns_the_launchctl_commands(install_env):
    outcome = install_launchd(**install_env)
    target = install_env["home"] / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    assert Path(outcome.path) == target and outcome.written
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    uid = os.getuid()
    assert outcome.commands == (f"launchctl bootstrap gui/{uid} {target}", f"launchctl print gui/{uid}/{LAUNCHD_LABEL}")
    assert plistlib.loads(target.read_bytes())["Label"] == LAUNCHD_LABEL
    assert (install_env["data_root"] / "logs").is_dir()
    assert install_launchd(**install_env).written  # identical content is accepted again


def test_install_refusals(install_env, tmp_path):
    with pytest.raises(PilotDataError) as tz:
        install_launchd(**{**install_env, "system_timezone": "Europe/London"})
    assert tz.value.code == "launchd_timezone_not_ist"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere\n")
    with pytest.raises(PilotDataError) as wt:
        install_launchd(**{**install_env, "repo_root": worktree})
    assert wt.value.code == "launchd_repo_is_worktree"
    with pytest.raises(PilotDataError) as rel:
        install_launchd(**{**install_env, "data_root": Path("relative/data")})
    assert rel.value.code == "launchd_path_relative"
    with pytest.raises(PilotDataError) as missing:
        install_launchd(**{**install_env, "uv_bin": tmp_path / "nope" / "uv"})
    assert missing.value.code == "launchd_uv_missing"
    install_launchd(**install_env)
    with pytest.raises(PilotDataError) as exists:
        install_launchd(**{**install_env, "minute": 50})
    assert exists.value.code == "launchd_plist_exists"
    assert install_launchd(**{**install_env, "minute": 50}, replace=True).written


def test_dry_run_writes_nothing(install_env):
    outcome = install_launchd(**install_env, dry_run=True)
    assert not outcome.written and "StartCalendarInterval" in outcome.plist
    assert not (install_env["home"] / "Library").exists() and not (install_env["data_root"] / "logs").exists()


def test_cli_install_launchd_dry_run_and_real_write(install_env, capsys):
    args = ["install-launchd", "--repo-root", str(install_env["repo_root"]), "--root", str(install_env["data_root"]),
            "--uv-bin", str(install_env["uv_bin"]), "--home", str(install_env["home"]),
            "--system-timezone", "Asia/Kolkata"]
    assert main([*args, "--dry-run"]) == 0
    assert plistlib.loads(capsys.readouterr().out.encode())["Label"] == LAUNCHD_LABEL
    assert not (install_env["home"] / "Library").exists()
    assert main(args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["commands"][0].startswith("launchctl bootstrap gui/")
    assert main([*args[:-2], "--system-timezone", "UTC"]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "launchd_timezone_not_ist"


def test_the_module_never_starts_a_process_or_calls_launchctl():
    tree = ast.parse(Path(rr.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name not in {"subprocess", "shlex"} for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module not in {"subprocess", "shlex"}
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", getattr(func, "id", ""))
            assert name not in {"system", "popen", "run", "Popen", "check_output", "check_call", "call"} or \
                getattr(getattr(func, "value", None), "id", "") not in {"os", "subprocess"}
            assert "launchctl" not in name

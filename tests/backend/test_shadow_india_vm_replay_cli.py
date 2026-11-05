"""Phase 64-05 Task 2b, Stage 2: the ``vm-replay`` command and its evidence sink.

Real: the evidence store, the replay, the recorded-build loader (a copy of ``gateway/vm`` orders code in a tmp checkout),
the synthetic ``private/`` config on disk and the CLI itself. Faked: nothing that the command is allowed to touch. The
module-wide network sentinel (imported from the replay tests) fails any socket, HTTP or relay call, and the tests here
add the runtime builders and ``OrdersClient`` to it, so a command that built a runtime or a relay link would fail.

Every canary is synthetic. The collar, the capital cap and the account id below are not the pilot's values.
"""

from __future__ import annotations

import ast
import builtins
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from india_limits_support import india_private_dir
from test_shadow_india_vm_replay import (  # noqa: F401 - the sentinel fixture is autouse once it is in this module
    ISIN_A,
    ISIN_B,
    RUN,
    S1,
    S2,
    Scenario,
    identity_of,
    make_vm,
    network_sentinel,
)

import shadow_india.vm_replay as vm_replay
from brokers.breeze_gateway.orders import OrdersClient
from pilot_data.core import canonical_sha256
from private_config.loader import load_workspace_config
from risk_india.state import limit_fields_from_config
from shadow_india import __main__ as cli
from shadow_india import policy
from shadow_india.evidence import EvidenceStore, read_records

REPO = Path(__file__).resolve().parents[2]
MAIN_SOURCE = REPO / "backend" / "shadow_india" / "__main__.py"

CANARY_COLLAR = "0.031417"
CANARY_CAP = "48611.37"
CANARY_ACCOUNT = "CANARYACCT7731"
CANARIES = (CANARY_COLLAR, CANARY_CAP, CANARY_ACCOUNT)
LITERALS = (CANARY_ACCOUNT, "CANARYHOST.example")
DAY1 = "2026-10-27"


@dataclass
class World:
    sc: Scenario
    vm: Path
    private: Path
    tmp: Path

    def argv(self, *extra: str, session: bool = True, private: bool = True) -> list[str]:
        out = ["vm-replay", "--root", str(self.sc.root), "--run-id", RUN, "--vm-source", str(self.vm)]
        out += ["--session", DAY1] if session else ["--all"]
        if private:
            out += ["--private-dir", str(self.private)]
        return out + list(extra)


def build_private(base: Path, name: str, *, collar: str = CANARY_COLLAR, cap: str = CANARY_CAP) -> Path:
    root = base / name
    root.mkdir()
    private = india_private_dir(root, capital_cap=cap, per_position_cap=cap, collar=collar)
    return private


def make_world(tmp_path: Path) -> World:
    vm = make_vm(tmp_path)
    private = build_private(tmp_path, "good")
    config = load_workspace_config(private, "india", require_india_execution=True)
    fields = limit_fields_from_config(config)
    file_sha = canonical_sha256(config.limits.model_dump(mode="json"))
    built = vm_replay.load_recorded_build(vm, identity_of(vm))
    try:
        mint = built.limits_cls.from_fields(fields).sha256  # what the Mac minted with, from the same nine keys
    finally:
        built.close()
    account = tmp_path / "account_ids.txt"  # a file the command has no reason to read
    account.write_text(CANARY_ACCOUNT)
    return World(Scenario(tmp_path, vm, mint, file_sha), vm, private, tmp_path)


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


def run_cli(args: list[str], *, literals: tuple[str, ...] = LITERALS) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = cli.main(args, forbidden_source=lambda _args: literals, out=out)
    lines = [line for line in out.getvalue().splitlines() if line.strip()]
    return code, (json.loads(lines[-1]) if lines else {})


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture()
def no_runtime(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The command builds no runtime, no relay link and no orders client: each of those entry points fails the test."""

    built: list[str] = []

    def trip(name: str) -> Any:
        def blocked(*_a: Any, **_k: Any) -> Any:
            built.append(name)
            raise AssertionError(f"vm-replay built {name}")

        return blocked

    monkeypatch.setattr(cli.runtime_module, "production_runtime", trip("production_runtime"))
    monkeypatch.setattr(cli.runtime_module, "release_runtime", trip("release_runtime"))
    monkeypatch.setattr(OrdersClient, "__init__", trip("OrdersClient"))
    return built


@pytest.fixture()
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every path opened through ``open``, ``io.open``, ``Path.open`` or ``os.open`` while the fixture is active."""

    seen: list[str] = []
    real_builtin, real_io, real_os = builtins.open, io.open, os.open

    def wrap(real: Any) -> Any:
        def spy(file: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(str(file))
            return real(file, *args, **kwargs)

        return spy

    monkeypatch.setattr(builtins, "open", wrap(real_builtin))
    monkeypatch.setattr(io, "open", wrap(real_io))
    monkeypatch.setattr(os, "open", wrap(real_os))
    return seen


def decide_day(w: World) -> None:
    w.sc.intent(S1, "buy", 1, "100.00")
    w.sc.close(S1)


# --- exit codes and what each run writes ----------------------------------------------------------------------------------


def test_a_passing_session_exits_zero_records_one_replay_and_a_rerun_writes_nothing(world: World, no_runtime: list[str]):
    decide_day(world)
    code, out = run_cli(world.argv())
    assert code == 0 and out["verdict"] == "PASS" and out["already_recorded"] is False and out["intents_compared"] == 1
    assert out["mint_limits_sha256"] == world.sc.mint_sha and out["limits_sha256"] == world.sc.file_sha
    records = read_records(world.sc.root, RUN)
    assert [r["kind"] for r in records].count("vm_replay") == 1
    code, again = run_cli(world.argv())
    assert code == 0 and again["already_recorded"] is True
    assert len(read_records(world.sc.root, RUN)) == len(records)  # an equal rerun appends nothing
    assert no_runtime == []


def test_a_divergence_exits_one_records_the_defect_and_pauses_the_run(world: World):
    world.sc.intent(S1, "buy", 5000, "100.00")  # Rs 500,000 against a Rs 48,611 cap, and the Mac admitted it
    world.sc.close(S1)
    code, out = run_cli(world.argv())
    assert code == 1 and out["verdict"] == "FAIL"
    assert out["divergences"] == [{"intent_id": "intent-0001", "reason": "mac_admitted_vm_refused"}]
    records = read_records(world.sc.root, RUN)
    assert [r["code"] for r in records if r["kind"] == "replay_defect"] == [vm_replay.DIVERGENCE]
    state = policy.state_from_records(records)
    assert [(p.event, p.scope) for p in state.open_pauses] == [(vm_replay.DIVERGENCE, "all")]


def test_a_refused_session_exits_one_with_a_closed_code_and_writes_nothing(world: World):
    world.sc.intent(S1, "buy", 1, "100.00")  # never closed
    before = tree(world.sc.root)
    code, out = run_cli(world.argv())
    assert (code, out) == (1, {"refused": vm_replay.SESSION_OPEN})
    assert tree(world.sc.root) == before


def test_an_aborted_run_exits_three_and_writes_nothing(world: World, monkeypatch: pytest.MonkeyPatch):
    decide_day(world)
    world.sc.raw(S1, "abort", {"event": "evidence_corrupt", "reason": "test"})
    before = tree(world.sc.root)
    monkeypatch.setattr(EvidenceStore, "open", lambda *a, **k: pytest.fail("the store was opened for writing on an aborted run"))
    code, out = run_cli(world.argv())
    assert (code, out) == (3, {"aborted": "run_aborted"})
    assert tree(world.sc.root) == before


def test_all_replays_every_decide_day_prints_the_recorded_verdict_and_writes_a_report_only_with_out_dir(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)  # a default report location would be the working directory
    decide_day(world)
    world.sc.intent(S2, "buy", 1, "100.00", isin=ISIN_B)
    world.sc.close(S2)
    code, out = run_cli(world.argv(session=False))
    assert code == 0 and out["verdict"] == "PASS" and out["sessions"] == 2 and out["not_replayed"] == []
    assert not list(tmp_path.rglob(cli.REPLAY_REPORT))  # nothing is written without --out-dir
    assert not list(cwd.iterdir()), "no default report location is written either"
    reports = tmp_path / "reports"
    code, out = run_cli(world.argv("--out-dir", str(reports), session=False))
    assert code == 0
    report = (reports / cli.REPLAY_REPORT).read_text()
    assert vm_replay.D06_LABEL in report and "D-23 verdict: PASS" in report and "—" not in report
    assert world.sc.mint_sha in report and world.sc.file_sha in report
    assert [p.name for p in reports.iterdir()] == [cli.REPLAY_REPORT]


def test_all_with_one_day_refused_and_so_unrecorded_exits_one(world: World):
    decide_day(world)
    world.sc.intent(S2, "buy", 1, "100.00", isin=ISIN_B)  # open: refuses, so it never gets a record
    code, out = run_cli(world.argv(session=False))
    assert code == 1 and out["verdict"] == "FAIL" and out["refused"] == {S2: vm_replay.SESSION_OPEN}
    assert vm_replay.SESSION_REFUSED in out["reasons"]


def test_all_on_a_run_with_no_decide_day_fails_without_opening_the_store(world: World, monkeypatch: pytest.MonkeyPatch):
    world.sc.raw(S1, "run_start", {"run_start_session": DAY1, "audit_base_seq": 0, "audit_base_sha256": "0" * 64, "window_start": DAY1})
    before = tree(world.sc.root)
    monkeypatch.setattr(EvidenceStore, "open", lambda *a, **k: pytest.fail("the store was opened for writing with nothing to replay"))
    code, out = run_cli(world.argv(session=False))
    assert code == 1 and out["verdict"] == "FAIL" and vm_replay.DAY_MISSING in out["reasons"]
    assert out["not_replayed"] == [S1]
    assert tree(world.sc.root) == before


# --- where the limits and the build come from -----------------------------------------------------------------------------


def test_there_is_no_pins_flag_and_a_stray_pins_file_is_never_opened(world: World, tmp_path: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch):
    decide_day(world)
    decoys = [tmp_path / "pins.json", world.sc.root / "pins.json", world.vm / "pins.json", world.private / "pins.json"]
    cwd = tmp_path / "cwd_decoy"
    cwd.mkdir()
    decoys.append(cwd / "pins.json")
    for decoy in decoys:
        decoy.write_text('{"intents": {}}')
    monkeypatch.chdir(cwd)
    mark = len(opened)  # the decoys above were opened by this test, not by the command
    assert run_cli(world.argv("--pins", str(decoys[0])))[0] == cli.EXIT_USAGE  # argparse refuses the unknown flag
    code, out = run_cli(world.argv())
    during = opened[mark:]
    assert code == 0 and out["verdict"] == "PASS"
    assert during and not [p for p in during if "pins" in Path(p).name]


def test_the_limits_come_only_from_the_private_dir_argument(world: World, tmp_path: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch):
    decide_day(world)
    other = build_private(tmp_path, "other", collar="0.0777", cap="12345.67")  # a different private dir on disk
    decoy_cwd = tmp_path / "cwd_decoy"
    (decoy_cwd / "private").mkdir(parents=True)
    monkeypatch.chdir(decoy_cwd)
    monkeypatch.setenv("GROWIN_PRIVATE_DIR", str(other))
    mark = len(opened)  # building the other private dir above is the test's own writing
    code, out = run_cli(world.argv())
    during = opened[mark:]
    assert code == 0 and out["verdict"] == "PASS"
    assert any(p.startswith(str(world.private)) for p in during)
    assert not [p for p in during if p.startswith(str(other)) or str(decoy_cwd) in p]


def test_a_private_dir_that_holds_other_limits_refuses_by_hash_and_writes_nothing(world: World, tmp_path: Path):
    decide_day(world)
    other = build_private(tmp_path, "other", cap="12345.67")  # same collar, another cap: the file hash differs
    before = tree(world.sc.root)
    code, out = run_cli(["vm-replay", "--root", str(world.sc.root), "--run-id", RUN, "--vm-source", str(world.vm),
                         "--session", DAY1, "--private-dir", str(other)])
    assert (code, out) == (1, {"refused": vm_replay.LIMITS_MISMATCH})
    assert tree(world.sc.root) == before
    collar_only = build_private(tmp_path, "collar", collar="0.0777")  # same file, another collar: only the mint hash sees it
    code, out = run_cli(["vm-replay", "--root", str(world.sc.root), "--run-id", RUN, "--vm-source", str(world.vm),
                         "--session", DAY1, "--private-dir", str(collar_only)])
    assert (code, out) == (1, {"refused": vm_replay.LIMITS_MISMATCH})
    assert tree(world.sc.root) == before


def test_no_private_dir_refuses_with_no_default(world: World, monkeypatch: pytest.MonkeyPatch):
    decide_day(world)
    monkeypatch.setenv("GROWIN_PRIVATE_DIR", str(world.private))
    before = tree(world.sc.root)
    code, out = run_cli(world.argv(private=False))
    assert (code, out) == (1, {"refused": "private_dir_required"})
    assert tree(world.sc.root) == before


def test_forbidden_literals_not_registered_refuses_before_anything_is_read_or_opened(world: World, monkeypatch: pytest.MonkeyPatch, opened: list[str]):
    decide_day(world)
    before = tree(world.sc.root)
    monkeypatch.setattr(EvidenceStore, "open", lambda *a, **k: pytest.fail("the store was opened"))
    monkeypatch.setattr(cli, "load_workspace_config", lambda *a, **k: pytest.fail("the private config was read"))
    mark = len(opened)
    code, out = run_cli(world.argv(), literals=())
    during = opened[mark:]
    assert (code, out) == (1, {"refused": "forbidden_literals_empty"})
    assert tree(world.sc.root) == before
    assert not [p for p in during if str(world.private) in p or str(world.sc.root) in p or str(world.vm) in p]


def test_the_command_hands_the_replay_exactly_what_the_shared_nine_key_builder_returned(world: World, monkeypatch: pytest.MonkeyPatch):
    decide_day(world)
    real = cli.limit_fields_from_config
    sentinel: dict[str, Any] = {}
    received: list[Any] = []

    def builder(config: Any) -> Any:
        sentinel.update(real(config))  # equal contents, a distinct object: only the shared builder could have produced it
        return sentinel

    real_replay = vm_replay.replay_session

    def capture(*args: Any, **kwargs: Any) -> Any:
        received.append(kwargs["limits"].fields)
        return real_replay(*args, **kwargs)

    monkeypatch.setattr(cli, "limit_fields_from_config", builder)
    monkeypatch.setattr(vm_replay, "replay_session", capture)
    assert run_cli(world.argv())[0] == 0
    assert len(received) == 1 and received[0] is sentinel  # the replay got the builder's own object, not a rebuilt dict


def test_the_cli_source_holds_no_second_copy_of_the_nine_keys():
    tree_ = ast.parse(MAIN_SOURCE.read_text())
    literals = {n.value for n in ast.walk(tree_) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "fat_finger_collar" not in literals  # the collar key exists only where the shared builder defines it
    for node in ast.walk(tree_):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            assert len(keys & {"capital_cap", "per_position_cap", "drawdown_halt", "drawdown_flatten", "position_stop"}) < 2


# --- what never leaves the command --------------------------------------------------------------------------------------


def test_no_limit_value_or_account_id_reaches_the_evidence_stdout_stderr_logs_or_the_report(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
):
    decide_day(world)
    world.sc.intent(S2, "buy", 5000, "100.00", isin=ISIN_B)  # a divergence, so the defect path runs too
    world.sc.close(S2)
    out = io.StringIO()
    with caplog.at_level("DEBUG"):
        cli.main(world.argv(session=False, *("--out-dir", str(tmp_path / "reports"))), forbidden_source=lambda _a: LITERALS, out=out)
        cli.main(world.argv(), forbidden_source=lambda _a: LITERALS, out=out)
    captured = capsys.readouterr()
    haystacks = {
        "stdout": out.getvalue() + captured.out,
        "stderr": captured.err,
        "logs": caplog.text,
        "report": (tmp_path / "reports" / cli.REPLAY_REPORT).read_text(),
        "evidence": b"".join(tree(world.sc.root).values()).decode("utf-8", "replace"),
    }
    assert "vm_replay" in haystacks["evidence"] and "replay_defect" in haystacks["evidence"]
    for where, text in haystacks.items():
        for canary in CANARIES:
            assert canary not in text, f"{canary} reached {where}"


def test_the_evidence_store_scan_is_live_for_the_replay_records(world: World):
    """A registered literal that a replay record would carry is refused by the store, so the guard is on this path."""

    decide_day(world)
    literal = world.sc.identity.commit  # the build commit is in every vm_replay record
    before = len([r for r in read_records(world.sc.root, RUN) if r["kind"] == "vm_replay"])
    code, out = run_cli(world.argv(), literals=(literal,))
    assert code == 1 and "refused" in out
    assert len([r for r in read_records(world.sc.root, RUN) if r["kind"] == "vm_replay"]) == before


# --- the sink -----------------------------------------------------------------------------------------------------------


def test_the_sink_records_hashes_and_closed_codes_opens_defects_and_pauses_scope_all(world: World):
    decide_day(world)
    store = world.sc.use(S1)
    sink = cli.EvidenceReplaySink(store)
    result = vm_replay.SessionReplay(
        run_id=RUN, session_id=S1, verdict="FAIL", build_digest=world.sc.identity.digest, build_commit=world.sc.identity.commit,
        limits_sha256=world.sc.file_sha, mint_limits_sha256=world.sc.mint_sha, intents_compared=1, no_intents=False,
        divergences=(vm_replay.Divergence("intent-0001", "mac_admitted_vm_refused", "admitted", (), ("capital_cap",)),),
        defects=(vm_replay.KILL_SWITCH_IN_VERDICT,),
    )
    sink.record_session(result)
    sink.open_defect(vm_replay.DIVERGENCE, S1, "mac_admitted_vm_refused")
    sink.pause_run(vm_replay.DIVERGENCE, f"{S1}:intent-0001:mac_admitted_vm_refused")
    records = read_records(world.sc.root, RUN)
    (replay,) = [r for r in records if r["kind"] == "vm_replay"]
    assert replay["divergence_reasons"] == ["mac_admitted_vm_refused"] and replay["divergent_intent_id"] == "intent-0001"
    assert replay["defects"] == [vm_replay.KILL_SWITCH_IN_VERDICT]
    assert "capital_cap" not in json.dumps(replay)  # the VM's codes are not recorded, only the reason
    (defect,) = [r for r in records if r["kind"] == "replay_defect"]
    assert (defect["code"], defect["detail"]) == (vm_replay.DIVERGENCE, "mac_admitted_vm_refused")
    state = policy.state_from_records(records)
    assert [(p.event, p.scope) for p in state.open_pauses] == [(vm_replay.DIVERGENCE, "all")]

"""Phase 64-03 Task 1: the anchored evidence store (P-08, D-04, T-64-13, T-64-15, T-64-32).

Every store lives under ``tmp_path``. Crash probes kill a real child process with SIGKILL at a chosen step of
an append, then reopen the store in this process. Tamper rows mutate the files on disk, and each one must abort
with the sidecar written, every byte kept and every later write refused.
"""

from __future__ import annotations

import errno
import hashlib
import inspect
import itertools
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import shadow_india.evidence as evidence_module
import test_execution_shadow_sim_account as sim
from shadow_india.evidence import (
    ALLOWED_AFTER_ABORT,
    ABORT_MARKER_PREFIX,
    ANCHOR_NAME,
    GENESIS,
    LOG_NAME,
    MANIFEST_NAME,
    RECORD_KINDS,
    SCHEMAS,
    SIDECAR_NAME,
    TORN_DIR,
    EvidenceAborted,
    EvidenceCorrupt,
    EvidenceError,
    EvidenceRefused,
    EvidenceStore,
    WitnessMismatch,
    read_records,
    verify_witness,
)
from test_shadow_india_admission import frozen_service_clock  # noqa: F401 - a fixture, requested by the port wrapper

BACKEND = str(Path(__file__).resolve().parents[2] / "backend")
RUN = "run1"
SESSION = "s-1"
HASHES = {"strategy_sha256": "a" * 64, "params_sha256": "b" * 64}
FIXED = datetime(2026, 10, 22, 4, 5, 6, tzinfo=timezone.utc)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make_store(tmp_path: Path, *, count: int = 0, run_class: str = "plumbing", **kwargs: Any) -> EvidenceStore:
    store = EvidenceStore.create(
        tmp_path, run_id=RUN, run_class=run_class, session_id=SESSION, hashes=HASHES, clock=lambda: FIXED, **kwargs
    )
    fill(store, count)
    return store


def fill(store: EvidenceStore, count: int, start: int = 0) -> None:
    for number in range(start, start + count):
        store.append("non_evaluation_day", {"as_of_date": f"2026-10-{number + 1:02d}", "reason": "cadence"})


def reopen(tmp_path: Path, **kwargs: Any) -> EvidenceStore:
    return EvidenceStore.open(tmp_path, run_id=RUN, session_id=SESSION, clock=lambda: FIXED, **kwargs)


def snapshot(run_dir: Path) -> dict[str, str]:
    return {
        str(p.relative_to(run_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(run_dir.rglob("*"))
        if p.is_file()
    }


def log_lines(run_dir: Path) -> list[bytes]:
    return (run_dir / LOG_NAME).read_bytes().split(b"\n")[:-1]


def write_log(run_dir: Path, lines: list[bytes], tail: bytes = b"") -> None:
    (run_dir / LOG_NAME).write_bytes(b"".join(line + b"\n" for line in lines) + tail)


def write_anchor(run_dir: Path, seq: int, head: str) -> None:
    (run_dir / ANCHOR_NAME).write_bytes(
        json.dumps({"head_sha256": head, "seq": seq}, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )


def entry_hash_at(run_dir: Path, seq: int) -> str:
    return json.loads(log_lines(run_dir)[seq - 1])["entry_sha256"] if seq else GENESIS


# --- layout (P-08) -----------------------------------------------------------------------------------


def test_the_layout_modes_and_manifest_follow_p08(tmp_path):
    store = make_store(tmp_path)
    run_dir = store.run_dir
    assert run_dir == tmp_path.resolve() / RUN
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    for name in (LOG_NAME, ANCHOR_NAME, MANIFEST_NAME, "evidence.lock"):
        assert stat.S_IMODE((run_dir / name).stat().st_mode) == 0o600, name
    manifest = json.loads((run_dir / MANIFEST_NAME).read_text())
    assert manifest["run_id"] == RUN and manifest["run_class"] == "plumbing" and manifest["hashes"] == HASHES
    assert manifest["created_utc"].endswith("+00:00") and manifest["created_ist"].endswith("+05:30")
    assert store.manifest.manifest_sha256 == manifest["manifest_sha256"]
    assert json.loads((run_dir / ANCHOR_NAME).read_text()) == {"head_sha256": GENESIS, "seq": 0}
    assert (run_dir / LOG_NAME).read_bytes() == b""


def test_a_run_cannot_be_created_twice_or_inside_the_repo_or_private(tmp_path):
    make_store(tmp_path)
    before = snapshot(tmp_path / RUN)
    with pytest.raises(EvidenceRefused) as twice:
        make_store(tmp_path)
    assert twice.value.code == "run_exists" and snapshot(tmp_path / RUN) == before
    repo = Path(BACKEND).parent
    for root in (repo / "evidence-test", repo / "private" / "x"):
        with pytest.raises(EvidenceRefused) as unsafe:
            EvidenceStore.create(root, run_id="r2", run_class="plumbing", session_id=SESSION, hashes=HASHES)
        assert unsafe.value.code == "evidence_root_inside_repo_or_private"
    assert not (repo / "evidence-test").exists() and not (repo / "private" / "x").exists()


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"run_id": "../x"}, "bad_run_id"),
        ({"run_id": ".hidden"}, "bad_run_id"),
        ({"run_class": "live"}, "bad_run_class"),
        ({"session_id": "has space"}, "bad_session_id"),
        ({"hashes": {}}, "bad_manifest_hashes"),
        ({"hashes": {"Bad-Name": "a" * 64}}, "bad_manifest_hashes"),
        ({"hashes": {"strategy_sha256": "xyz"}}, "bad_manifest_hashes"),
    ],
)
def test_create_refuses_bad_identity_arguments(tmp_path, kwargs, code):
    arguments: dict[str, Any] = dict(run_id=RUN, run_class="plumbing", session_id=SESSION, hashes=HASHES)
    arguments.update(kwargs)
    with pytest.raises(EvidenceRefused) as caught:
        EvidenceStore.create(tmp_path, **arguments)
    assert caught.value.code == code
    assert not list(tmp_path.iterdir())


def test_a_record_carries_the_envelope_and_the_chain_is_anchored_after_every_append(tmp_path):
    store = make_store(tmp_path)
    run_dir = store.run_dir
    prev = GENESIS
    for number in range(1, 4):
        record = store.append("non_evaluation_day", {"as_of_date": f"2026-10-{number:02d}", "reason": "cadence"})
        assert record["seq"] == number and record["prev_sha256"] == prev
        assert (record["run_id"], record["session_id"], record["run_class"]) == (RUN, SESSION, "plumbing")
        assert record["utc"] == "2026-10-22T04:05:06.000000+00:00"
        assert record["ist"] == "2026-10-22T09:35:06.000000+05:30"
        assert json.loads((run_dir / ANCHOR_NAME).read_text()) == {"head_sha256": record["entry_sha256"], "seq": number}
        prev = record["entry_sha256"]
    assert store.head() == (3, prev)
    assert store.witness_line() == f"witness run={RUN} seq=3 head={prev}"
    assert [r["seq"] for r in store.records()] == [1, 2, 3]
    assert reopen(tmp_path).records() == store.records()


def test_records_are_copies_and_a_naive_clock_is_refused(tmp_path):
    store = make_store(tmp_path, count=1)
    store.records()[0]["kind"] = "tampered"
    assert store.records()[0]["kind"] == "non_evaluation_day"
    naive = EvidenceStore.open(tmp_path, run_id=RUN, session_id=SESSION, clock=lambda: datetime(2026, 10, 22))
    before = snapshot(store.run_dir)
    with pytest.raises(EvidenceRefused) as caught:
        naive.append("vm_ambiguous", {"intent_id": "i-1"})
    assert caught.value.code == "naive_clock" and snapshot(store.run_dir) == before


def test_the_fsync_of_the_line_precedes_the_anchor_replace_and_the_anchor_is_fsynced(tmp_path, monkeypatch):
    store = make_store(tmp_path, count=1)
    log_inode = (store.run_dir / LOG_NAME).stat().st_ino
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        inode = os.fstat(fd).st_ino
        events.append("fsync:log" if inode == log_inode else "fsync:other")
        real_fsync(fd)

    def replace(src: Any, dst: Any) -> None:
        events.append(f"replace:{Path(dst).name}")
        real_replace(src, dst)

    monkeypatch.setattr(evidence_module.os, "fsync", fsync)
    monkeypatch.setattr(evidence_module.os, "replace", replace)
    store.append("vm_ambiguous", {"intent_id": "i-1"})
    assert events.index("fsync:log") < events.index(f"replace:{ANCHOR_NAME}")
    assert events[events.index(f"replace:{ANCHOR_NAME}") - 1] == "fsync:other"  # the anchor temp file
    assert events[-1] == "fsync:other"  # the directory, after the rename


# --- crash probes: a real child process, SIGKILL at a chosen step --------------------------------------

CHILD = r"""
import os, signal, sys
import shadow_india.evidence as ev

root, run_id, stage = sys.argv[1:4]
store = ev.EvidenceStore.open(root, run_id=run_id, session_id="s-1")

def die():
    os.kill(os.getpid(), signal.SIGKILL)

if stage == "mid_write":
    def half(fd, data):
        os.write(fd, data[: len(data) // 2])
        die()
    ev._write_all = half
else:
    def checkpoint(name):
        if name == stage:
            die()
    store._checkpoint = checkpoint
store.append("vm_ambiguous", {"intent_id": "i-crash"})
"""


def crash_append(root: Path, stage: str) -> None:
    done = subprocess.run(
        [sys.executable, "-P", "-c", CHILD, str(root), RUN, stage],
        env={**os.environ, "PYTHONPATH": BACKEND},
        capture_output=True,
        timeout=60,
    )
    assert done.returncode == -signal.SIGKILL, done.stderr.decode()


@pytest.mark.parametrize(
    ("stage", "state"),
    [
        ("before_write", "clean"),
        ("mid_write", "torn"),
        ("after_write", "adopted"),  # killed after the write and before the fsync
        ("after_fsync", "adopted"),  # killed after the fsync and before the anchor
        ("after_anchor", "clean_plus_one"),
    ],
)
def test_a_killed_append_reaches_a_defined_state_and_loses_no_anchored_entry(tmp_path, stage, state):
    store = make_store(tmp_path, count=3)
    run_dir = store.run_dir
    anchored = log_lines(run_dir)
    crash_append(tmp_path, stage)

    # Read-only verification sees the same state and repairs nothing.
    if state in ("clean", "torn"):
        assert [r["seq"] for r in read_records(tmp_path, RUN)] == [1, 2, 3]
    else:
        assert [r["seq"] for r in read_records(tmp_path, RUN)] == [1, 2, 3, 4]
    if state == "adopted":
        assert json.loads((run_dir / ANCHOR_NAME).read_text())["seq"] == 3  # not yet adopted

    reopened = reopen(tmp_path)
    records = reopened.records()
    assert log_lines(run_dir)[:3] == anchored  # no anchored entry lost or rewritten
    if state == "clean":
        assert len(records) == 3 and not (run_dir / TORN_DIR).exists()
    elif state == "torn":
        assert len(records) == 3
        torn = [p.read_bytes() for p in (run_dir / TORN_DIR).iterdir()]
        assert len(torn) == 1 and torn[0].startswith(b"{") and not torn[0].endswith(b"\n")
        assert (run_dir / LOG_NAME).read_bytes() == b"".join(line + b"\n" for line in anchored)
    else:
        assert [r["seq"] for r in records] == [1, 2, 3, 4] and records[3]["intent_id"] == "i-crash"
        assert not (run_dir / TORN_DIR).exists()
    seq, head = reopened.head()
    assert json.loads((run_dir / ANCHOR_NAME).read_text()) == {"head_sha256": head, "seq": seq}

    nxt = reopened.append("vm_ambiguous", {"intent_id": "i-after"})  # appends continue on a verified chain
    assert nxt["seq"] == seq + 1 and read_records(tmp_path, RUN)[-1]["intent_id"] == "i-after"
    assert not (run_dir / SIDECAR_NAME).exists()


def test_a_killed_torn_append_survives_two_restarts_with_one_set_aside_copy(tmp_path):
    make_store(tmp_path, count=2)
    crash_append(tmp_path, "mid_write")
    first = reopen(tmp_path)
    second = reopen(tmp_path)
    assert len(first.records()) == len(second.records()) == 2
    assert len(list((first.run_dir / TORN_DIR).iterdir())) == 1


# --- durability order: nothing is anchored or cut before the bytes it relies on are on disk ---------------

CHILD_RECOVER = r"""
import os, signal, sys
import shadow_india.evidence as ev

root, run_id, where = sys.argv[1:4]
real = ev._replace_file

def die():
    os.kill(os.getpid(), signal.SIGKILL)

def replace(path, data):
    if where == "before_anchor":
        die()
    real(path, data)
    die()

ev._replace_file = replace
ev.EvidenceStore.open(root, run_id=run_id, session_id="s-1")  # recovery adopts the surviving entry
"""


def crash_recovery(root: Path, where: str) -> None:
    done = subprocess.run(
        [sys.executable, "-P", "-c", CHILD_RECOVER, str(root), RUN, where],
        env={**os.environ, "PYTHONPATH": BACKEND},
        capture_output=True,
        timeout=60,
    )
    assert done.returncode == -signal.SIGKILL, done.stderr.decode()


class DiskProbe:
    """Records every fsync, anchor replace and truncate by what it touches, plus the durability invariant.

    A new process cannot know what an earlier one synced, so ``synced`` starts at zero: an anchor replace is a
    violation unless this process fsynced the log up to the end of the entry the anchor names first.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.events: list[str] = []
        self.violations: list[str] = []
        self.synced = 0
        real_fsync, real_replace, real_truncate = os.fsync, os.replace, os.ftruncate

        def fsync(fd: int) -> None:
            label = self.label(fd)
            if label == "log":
                self.synced = os.fstat(fd).st_size
            self.events.append(f"fsync:{label}")
            real_fsync(fd)

        def replace(src: Any, dst: Any) -> None:
            if Path(dst).name == ANCHOR_NAME:
                seq = json.loads(Path(src).read_text())["seq"]
                durable_need = sum(len(line) + 1 for line in log_lines(run_dir)[:seq])
                if self.synced < durable_need:
                    self.violations.append(f"anchor {seq} replaced with the log synced to {self.synced} of {durable_need}")
            self.events.append(f"replace:{Path(dst).name}")
            real_replace(src, dst)

        def ftruncate(fd: int, length: int) -> None:
            self.events.append(f"truncate:{self.label(fd)}")
            real_truncate(fd, length)

        monkeypatch.setattr(evidence_module.os, "fsync", fsync)
        monkeypatch.setattr(evidence_module.os, "replace", replace)
        monkeypatch.setattr(evidence_module.os, "ftruncate", ftruncate)

    def label(self, fd: int) -> str:
        ino = os.fstat(fd).st_ino
        named = {self.run_dir: "rundir", self.run_dir / TORN_DIR: "torn", self.run_dir / LOG_NAME: "log"}
        torn = self.run_dir / TORN_DIR
        if torn.is_dir():
            named.update({p: "archive" for p in torn.iterdir()})
        for path, name in named.items():
            if path.exists() and path.stat().st_ino == ino:
                return name
        return "other"


def test_a_normal_append_keeps_the_anchor_behind_the_synced_log(tmp_path, monkeypatch):
    store = make_store(tmp_path, count=2)
    probe = DiskProbe(monkeypatch, store.run_dir)
    store.append("vm_ambiguous", {"intent_id": "i-1"})
    assert probe.violations == [] and probe.events.index("fsync:log") < probe.events.index(f"replace:{ANCHOR_NAME}")


@pytest.mark.parametrize("stage", ["after_write", "after_fsync"])
def test_recovery_fsyncs_the_log_before_it_anchors_the_surviving_entry(tmp_path, monkeypatch, stage):
    make_store(tmp_path, count=3)
    crash_append(tmp_path, stage)  # one complete entry beyond the anchor; this process cannot know it was synced
    probe = DiskProbe(monkeypatch, tmp_path.resolve() / RUN)
    assert [r["seq"] for r in reopen(tmp_path).records()] == [1, 2, 3, 4]
    assert probe.violations == []
    assert probe.events.index("fsync:log") < probe.events.index(f"replace:{ANCHOR_NAME}")


@pytest.mark.parametrize("where", ["before_anchor", "after_anchor"])
def test_a_crash_inside_recovery_loses_no_entry_and_the_next_recovery_finishes(tmp_path, where):
    store = make_store(tmp_path, count=3)
    run_dir = store.run_dir
    anchored = log_lines(run_dir)
    crash_append(tmp_path, "after_write")
    crash_recovery(tmp_path, where)  # killed while adopting, or just after
    assert log_lines(run_dir)[:3] == anchored
    reopened = reopen(tmp_path)
    assert [r["seq"] for r in reopened.records()] == [1, 2, 3, 4]
    assert json.loads((run_dir / ANCHOR_NAME).read_text())["seq"] == 4
    assert log_lines(run_dir)[:3] == anchored and not (run_dir / SIDECAR_NAME).exists()
    assert reopened.append("vm_ambiguous", {"intent_id": "i-after"})["seq"] == 5


def test_the_torn_tail_is_archived_and_synced_before_the_log_is_truncated(tmp_path, monkeypatch):
    make_store(tmp_path, count=3)
    crash_append(tmp_path, "mid_write")
    probe = DiskProbe(monkeypatch, tmp_path.resolve() / RUN)
    assert len(reopen(tmp_path).records()) == 3
    cut = probe.events.index("truncate:log")
    for needed in ("fsync:archive", "fsync:torn", "fsync:rundir"):
        assert needed in probe.events[:cut], (needed, probe.events)


def test_a_torn_tail_already_archived_by_an_earlier_attempt_is_synced_again_before_the_cut(tmp_path, monkeypatch):
    make_store(tmp_path, count=3)
    crash_append(tmp_path, "mid_write")
    run_dir = tmp_path.resolve() / RUN
    reopen(tmp_path)  # first recovery: archives and cuts
    (archived,) = list((run_dir / TORN_DIR).iterdir())
    tail = archived.read_bytes()
    (run_dir / LOG_NAME).write_bytes((run_dir / LOG_NAME).read_bytes() + tail)  # the same tail shows up again
    probe = DiskProbe(monkeypatch, run_dir)
    assert len(reopen(tmp_path).records()) == 3
    assert len(list((run_dir / TORN_DIR).iterdir())) == 1  # no second copy was written
    cut = probe.events.index("truncate:log")
    for needed in ("fsync:archive", "fsync:torn", "fsync:rundir"):
        assert needed in probe.events[:cut], (needed, probe.events)


# --- tamper rows: every one aborts, writes the sidecar, keeps every byte and refuses appends -----------


def drop_last(count: int):
    def mutate(run_dir: Path) -> None:
        write_log(run_dir, log_lines(run_dir)[:-count])

    return mutate


def delete(*names: str):
    def mutate(run_dir: Path) -> None:
        for name in names:
            (run_dir / name).unlink()

    return mutate


def swap_two_lines(run_dir: Path) -> None:
    lines = log_lines(run_dir)
    lines[1], lines[2] = lines[2], lines[1]
    write_log(run_dir, lines)


def cut_a_middle_line(run_dir: Path) -> None:
    lines = log_lines(run_dir)
    lines[2] = lines[2][: len(lines[2]) // 2]  # the newline stays, so the torn line is not at the tail
    write_log(run_dir, lines)


def duplicate_a_line(run_dir: Path) -> None:
    lines = log_lines(run_dir)
    write_log(run_dir, lines[:3] + [lines[2]] + lines[3:])


def unterminated_tail_after_a_pending_entry(run_dir: Path) -> None:
    write_anchor(run_dir, 5, entry_hash_at(run_dir, 5))  # the sixth entry is the single allowed pending one
    (run_dir / LOG_NAME).write_bytes((run_dir / LOG_NAME).read_bytes() + b'{"seq":7')


def anchor_two_behind(run_dir: Path) -> None:
    write_anchor(run_dir, 4, entry_hash_at(run_dir, 4))


def anchor_head_of_another_chain(run_dir: Path) -> None:
    write_anchor(run_dir, 6, sha("another chain"))


def anchor_garbage(run_dir: Path) -> None:
    (run_dir / ANCHOR_NAME).write_bytes(b"not json\n")


def edit_manifest(run_dir: Path) -> None:
    path = run_dir / MANIFEST_NAME
    path.write_bytes(path.read_bytes().replace(b"plumbing", b"qualifying"))


def rewrite_entry(index: int):
    """Edit one entry and recompute its own hash, so only the chain link (or the anchor) can see it."""

    def mutate(run_dir: Path) -> None:
        lines = log_lines(run_dir)
        entry = json.loads(lines[index])
        entry["reason"] = "rewritten"
        entry["entry_sha256"] = hashlib.sha256(
            json.dumps({k: v for k, v in entry.items() if k != "entry_sha256"}, sort_keys=True, separators=(",", ":"))
            .encode("ascii")
        ).hexdigest()
        lines[index] = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("ascii")
        write_log(run_dir, lines)

    return mutate


def flip_final_newline(run_dir: Path) -> None:
    raw = (run_dir / LOG_NAME).read_bytes()
    (run_dir / LOG_NAME).write_bytes(raw[:-1] + b"x")


def log_and_anchor_from_another_run(run_dir: Path) -> None:
    """A valid chain and anchor, internally consistent, but recorded under another run id."""

    foreign = EvidenceStore.create(
        run_dir.parent / "foreign", run_id="run2", run_class="plumbing", session_id=SESSION, hashes=HASHES,
        clock=lambda: FIXED,
    )
    fill(foreign, 6)
    shutil.copyfile(foreign.run_dir / LOG_NAME, run_dir / LOG_NAME)
    shutil.copyfile(foreign.run_dir / ANCHOR_NAME, run_dir / ANCHOR_NAME)


TAMPER_ROWS = [
    ("drop_last_1_valid_prefix", drop_last(1), "log_shorter_than_anchor"),
    ("drop_last_2_valid_prefix", drop_last(2), "log_shorter_than_anchor"),
    ("drop_last_5_valid_prefix", drop_last(5), "log_shorter_than_anchor"),
    ("empty_the_log", drop_last(6), "log_shorter_than_anchor"),
    ("delete_the_log", delete(LOG_NAME), "log_missing"),
    ("delete_the_anchor", delete(ANCHOR_NAME), "anchor_missing"),
    ("delete_log_and_anchor", delete(LOG_NAME, ANCHOR_NAME), "anchor_missing"),
    ("reorder_two_lines", swap_two_lines, "chain_break"),
    ("duplicate_a_line", duplicate_a_line, "chain_break"),
    ("middle_entry_rewritten_with_its_hash_recomputed", rewrite_entry(2), "chain_break"),
    ("first_entry_rewritten_with_its_hash_recomputed", rewrite_entry(0), "chain_break"),
    ("last_entry_rewritten_with_its_hash_recomputed", rewrite_entry(5), "head_mismatch"),
    ("torn_line_not_at_the_tail", cut_a_middle_line, "line_invalid"),
    ("unterminated_tail_after_a_pending_entry", unterminated_tail_after_a_pending_entry, "torn_tail_not_after_anchor"),
    ("final_newline_flipped", flip_final_newline, "log_shorter_than_anchor"),
    ("log_two_entries_ahead_of_the_anchor", anchor_two_behind, "ahead_of_anchor"),
    ("anchor_head_of_another_chain", anchor_head_of_another_chain, "head_mismatch"),
    ("anchor_unreadable", anchor_garbage, "anchor_invalid"),
    ("manifest_edited", edit_manifest, "manifest_hash_mismatch"),
    ("log_and_anchor_copied_from_another_run", log_and_anchor_from_another_run, "run_mismatch"),
]


@pytest.mark.parametrize(("name", "mutate", "code"), TAMPER_ROWS, ids=[row[0] for row in TAMPER_ROWS])
def test_a_tampered_store_aborts_with_the_sidecar_and_keeps_every_byte(tmp_path, name, mutate, code):
    live = make_store(tmp_path, count=6)
    run_dir = live.run_dir
    mutate(run_dir)
    mutated = snapshot(run_dir)

    with pytest.raises(EvidenceCorrupt) as opened:
        reopen(tmp_path)
    assert opened.value.code == code
    assert (run_dir / SIDECAR_NAME).exists() and stat.S_IMODE((run_dir / SIDECAR_NAME).stat().st_mode) == 0o600
    assert json.loads((run_dir / SIDECAR_NAME).read_text())["code"] == code

    # An already-open store refuses its next append too, and nothing is created, cut or rewritten.
    with pytest.raises(EvidenceCorrupt):
        live.append("vm_ambiguous", {"intent_id": "i-1"})
    with pytest.raises(EvidenceCorrupt):
        live.records()
    with pytest.raises(EvidenceCorrupt) as again:  # the sidecar makes every later open refuse as well
        reopen(tmp_path)
    assert again.value.code == code
    after = snapshot(run_dir)
    assert {k: v for k, v in after.items() if k != SIDECAR_NAME} == mutated, name
    assert not (run_dir / TORN_DIR).exists()


def test_a_flipped_byte_anywhere_in_a_middle_line_is_detected(tmp_path):
    base = make_store(tmp_path / "base", count=6)
    line_start = sum(len(line) + 1 for line in log_lines(base.run_dir)[:2])
    line_len = len(log_lines(base.run_dir)[2])
    undetected = []
    for offset in range(line_len + 1):  # the line and its newline
        trial_root = tmp_path / f"t{offset}"
        shutil.copytree(tmp_path / "base", trial_root)
        run_dir = trial_root / RUN
        raw = bytearray((run_dir / LOG_NAME).read_bytes())
        raw[line_start + offset] ^= 0x01
        (run_dir / LOG_NAME).write_bytes(bytes(raw))
        mutated = snapshot(run_dir)
        try:
            EvidenceStore.open(trial_root, run_id=RUN, session_id=SESSION)
            undetected.append(offset)
        except EvidenceCorrupt:
            assert {k: v for k, v in snapshot(run_dir).items() if k != SIDECAR_NAME} == mutated
    assert undetected == []


def test_a_missing_manifest_or_loosened_permissions_refuse_without_a_sidecar_and_keep_bytes(tmp_path):
    store = make_store(tmp_path, count=2)
    run_dir = store.run_dir
    os.chmod(run_dir / LOG_NAME, 0o644)
    before = snapshot(run_dir)
    with pytest.raises(EvidenceRefused) as loose:
        reopen(tmp_path)
    assert loose.value.code == "permissions_too_open" and snapshot(run_dir) == before
    assert stat.S_IMODE((run_dir / LOG_NAME).stat().st_mode) == 0o644  # reported, never repaired
    os.chmod(run_dir / LOG_NAME, 0o600)
    (run_dir / MANIFEST_NAME).unlink()
    with pytest.raises(EvidenceRefused) as incomplete:
        reopen(tmp_path)
    assert incomplete.value.code == "store_incomplete" and not (run_dir / SIDECAR_NAME).exists()
    with pytest.raises(EvidenceRefused) as missing:
        EvidenceStore.open(tmp_path, run_id="nope", session_id=SESSION)
    assert missing.value.code == "store_missing"


# --- external witness (T-64-32) ------------------------------------------------------------------------


def test_the_witness_accepts_the_real_chain_and_needs_at_least_one_head(tmp_path):
    store = make_store(tmp_path, count=4)
    early = store.witness_line()
    fill(store, 2, start=4)
    text = f"notes\n{early}\nmore\n{store.witness_line()}\nwitness run=other seq=99 head={'c' * 64}\n"
    assert verify_witness(tmp_path, RUN, text) == [(4, early.split("head=")[1]), (6, store.head()[1])]
    with pytest.raises(WitnessMismatch) as none:
        verify_witness(tmp_path, RUN, "no heads pasted here")
    assert none.value.code == "witness_missing"


def test_log_tail_and_anchor_rolled_back_together_pass_open_but_fail_the_witness(tmp_path):
    store = make_store(tmp_path, count=6)
    run_dir = store.run_dir
    pasted = store.witness_line()
    write_log(run_dir, log_lines(run_dir)[:3])
    write_anchor(run_dir, 3, entry_hash_at(run_dir, 3))
    assert len(reopen(tmp_path).records()) == 3  # chain and anchor agree: only the witness can see it
    assert not (run_dir / SIDECAR_NAME).exists()
    mutated = snapshot(run_dir)
    with pytest.raises(WitnessMismatch) as caught:
        verify_witness(tmp_path, RUN, pasted)
    assert caught.value.code == "witness_head_not_in_chain"

    # The mismatch is terminal: the sidecar is written and every later write is refused, every byte kept.
    assert (run_dir / SIDECAR_NAME).exists() and stat.S_IMODE((run_dir / SIDECAR_NAME).stat().st_mode) == 0o600
    assert json.loads((run_dir / SIDECAR_NAME).read_text())["code"] == "witness_head_not_in_chain"
    with pytest.raises(EvidenceCorrupt) as refused:
        store.append("vm_ambiguous", {"intent_id": "i-1"})
    assert refused.value.code == "witness_head_not_in_chain"
    with pytest.raises(EvidenceCorrupt):
        reopen(tmp_path).append("vm_ambiguous", {"intent_id": "i-2"})
    with pytest.raises(EvidenceCorrupt):
        store.records()
    assert {k: v for k, v in snapshot(run_dir).items() if k != SIDECAR_NAME} == mutated


def test_a_witness_that_matches_or_is_missing_writes_no_sidecar(tmp_path):
    store = make_store(tmp_path, count=3)
    verify_witness(tmp_path, RUN, store.witness_line())
    with pytest.raises(WitnessMismatch):
        verify_witness(tmp_path, RUN, "nothing pasted")
    assert not (store.run_dir / SIDECAR_NAME).exists()
    assert store.append("vm_ambiguous", {"intent_id": "i-1"})["seq"] == 4


def test_deleting_the_log_and_the_anchor_fails_the_witness_check(tmp_path):
    store = make_store(tmp_path, count=5)
    pasted = store.witness_line()
    (store.run_dir / LOG_NAME).unlink()
    (store.run_dir / ANCHOR_NAME).unlink()
    with pytest.raises(EvidenceError):
        verify_witness(tmp_path, RUN, pasted)


def test_a_wiped_and_recreated_run_fails_the_witness_check(tmp_path):
    store = make_store(tmp_path, count=5)
    pasted = store.witness_line()
    shutil.rmtree(store.run_dir)
    make_store(tmp_path, count=2)  # a clean-looking store under the same run id
    with pytest.raises(WitnessMismatch) as caught:
        verify_witness(tmp_path, RUN, pasted)
    assert caught.value.code == "witness_head_not_in_chain"


# --- an abort that cannot be written still latches (codex-r2-1) ----------------------------------------

FRESH_OPEN = r"""
import sys
import shadow_india.evidence as ev

root, run_id = sys.argv[1:3]
try:
    store = ev.EvidenceStore.open(root, run_id=run_id, session_id="s-1")
    store.append("vm_ambiguous", {"intent_id": "i-fresh"})
except ev.EvidenceError as exc:
    print("REFUSED", exc.code)
else:
    print("ACCEPTED")
"""


def fresh_process_open_and_append(root: Path) -> str:
    done = subprocess.run(
        [sys.executable, "-P", "-c", FRESH_OPEN, str(root), RUN],
        env={**os.environ, "PYTHONPATH": BACKEND},
        capture_output=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode()
    return done.stdout.decode().strip()


def fail_the_sidecar_write(monkeypatch: pytest.MonkeyPatch) -> None:
    real = evidence_module._write_new_file

    def write(path: Path, data: bytes) -> None:
        if Path(path).name == SIDECAR_NAME:
            raise OSError(errno.ENOSPC, "No space left on device")
        real(path, data)

    monkeypatch.setattr(evidence_module, "_write_new_file", write)


def rolled_back_store_and_pasted_witness(tmp_path: Path) -> tuple[EvidenceStore, str]:
    store = make_store(tmp_path, count=6)
    pasted = store.witness_line()
    write_log(store.run_dir, log_lines(store.run_dir)[:3])
    write_anchor(store.run_dir, 3, entry_hash_at(store.run_dir, 3))
    return store, pasted


def test_a_witness_mismatch_latches_even_when_the_sidecar_write_fails(tmp_path, monkeypatch):
    store, pasted = rolled_back_store_and_pasted_witness(tmp_path)
    run_dir = store.run_dir
    mutated = snapshot(run_dir)
    fail_the_sidecar_write(monkeypatch)

    with pytest.raises(EvidenceError):  # the abort is surfaced, not swallowed
        verify_witness(tmp_path, RUN, pasted)
    assert not (run_dir / SIDECAR_NAME).exists()
    # The live store and a second store in this process both refuse, with the abort code.
    with pytest.raises(EvidenceCorrupt) as live:
        store.append("vm_ambiguous", {"intent_id": "i-1"})
    assert live.value.code == "witness_head_not_in_chain"
    with pytest.raises(EvidenceCorrupt):
        reopen(tmp_path)
    with pytest.raises(EvidenceCorrupt):
        read_records(tmp_path, RUN)
    # A fresh process refuses too: the fallback marker is durable and carries the code.
    marker = run_dir / (ABORT_MARKER_PREFIX + "witness_head_not_in_chain")
    assert marker.exists() and not (run_dir / ANCHOR_NAME).exists()
    assert fresh_process_open_and_append(tmp_path) == "REFUSED witness_head_not_in_chain"
    # Every byte is kept: the anchor only changed its name.
    assert snapshot(run_dir)[marker.name] == mutated[ANCHOR_NAME]
    assert {k: v for k, v in snapshot(run_dir).items() if k != marker.name} == {
        k: v for k, v in mutated.items() if k != ANCHOR_NAME
    }


def test_an_abort_that_cannot_be_recorded_anywhere_is_reported_and_still_latches(tmp_path, monkeypatch):
    store, pasted = rolled_back_store_and_pasted_witness(tmp_path)
    run_dir = store.run_dir
    mutated = snapshot(run_dir)
    fail_the_sidecar_write(monkeypatch)

    def no_rename(src: Any, dst: Any) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(evidence_module.os, "rename", no_rename)
    with pytest.raises(EvidenceRefused) as caught:
        verify_witness(tmp_path, RUN, pasted)
    assert caught.value.code == "abort_not_recorded"
    with pytest.raises(EvidenceCorrupt) as live:
        store.append("vm_ambiguous", {"intent_id": "i-1"})
    assert live.value.code == "witness_head_not_in_chain"
    assert snapshot(run_dir) == mutated  # nothing created, cut or rewritten


@pytest.mark.parametrize("name", ["delete_the_log", "log_and_anchor_copied_from_another_run"])
def test_a_corruption_found_by_an_open_store_latches_when_the_sidecar_write_fails(tmp_path, monkeypatch, name):
    store = make_store(tmp_path, count=6)
    {row[0]: row[1] for row in TAMPER_ROWS}[name](store.run_dir)
    mutated = snapshot(store.run_dir)
    fail_the_sidecar_write(monkeypatch)
    with pytest.raises(EvidenceCorrupt):
        store.append("vm_ambiguous", {"intent_id": "i-1"})
    monkeypatch.undo()  # the disk recovers; the latch and the marker still hold
    with pytest.raises(EvidenceCorrupt):
        store.append("vm_ambiguous", {"intent_id": "i-2"})
    assert fresh_process_open_and_append(tmp_path).startswith("REFUSED ")
    assert {k: v for k, v in snapshot(store.run_dir).items() if not k.startswith(ABORT_MARKER_PREFIX)} == {
        k: v for k, v in mutated.items() if k != ANCHOR_NAME
    }


# --- record schemas (D-04) -----------------------------------------------------------------------------

INTENT = {"intent_id": "a" * 64}
GOOD: dict[str, dict[str, Any]] = {
    "signal": {
        "signal_id": "sig-1", "symbol": "RELIANCE", "isin": "INE002A01018", "series": "EQ", "as_of_date": "2026-10-21",
        **{name: sha(name) for name in SCHEMAS["signal"][0] if name.endswith("_sha256")},
    },
    "non_evaluation_day": {"as_of_date": "2026-10-21", "reason": "cadence"},
    "intent": {
        **INTENT, "signal_id": "sig-1", "leg": "entry", "intent_sha256": sha("i"), "symbol": "RELIANCE", "side": "BUY",
        "quantity": "2", "limit_price": "100.00",
    },
    "admission": {**INTENT, "decision": "ADMITTED", "reason_code": "OK"},
    "denied": {**INTENT, "code": "ADMISSION_EVIDENCE_IS_STALE"},
    "reservation_refused": {**INTENT, "code": "IndiaCapExceeded"},
    "reservation": {**INTENT, "reservation_created_at": "2026-10-22T04:05:06+00:00"},
    "mint_started": {**INTENT, "at": "2026-10-22T04:05:06+00:00"},
    "vm_decision": {
        **INTENT, "audit_seq": 5, "entry_hash": sha("e"), "intent_sha256": sha("i"), "codes": ["kill_switch"],
        "recovered_from_audit": False,
    },
    "vm_ambiguous": INTENT,
    "vm_late_entry": {**INTENT, "audit_seq": 5, "entry_hash": sha("e")},
    "prep_abandoned": INTENT,
    "attempt": {**INTENT, "attempt_id": "att-1", "outcome": "INCONCLUSIVE"},
    "attempt_inconclusive": {**INTENT, "code": "mint_ambiguous"},
    "session_excluded": {"code": "mint_ambiguous", "excluded_from_qualification": True},
    "booking_intent": {
        **INTENT, "booking_id": sha("b"), "cumulative_quantity": "2", "price": "100.00", "charges": "1.25",
        "schedule_sha256": sha("s"), "scenario_set_sha256": sha("c"), "attempt_id": "att-1",
    },
    "booking_done": {**INTENT, "booking_id": sha("b"), "ledger_event_id": 7},
    "booking_refused": {**INTENT, "booking_id": sha("b"), "code": "InvalidTransition"},
    "fill_result": {**INTENT, "attempt_id": "att-1", "outcome": "INCONCLUSIVE", "reason": "no_post_submission_evidence"},
    "expiry": {**INTENT, "reason": "day_close"},
    "mark": {
        "as_of_date": "2026-10-21", "equity": "49990.50", "peak": "50000", "drawdown": "-0.0002", "cash": "49000",
        "held_capital": "990.50", "exposure": "0.02",
    },
    "position_mark": {
        "as_of_date": "2026-10-21", "isin": "INE002A01018", "symbol": "RELIANCE", "quantity": "10", "close": "100.50",
        "cost": "1000", "mark_basis": "raw_close",
    },
    "evaluation_inputs": {"as_of_date": "2026-10-21", "position_mark_seqs": [4, 5]},
    "evaluation_commit": {"as_of_date": "2026-10-21", "position_mark_seqs": [4, 5], "equity": "49990.50"},
    "stop_open_report": {"as_of_date": "2026-10-21", "isins": ["INE002A01018"], "buys_blocked": True},
    "run_ended": {"event": "drawdown_flatten_15", "reason": "equity_at_or_below_minus_15_percent_of_peak"},
    "latch": {"latch": "stop_open", "scope": "all", "reason": "stop_exit_open"},
    "pause": {"event": "stale_data", "scope": "all", "reason": "bars older than one session"},
    "abort": {"event": "audit_corrupt", "reason": "chain break"},
    "resume": {"reason": "operator reviewed the bars", "cleared_pause_seqs": [3]},
    "session_close": {"outcome": "ok", "record_count": 12},
}


def test_every_plan_record_type_and_every_adapter_record_type_has_a_schema():
    plan = {
        "signal", "non_evaluation_day", "intent", "admission", "reservation", "mint_started", "vm_decision",
        "vm_ambiguous", "vm_late_entry", "prep_abandoned", "attempt", "booking_intent", "booking_done", "fill_result",
        "expiry", "mark", "latch", "pause", "abort", "resume", "session_close",
    }
    from shadow_india import admission

    written = {getattr(admission, name) for name in dir(admission) if name.startswith("E_")}
    assert plan <= RECORD_KINDS and written <= RECORD_KINDS
    assert set(GOOD) == RECORD_KINDS


@pytest.mark.parametrize("kind", sorted(GOOD))
def test_each_record_type_stores_and_a_missing_required_field_is_refused(tmp_path, kind):
    store = make_store(tmp_path)
    stored = store.append(kind, GOOD[kind])
    assert stored["kind"] == kind and all(stored[k] == v for k, v in GOOD[kind].items())
    run_dir = store.run_dir
    before = snapshot(run_dir)
    for name in SCHEMAS[kind][0]:
        partial = {k: v for k, v in GOOD[kind].items() if k != name}
        with pytest.raises(EvidenceRefused) as caught:
            store.append(kind, partial)
        assert caught.value.code == "missing_field", (kind, name)
    assert snapshot(run_dir) == before


@pytest.mark.parametrize(
    ("kind", "change", "code"),
    [
        ("intent", {"extra": "x"}, "unknown_field"),
        ("intent", {"seq": 9}, "reserved_field"),
        ("intent", {"run_class": "qualifying"}, "reserved_field"),
        ("intent", {"utc": "2020-01-01T00:00:00+00:00"}, "reserved_field"),
        ("intent", {"kind": "mark"}, "reserved_field"),
        ("intent", {"quantity": 2}, "bad_field"),
        ("intent", {"quantity": Decimal("2")}, "bad_field"),
        ("intent", {"quantity": "NaN"}, "bad_field"),
        ("intent", {"quantity": "1e5"}, "bad_field"),
        ("intent", {"limit_price": 100.5}, "float_refused"),
        ("intent", {"intent_sha256": "short"}, "bad_field"),
        ("intent", {"session_id": "has space"}, "bad_field"),
        ("vm_decision", {"audit_seq": True}, "bad_field"),
        ("vm_decision", {"audit_seq": -1}, "bad_field"),
        ("vm_decision", {"codes": "kill_switch"}, "bad_field"),
        ("vm_decision", {"recovered_from_audit": "no"}, "bad_field"),
        ("mint_started", {"at": "yesterday"}, "bad_field"),
        ("mint_started", {"at": "2026-10-22T04:05:06"}, "bad_field"),
        ("mark", {"as_of_date": "2026-13-45"}, "bad_field"),
        ("pause", {"reason": "   "}, "bad_field"),
        ("pause", {"reason": "stale bars\n"}, "bad_field"),
        ("pause", {"reason": "stale bars\r\n"}, "bad_field"),
        ("pause", {"reason": "stale\nbars"}, "bad_field"),
        ("pause", {"reason": "\nstale bars"}, "bad_field"),
        ("resume", {"reason": "reviewed\n"}, "bad_field"),
        ("evaluation_commit", {"position_mark_seqs": "4"}, "bad_field"),
        ("evaluation_commit", {"position_mark_seqs": [4, -1]}, "bad_field"),
        ("position_mark", {"carried_value": "NaN"}, "bad_field"),
        ("signal", {"isin": "not-an-isin"}, "bad_field"),
        ("signal", {"isin": "INE002A01018\n"}, "bad_field"),
        ("pause", {"scope": "isin:INE002A01018\n"}, "bad_field"),
        ("pause", {"event": "stale_data\n"}, "bad_field"),
        ("pause", {"scope": "all\n"}, "bad_field"),
        ("denied", {"code": "kill_switch\r\n"}, "bad_field"),
        ("intent", {"session_id": "abc\n"}, "bad_field"),
        ("mark", {"as_of_date": "2026-10-22\n"}, "bad_field"),
        ("intent", {"intent_sha256": "a" * 64 + "\n"}, "bad_field"),
    ],
)
def test_a_malformed_record_is_refused_before_anything_is_written(tmp_path, kind, change, code):
    store = make_store(tmp_path, count=1)
    before = snapshot(store.run_dir)
    with pytest.raises(EvidenceRefused) as caught:
        store.append(kind, {**GOOD[kind], **change})
    assert caught.value.code == code
    assert snapshot(store.run_dir) == before


def test_an_unknown_kind_and_a_non_mapping_payload_are_refused(tmp_path):
    store = make_store(tmp_path)
    for kind, payload, code in (("nope", {}, "unknown_kind"), ("intent", ["x"], "payload_not_a_mapping")):
        with pytest.raises(EvidenceRefused) as caught:
            store.append(kind, payload)  # type: ignore[arg-type]
        assert caught.value.code == code


def test_a_record_about_another_session_names_that_session(tmp_path):
    store = make_store(tmp_path)
    record = store.append("session_excluded", {"session_id": "s-earlier", **GOOD["session_excluded"]})
    assert record["session_id"] == "s-earlier"
    assert store.append("vm_ambiguous", INTENT)["session_id"] == SESSION


# --- canary rows (T-64-15) -----------------------------------------------------------------------------

CANARY_TOKEN = "CANARY-TOKEN-8f3a91c2d7"
CANARY_ACCOUNT = "CANARY-ACCT-4471920"
CANARY_URL = "https://api.example-broker.test/apiuser/login?api_key=CANARY-KEY-5521"
CANARY_IP = "203.0.113.77"
CANARY_IPV6 = "2001:db8::7334"
CANARIES = (CANARY_TOKEN, CANARY_ACCOUNT, CANARY_URL, CANARY_IP, CANARY_IPV6)


def planted(value: str) -> list[tuple[str, dict[str, Any], str]]:
    return [
        ("pause", {**GOOD["pause"], "reason": f"note {value} end"}, "value"),
        ("abort", {**GOOD["abort"], "reason": value}, "value"),
        ("intent", {**GOOD["intent"], "symbol": value}, "value"),
        ("intent", {**GOOD["intent"], "intent_id": value}, "value"),
        ("vm_decision", {**GOOD["vm_decision"], "codes": [value]}, "value"),
    ]


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (CANARY_TOKEN, "canary_refused"),
        (CANARY_ACCOUNT, "canary_refused"),
        (CANARY_URL, "canary_refused"),  # registered, and also a URL
        (CANARY_IP, "ip_refused"),
        (CANARY_IPV6, "ip_refused"),
        ("Bearer abcdef", "token_refused"),
        ("eyJhbGciOiJIUzI1NiJ9.payload.sig", "token_refused"),
    ],
)
def test_a_planted_secret_is_refused_and_appears_nowhere(tmp_path, value, code):
    registered = CANARIES if code == "canary_refused" else ()  # the IP and bearer rows must stand on patterns
    store = make_store(tmp_path, count=1, forbidden=registered)
    run_dir = store.run_dir
    before = snapshot(run_dir)
    for kind, payload, _ in planted(value):
        with pytest.raises(EvidenceRefused) as caught:
            store.append(kind, payload)
        assert caught.value.code == code
        for text in (str(caught.value), repr(caught.value), caught.value.code):
            assert not any(canary in text for canary in CANARIES)
    assert snapshot(run_dir) == before
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert not any(canary.encode() in path.read_bytes() for canary in CANARIES)


@pytest.mark.parametrize(
    "key", ["session_token", "api_key", "account_id", "account", "ip", "client_ip", "login_url", "Authorization", "callback"]
)
def test_a_field_named_like_a_secret_is_refused_even_with_a_clean_value(tmp_path, key):
    store = make_store(tmp_path, count=1)
    before = snapshot(store.run_dir)
    with pytest.raises(EvidenceRefused) as caught:
        store.append("pause", {**GOOD["pause"], key: "ok"})
    assert caught.value.code == "secret_key_refused"
    with pytest.raises(EvidenceRefused):
        store.append("pause", {**GOOD["pause"], "reason": {key: "ok"}})
    assert snapshot(store.run_dir) == before


def test_a_registered_value_is_refused_case_insensitively_and_in_a_nested_value(tmp_path):
    store = make_store(tmp_path, forbidden=(CANARY_ACCOUNT,))
    for payload in (
        {**GOOD["pause"], "reason": CANARY_ACCOUNT.lower()},
        {**GOOD["pause"], "reason": {"deep": [CANARY_ACCOUNT]}},
        {**GOOD["pause"], "scope": CANARY_ACCOUNT},
    ):
        with pytest.raises(EvidenceRefused) as caught:
            store.append("pause", payload)
        assert caught.value.code == "canary_refused"
    assert store.records() == []


def test_ordinary_times_hashes_ids_and_the_simulation_account_are_not_false_positives(tmp_path):
    store = make_store(tmp_path)
    store.append("pause", {**GOOD["pause"], "reason": "stale at 09:30:00 IST, ratio 1.5, v1.2 of 2026-10-22"})
    store.append("session_close", {"outcome": "ok"})
    store.append("denied", {"intent_id": "sim-run1:abc", "code": "NOT_AN_IP_1.2.3"})
    assert len(store.records()) == 3


@pytest.mark.parametrize("canary", [CANARY_TOKEN, CANARY_ACCOUNT])
def test_a_registered_canary_as_a_run_id_or_session_id_never_reaches_the_log_or_the_manifest(tmp_path, canary):
    key = "mykey_sha256"  # a manifest field name is a field too
    cases: list[tuple[dict[str, Any], tuple[str, ...]]] = [
        ({"run_id": canary}, (canary,)),
        ({"session_id": canary}, (canary,)),
        ({"hashes": {key: "a" * 64}}, (key,)),
    ]
    for change, forbidden in cases:
        arguments: dict[str, Any] = dict(run_id=RUN, run_class="plumbing", session_id=SESSION, hashes=HASHES)
        with pytest.raises(EvidenceRefused) as caught:
            EvidenceStore.create(tmp_path, forbidden=forbidden, **{**arguments, **change})
        assert caught.value.code == "canary_refused", change
        assert not list(tmp_path.iterdir()), change  # not even the run directory

    # open() scans the identity it will stamp on every record, and writes nothing when it refuses.
    plain = EvidenceStore.create(tmp_path, run_id=canary, run_class="plumbing", session_id=SESSION, hashes=HASHES)
    other = make_store(tmp_path / "other", count=1)
    for root, run_id, session_id in ((tmp_path, canary, SESSION), (tmp_path / "other", RUN, canary)):
        run_dir = (root / run_id).resolve()
        before = snapshot(run_dir)
        with pytest.raises(EvidenceRefused) as caught:
            EvidenceStore.open(root, run_id=run_id, session_id=session_id, forbidden=(canary,))
        assert caught.value.code == "canary_refused" and snapshot(run_dir) == before
    assert plain.records() == [] and len(other.records()) == 1


def test_a_canary_session_id_inside_a_payload_is_refused_for_the_log(tmp_path):
    store = make_store(tmp_path, forbidden=(CANARY_ACCOUNT,))
    before = snapshot(store.run_dir)
    with pytest.raises(EvidenceRefused) as caught:
        store.append("session_excluded", {**GOOD["session_excluded"], "session_id": CANARY_ACCOUNT})
    assert caught.value.code == "canary_refused" and snapshot(store.run_dir) == before


@pytest.mark.parametrize("field", ["run_id", "session_id"])
def test_the_generated_envelope_is_scanned_before_the_write(tmp_path, field):
    # A store created before the value was registered (the registry can grow): the append scans the whole envelope.
    arguments: dict[str, Any] = dict(run_id=RUN, run_class="plumbing", session_id=SESSION, hashes=HASHES)
    arguments[field] = CANARY_ACCOUNT
    store = EvidenceStore.create(tmp_path, **arguments)
    store._forbidden = (CANARY_ACCOUNT,)
    before = snapshot(store.run_dir)
    with pytest.raises(EvidenceRefused) as caught:
        store.append("vm_ambiguous", INTENT)
    assert caught.value.code == "canary_refused"
    assert snapshot(store.run_dir) == before and store.records() == []
    assert CANARY_ACCOUNT.encode() not in (store.run_dir / LOG_NAME).read_bytes()


@pytest.mark.parametrize("run_id", ["10.1.2.3", "www.example", "eyJhbGciOiJIUzI1NiJ9.payload", "run1\n"])
def test_an_address_like_run_id_is_refused_before_anything_exists(tmp_path, run_id):
    with pytest.raises(EvidenceRefused):
        EvidenceStore.create(tmp_path, run_id=run_id, run_class="plumbing", session_id=SESSION, hashes=HASHES)
    assert not list(tmp_path.iterdir())


# --- the abort latch at the store ------------------------------------------------------------------------


def test_after_an_abort_only_a_further_abort_and_the_close_can_be_appended(tmp_path):
    store = make_store(tmp_path)
    store.append("abort", GOOD["abort"])
    assert ALLOWED_AFTER_ABORT == {"abort", "session_close"}
    before = snapshot(store.run_dir)
    for kind in sorted(set(GOOD) - ALLOWED_AFTER_ABORT):
        with pytest.raises(EvidenceAborted) as caught:
            store.append(kind, GOOD[kind])
        assert caught.value.code == "run_aborted"
    assert snapshot(store.run_dir) == before
    store.append("abort", {"event": "bypass_detected", "reason": "second cause"})
    closing = store.append("session_close", GOOD["session_close"])
    assert reopen(tmp_path).head()[0] == closing["seq"]  # the latch survives a restart


# --- conformance to the port 64-07 tested against --------------------------------------------------------


class StoreEvidence:
    """``MemEvidence``'s shape over a real store, so 64-07's own scenarios run on the real thing."""

    roots: list[str] = []
    instances: list["StoreEvidence"] = []

    def __init__(self) -> None:
        root = tempfile.mkdtemp(prefix="evidence-port-")
        self.roots.append(root)
        self.instances.append(self)
        self.store = EvidenceStore.create(
            root, run_id=sim.RUN_ID, run_class="plumbing", session_id="s-1", hashes=HASHES
        )
        self.crash_before: str | None = None
        self.crash_after: str | None = None
        self.rows: list[dict[str, Any]] = []  # what MemEvidence would hold: the kind and the payload only

    def append(self, kind: str, payload: Any) -> Any:
        if self.crash_before == kind:
            self.crash_before = None
            raise sim.Crash(kind)
        record = self.store.append(kind, payload)
        self.rows.append({"kind": kind, **payload})
        if self.crash_after == kind:
            self.crash_after = None
            raise sim.Crash(kind)
        return record

    def records(self) -> list[dict[str, Any]]:
        return self.store.records()

    def kinds(self, intent_id: str | None = None) -> list[str]:
        return [r["kind"] for r in self.rows if intent_id is None or r.get("intent_id") == intent_id]


def _expand(fn: Any) -> list[dict[str, Any]]:
    """The keyword sets pytest would pass: every parametrize mark of ``fn`` crossed."""

    axes: list[list[dict[str, Any]]] = []
    for mark in getattr(fn, "pytestmark", []):
        if mark.name != "parametrize":
            continue
        names = [n.strip() for n in (mark.args[0].split(",") if isinstance(mark.args[0], str) else mark.args[0])]
        rows = []
        for value in mark.args[1]:
            values = getattr(value, "values", value)
            values = values if len(names) > 1 else [values] if not hasattr(value, "values") else values
            rows.append(dict(zip(names, values)))
        axes.append(rows)
    return [{k: v for part in combo for k, v in part.items()} for combo in itertools.product(*axes)]


def _port_scenarios() -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for name, fn in sorted(vars(sim).items()):
        if not (name.startswith("test_") and inspect.isfunction(fn)) or "make_prep(" not in inspect.getsource(fn):
            continue
        extras = set(inspect.signature(fn).parameters) - {"tmp_path"}
        for params in _expand(fn):
            if extras <= set(params):
                found.append((name, params))
    return found


# This scenario edits evidence rows in memory to fake a tampered store. On disk that tamper is a corruption
# abort, and the tamper rows above prove it; the in-memory edit cannot reach a real file.
NOT_APPLICABLE = {"test_booking_recovery_aborts_on_a_payload_mismatch_and_on_an_unknown_ledger_booking"}
PORT_SCENARIOS = [row for row in _port_scenarios() if row[0] not in NOT_APPLICABLE]


def test_the_adapter_scenarios_are_all_collected_for_the_real_store():
    names = {name for name, _ in PORT_SCENARIOS}
    assert len(names) >= 25 and NOT_APPLICABLE <= set(vars(sim))
    assert {
        "test_the_live_path_orders_its_evidence_around_the_ledger_and_mints_once",
        "test_an_unresolved_mint_is_never_retried_and_a_late_entry_is_recorded_once",
        "test_a_booking_is_evidenced_before_and_after_the_ledger_commit",
        "test_a_refusal_that_died_before_its_row_is_closed_by_the_next_recovery",
    } <= names


@pytest.fixture
def port_roots():
    StoreEvidence.roots.clear()
    StoreEvidence.instances.clear()
    yield
    for root in StoreEvidence.roots:
        shutil.rmtree(root, ignore_errors=True)


def _scenario_ids() -> list[str]:
    seen: dict[str, int] = {}
    ids = []
    for name, _ in PORT_SCENARIOS:  # addresses in the parameters are not stable ids: number the rows instead
        seen[name] = seen.get(name, 0) + 1
        ids.append(f"{name}-{seen[name]}")
    return ids


@pytest.mark.usefixtures("frozen_service_clock")  # the sim module's own pytestmark never runs on direct calls
@pytest.mark.parametrize(("name", "params"), PORT_SCENARIOS, ids=_scenario_ids())
def test_the_real_store_passes_the_adapter_scenarios_written_against_the_port(
    tmp_path, monkeypatch, port_roots, name, params
):
    monkeypatch.setattr(sim, "MemEvidence", StoreEvidence)
    getattr(sim, name)(tmp_path=tmp_path, **params)
    assert StoreEvidence.roots  # the scenario really ran on a real store
    for instance in StoreEvidence.instances:  # the store keeps exactly what the adapter wrote, in order
        stored = instance.records()
        assert len(stored) == len(instance.rows)
        assert [{k: row[k] for k in written} for written, row in zip(instance.rows, stored)] == instance.rows
    for root in StoreEvidence.roots:
        records = read_records(root, sim.RUN_ID)  # every store a scenario left still verifies
        assert [r["seq"] for r in records] == list(range(1, len(records) + 1))

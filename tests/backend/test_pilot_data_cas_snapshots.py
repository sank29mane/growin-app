"""D-34 CAS class from session-dated security-master snapshots (64-02 Task 2b).

Every snapshot here is a small synthetic file built in the test. No test reads a real NSE file, and the
network sentinel below fails any socket use. The plants in the PR body (28 to 35) are each caught by one of
the tests named in them.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import io
import json
import re
import shutil
import socket
import subprocess
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import cas_testkit as ck
import duckdb
import pilot_data_testkit as kit
import pytest
from costs import cas_class
from costs.core import ScheduleError
from costs.cas_class import (
    CAS,
    NON_CAS,
    SUB_REASONS,
    CasClassRow,
    CasUnresolved,
    IciciRef,
    entry_sha256,
    is_append_only_extension,
    load_procedure_log,
    parse_procedure_log,
)
from market_data import models as market_models
from pilot_data import cas_snapshots as cs
from pilot_data import security_master as sm
from pilot_data.store import DB_FILE_NAME, PilotDataStore

REPO = Path(__file__).resolve().parents[2]
D, D_PREV, D_NEXT, SATURDAY = ck.D, ck.D_PREV, ck.D_NEXT, ck.SATURDAY


@pytest.fixture(autouse=True)
def network_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this module may open a socket or resolve a name."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("network used by the CAS snapshot code")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ck.World:
    return ck.make_world(tmp_path, monkeypatch, icici_days=(D_PREV, D, D_NEXT))


def refusal(call: Callable[[], Any]) -> str:
    with pytest.raises(cs.CasSnapshotError) as caught:
        call()
    return caught.value.code


def raw_path(world: ck.World, day: date = D) -> Path:
    (path,) = sorted((cs.cas_dir(world.root, day) / "raw").glob("*.csv.gz"))
    return path


def index_path(world: ck.World, day: date = D, version: int = 1) -> Path:
    (path,) = sorted((cs.cas_dir(world.root, day) / "index").glob(f"v{version}-*.json"))
    return path


def tree_bytes(directory: Path) -> dict[str, bytes]:
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


# ---- the kit itself ----------------------------------------------------------------------


def test_kit_check_digit_is_independent_of_the_validator() -> None:
    assert ck.isin_with_check("INE002A0101") == "INE002A01018"
    for isin in (ck.ISIN_DVR, ck.ISIN_UNK1, ck.ISIN_CONF, ck.ISIN_AMB1, ck.ISIN_ABSENT):
        assert market_models.is_valid_isin(isin)
    assert not market_models.is_valid_isin(ck.ISIN_BADCHK)
    assert not market_models.is_valid_isin(ck.ISIN_DUMMY)


# ---- resolution from the same-date snapshot ----------------------------------------------


def test_cas_resolves_from_same_date_snapshot(world: ck.World) -> None:
    outcome = world.import_()
    spec, impl = cs.running_identity()
    row = world.resolve("RELIND")
    assert isinstance(row, CasClassRow)
    assert (row.isin, row.nse_symbol, row.series, row.cas_class) == (ck.ISIN_REL, "RELIANCE", "EQ", CAS)
    assert row.snapshot_date == D
    assert row.snapshot_sha256 == hashlib.sha256(raw_path(world).read_bytes()).hexdigest() == outcome.raw_sha256
    assert row.index_sha256 == hashlib.sha256(index_path(world).read_bytes()).hexdigest() == outcome.index_sha256
    assert (row.procedure_version, row.spec_sha256, row.impl_sha256) == (1, spec, impl)
    assert (row.u24_status, row.u24_evidence_ref, row.stock_code) == ("unverified", None, "RELIND")
    assert row.icici_ref == IciciRef(D, world.icici_sha[D], row.icici_ref.row_sha256)
    non_cas = world.resolve("NIFBEE")
    assert isinstance(non_cas, CasClassRow) and non_cas.cas_class == NON_CAS
    assert world.resolve("CANBAN").cas_class == NON_CAS
    assert world.resolve("TATMOT").nse_symbol == "TMPV"
    assert world.resolve("DVRCO").cas_class == CAS


def test_cas_no_carry_forward_from_prior_snapshot(world: ck.World) -> None:
    world.import_(D_PREV)
    assert isinstance(world.resolve("RELIND", D_PREV), CasClassRow)
    for later in (D, D_NEXT):
        verdict = world.resolve("RELIND", later)
        assert isinstance(verdict, CasUnresolved)
        assert (verdict.sub_reason, verdict.detail) == ("snapshot_missing", "no_snapshot_for_date")
        assert not isinstance(verdict, CasClassRow)


def test_cas_missing_snapshot_unknown(world: ck.World) -> None:
    verdict = world.resolve("RELIND")
    assert isinstance(verdict, CasUnresolved) and verdict.sub_reason == "snapshot_missing"
    # Snapshots on both neighbours never stand in for the missing day.
    world.import_(D_PREV)
    world.import_(D_NEXT)
    bracketed = world.resolve("RELIND")
    assert isinstance(bracketed, CasUnresolved)
    assert (bracketed.sub_reason, bracketed.detail) == ("snapshot_missing", "no_snapshot_for_date")
    # A stored snapshot with no index for this procedure version is also missing, not guessed.
    world.log.approve(2)
    assert world.resolve("RELIND", D_PREV, version=2).detail == "index_missing_for_procedure"
    # So is a missing ICICI master for the date.
    world.import_()
    cs_row = world.resolve("RELIND", D)
    assert isinstance(cs_row, CasClassRow)


def test_cas_snapshot_wrong_date_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = ck.make_world(tmp_path, monkeypatch, icici_days=(D, D_NEXT))
    world.import_()
    shutil.copytree(cs.cas_dir(world.root, D), cs.cas_dir(world.root, D_NEXT))
    verdict = world.resolve("RELIND", D_NEXT)
    assert isinstance(verdict, CasUnresolved)
    assert (verdict.sub_reason, verdict.detail) == ("snapshot_wrong_date", "stored_entry_is_for_another_date")
    # The ICICI side: a pin dated for another day is refused, never re-pointed.
    other = world.resolve("RELIND", D, pin=cs.IciciPin.select(D_NEXT))
    assert isinstance(other, CasUnresolved)
    assert (other.sub_reason, other.detail) == ("snapshot_wrong_date", "icici_pin_date_mismatch")


def test_cas_hash_mismatch_refused(world: ck.World) -> None:
    world.import_()
    raw, index = raw_path(world), index_path(world)
    original_raw, original_index = raw.read_bytes(), index.read_bytes()

    raw.write_bytes(original_raw + b"x")
    verdict = world.resolve("RELIND")
    assert isinstance(verdict, CasUnresolved)
    assert (verdict.sub_reason, verdict.detail) == ("snapshot_hash_mismatch", "raw_bytes_do_not_match_their_hash")
    assert cs.verify_stored(world.root, D, 1) == ["raw_bytes_do_not_match_their_hash"]
    raw.write_bytes(original_raw)
    assert isinstance(world.resolve("RELIND"), CasClassRow)

    index.write_bytes(original_index + b" ")
    verdict = world.resolve("RELIND")
    assert isinstance(verdict, CasUnresolved)
    assert (verdict.sub_reason, verdict.detail) == ("snapshot_hash_mismatch", "index_bytes_do_not_match_their_hash")
    assert cs.verify_stored(world.root, D, 1) == ["index_bytes_do_not_match_their_hash"]
    index.write_bytes(original_index)

    # Garbage that is not JSON, and a second raw file beside the first.
    index.write_bytes(b"not json")
    assert world.resolve("RELIND").detail == "index_unreadable"
    index.write_bytes(original_index)
    (raw.parent / ("0" * 64 + ".csv.gz")).write_bytes(b"x")
    assert world.resolve("RELIND").detail == "more_than_one_raw_snapshot_for_date"


def test_cas_index_tamper_refused(world: ck.World) -> None:
    """An edited index that still parses and still names the right raw file is refused by its hash."""
    world.import_()
    index = index_path(world)
    doc = json.loads(index.read_bytes())
    doc["isins"][ck.ISIN_REL]["cas_class"] = NON_CAS
    index.write_bytes(cs.index_bytes(doc))
    verdict = world.resolve("RELIND")
    assert isinstance(verdict, CasUnresolved) and verdict.sub_reason == "snapshot_hash_mismatch"
    assert refusal(lambda: world.import_()) == "replace_refused"


def test_cas_class_change_reported(world: ck.World) -> None:
    rows_before = [ck.nse_row("RELIANCE", "EQ", ck.ISIN_REL, "0"), ck.nse_row("NIFTYBEES", "EQ", ck.ISIN_NIFTY, "0")]
    rows_after = [ck.nse_row("RELIANCE", "EQ", ck.ISIN_REL, "1"), ck.nse_row("NIFTYBEES", "EQ", ck.ISIN_NIFTY, "0")]
    world.import_(D_PREV, rows=rows_before)
    outcome = world.import_(D, rows=rows_after)
    assert outcome.report["previous_snapshot_compared"] == D_PREV.isoformat()
    assert outcome.report["class_changes"] == [{"isin": ck.ISIN_REL, "from": NON_CAS, "to": CAS}]
    assert world.resolve("RELIND", D_PREV).cas_class == NON_CAS
    assert world.resolve("RELIND", D).cas_class == CAS
    assert outcome.report["class_counts"] == {CAS: 1, NON_CAS: 1}


# ---- identity and flag rules -------------------------------------------------------------


def test_cas_non_isin_id_rejected(world: ck.World) -> None:
    outcome = world.import_()
    counts = outcome.report["id_counts"]["identity_invalid"]
    assert counts == {"groups": 2, "with_active_rows": 2}  # DUMMY and the bad check digit
    verdict = world.resolve("DUMMYC")
    assert isinstance(verdict, CasUnresolved) and verdict.sub_reason == "identity_invalid"
    assert isinstance(world.resolve("DVRCO"), CasClassRow)  # a valid IN9 id is accepted
    # The same id in the snapshot but not in the ICICI row is still refused through the NSE group.
    rows = [ck.nse_row("RELIANCE", "EQ", ck.ISIN_BADCHK, "1")]
    world.import_(D_NEXT, rows=rows)
    ick = ck.ISIN_BADCHK
    store_rows = [kit.master_row(2885, "RELIND", "EQ", "RELIANCE", "0.01", "10", ick, "RELIANCE")]
    ck.ingest_icici(world.root, D_NEXT, store_rows, fetched_at=datetime(2026, 11, 3, tzinfo=timezone.utc))
    bad = world.resolve("RELIND", D_NEXT)
    assert isinstance(bad, CasUnresolved) and bad.sub_reason == "identity_invalid"


def test_cas_duplicate_id_conflicting_flags_unresolved(world: ck.World) -> None:
    world.import_()
    verdict = world.resolve("CONFA")
    assert isinstance(verdict, CasUnresolved)
    assert (verdict.sub_reason, verdict.detail) == ("flag_conflict", "flag_conflict_for_isin")


def test_cas_unknown_flag_beside_valid_row_unresolved(world: ck.World) -> None:
    outcome = world.import_()
    assert outcome.report["id_counts"]["flag_unknown"]["groups"] == 2
    for code in ("UNKA", "UNKD"):  # usable row first, then unknown first
        verdict = world.resolve(code)
        assert isinstance(verdict, CasUnresolved), code
        assert (verdict.sub_reason, verdict.detail) == ("flag_unknown", "flag_unknown_for_isin")
    # An unknown flag on an inactive row of the same ISIN does not poison the group.
    rows = [ck.nse_row("RELIANCE", "EQ", ck.ISIN_REL, "1"), ck.nse_row("RELOLD", "EQ", ck.ISIN_REL, "X", del_flag="Y")]
    world.import_(D_NEXT, rows=rows)
    assert world.resolve("RELIND", D_NEXT).cas_class == CAS


def test_cas_duplicate_rows_keep_row_evidence(world: ck.World) -> None:
    world.import_()
    index = json.loads(index_path(world).read_bytes())
    nifty = index["isins"][ck.ISIN_NIFTY]["rows"]
    assert {(r["symbol"], r["active"]) for r in nifty} == {("NIFTYBEES", True), ("NIFTYINAV", False)}
    tata = index["isins"][ck.ISIN_TMPV]["rows"]
    assert {(r["symbol"], r["del_flag"], r["active"]) for r in tata} == {("TMPV", "N", True), ("TATAMOTORS", "Y", False)}
    assert world.resolve("NIFBEE").nse_symbol == "NIFTYBEES"
    old = cs.resolve_symbol(world.root, D, 1, "TATAMOTORS")
    assert isinstance(old, CasUnresolved) and old.detail == "symbol_has_no_active_row"
    assert cs.resolve_symbol(world.root, D, 1, "TMPV").cas_class == CAS


def test_cas_series_be_unsupported(world: ck.World) -> None:
    world.import_()
    verdict = world.resolve("HDFBE")
    assert isinstance(verdict, CasUnresolved)
    assert (verdict.sub_reason, verdict.detail) == ("series_unsupported", "icici_series_BE")
    # The EQ twin of the same ISIN resolves; the BE code never borrows its class.
    assert world.resolve("HDFBAN").cas_class == CAS
    assert not isinstance(verdict, CasClassRow)


def test_cas_identity_symbol_mismatch_unknown(world: ck.World) -> None:
    world.import_()
    stale = world.resolve("TATOLD")  # ICICI says TATAMOTORS; the active NSE row for the ISIN is TMPV
    assert isinstance(stale, CasUnresolved)
    assert (stale.sub_reason, stale.detail) == ("identity_mismatch", "no_active_row")
    absent = world.resolve("ABSENT")
    assert isinstance(absent, CasUnresolved)
    assert (absent.sub_reason, absent.detail) == ("identity_mismatch", "isin_not_in_snapshot")
    unmapped = world.resolve("NOSUCH")
    assert isinstance(unmapped, CasUnresolved)
    assert (unmapped.sub_reason, unmapped.detail) == ("identity_mismatch", "icici_unmapped")


def test_cas_icici_mapping_disagrees_identity_mismatch(world: ck.World) -> None:
    outcome = world.import_()
    disagreements = outcome.report["icici"]["disagreements"]
    assert [d["stock_code"] for d in disagreements] == ["TATOLD"]
    assert disagreements[0]["nse_active_symbols"] == ["TMPV"]
    verdict = world.resolve("TATOLD")
    assert isinstance(verdict, CasUnresolved) and verdict.sub_reason == "identity_mismatch"
    # The right mapping of the same ISIN is fine, so the refusal is the symbol cross-check, not the ISIN.
    assert world.resolve("TATMOT").cas_class == CAS


def test_cas_ambiguous_required_symbol_unresolved(world: ck.World) -> None:
    world.import_()
    two_isins = world.resolve("AMBIG")
    assert isinstance(two_isins, CasUnresolved)
    assert (two_isins.sub_reason, two_isins.detail) == ("identity_ambiguous", "symbol_carries_more_than_one_isin")
    twin = world.resolve("TWINC")
    assert isinstance(twin, CasUnresolved)
    assert (twin.sub_reason, twin.detail) == ("identity_ambiguous", "stock_code_ambiguous")
    shown = cs.resolve_symbol(world.root, D, 1, "AMBIG")
    assert isinstance(shown, CasUnresolved) and shown.sub_reason == "identity_ambiguous"


def test_cas_no_active_row_identity_mismatch(world: ck.World) -> None:
    outcome = world.import_()
    assert outcome.report["id_counts"]["no_active_row"] == {"groups": 1, "with_active_rows": 0}
    verdict = world.resolve("GONECO")
    assert isinstance(verdict, CasUnresolved)
    assert (verdict.sub_reason, verdict.detail) == ("identity_mismatch", "no_active_row")


@pytest.mark.parametrize(
    ("series", "del_flag", "eligibility", "status", "active"),
    [
        ("EQ", "N", "1", "1", True),
        ("EQ", "N", "1", "6", True),
        ("EQ", "N", "1", " 6 ", True),
        ("BE", "N", "1", "1", False),
        ("EQ", "Y", "1", "1", False),
        ("EQ", "N", "0", "1", False),
        ("EQ", "N", "1", "3", False),
        ("EQ", "N", "1", "", False),
        ("EQ", "", "1", "1", False),
        ("eq", "N", "1", "1", False),
    ],
)
def test_cas_active_row_predicate(series: str, del_flag: str, eligibility: str, status: str, active: bool) -> None:
    row = cs.SnapshotRow("X", series, ck.ISIN_REL, "1", del_flag, eligibility, status.strip())
    assert row.active is active
    parsed = cs.parse_snapshot(ck.snapshot_bytes([ck.nse_row("X", series, ck.ISIN_REL, "1", del_flag=del_flag,
                                                              eligibility=eligibility, status=status)]))
    assert parsed[0].active is active


# ---- resolve reads stored bytes only -----------------------------------------------------


def test_cas_resolve_never_reparses(world: ck.World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.import_()
    expected = world.resolve("RELIND")

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("resolution re-parsed the raw snapshot")

    monkeypatch.setattr(cs, "parse_snapshot", boom)
    monkeypatch.setattr(cs, "build_index", boom)
    assert world.resolve("RELIND") == expected
    assert cs.resolve_symbol(world.root, D, 1, "RELIANCE").isin == ck.ISIN_REL


def test_cas_reprocess_under_new_procedure(world: ck.World) -> None:
    world.import_()
    first = world.resolve("RELIND")
    old_index = index_path(world).read_bytes()
    world.log.approve(2, u24_status="verified", u24_evidence_ref="e" * 64)
    outcome = cs.reindex(world.root, D, 2)
    assert outcome.index_written
    second = world.resolve("RELIND", version=2)
    assert isinstance(second, CasClassRow)
    assert (second.procedure_version, second.u24_status, second.u24_evidence_ref) == (2, "verified", "e" * 64)
    assert second.snapshot_sha256 == first.snapshot_sha256
    assert second.index_sha256 != first.index_sha256
    # The recorded result under version 1 is unchanged and its index is byte-identical.
    assert world.resolve("RELIND") == first
    assert index_path(world).read_bytes() == old_index
    # A reindex of a version that is not approved, or already stored, changes nothing.
    assert refusal(lambda: cs.reindex(world.root, D, 3)) == "procedure_unapproved"
    assert cs.reindex(world.root, D, 2).index_written is False


def test_cas_index_replace_refused(world: ck.World) -> None:
    world.import_()
    folder = cs.cas_dir(world.root, D) / "index"
    (folder / ("v1-" + "a" * 64 + ".json")).write_bytes(b"{}")
    assert refusal(lambda: world.import_()) == "index_replace_refused"
    assert refusal(lambda: cs.reindex(world.root, D, 1)) == "index_replace_refused"


def test_cas_reimport_different_bytes_refused(world: ck.World) -> None:
    world.import_()
    before = tree_bytes(cs.cas_dir(world.root, D))
    changed = ck.standard_rows() + [ck.nse_row("EXTRA", "EQ", ck.isin_with_check("INE0EXT0100"), "1")]
    assert refusal(lambda: world.import_(rows=changed)) == "reimport_different_bytes_refused"
    assert tree_bytes(cs.cas_dir(world.root, D)) == before
    assert isinstance(world.resolve("RELIND"), CasClassRow)


def test_cas_replay_immutable(world: ck.World) -> None:
    world.import_()
    stable = {k: v for k, v in tree_bytes(cs.cas_dir(world.root, D)).items() if k.startswith(("raw", "index"))}
    world.log.approve(2)
    cs.reindex(world.root, D, 2)
    refusal(lambda: world.import_(rows=[ck.nse_row("RELIANCE", "EQ", ck.ISIN_REL, "0")]))
    after = tree_bytes(cs.cas_dir(world.root, D))
    for name, data in stable.items():
        assert after[name] == data


def test_cas_identical_reimport_later_is_noop(world: ck.World) -> None:
    first_clock = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
    second_clock = first_clock + timedelta(days=30)
    first = world.import_(clock=lambda: first_clock)
    stored = tree_bytes(cs.cas_dir(world.root, D))
    second = world.import_(clock=lambda: second_clock)
    assert (second.raw_written, second.index_written) == (False, False)
    assert (second.raw_sha256, second.index_sha256) == (first.raw_sha256, first.index_sha256)
    after = tree_bytes(cs.cas_dir(world.root, D))
    for name, data in stored.items():
        if name != "imports.jsonl":
            assert after[name] == data
    lines = [json.loads(line) for line in after["imports.jsonl"].decode().splitlines()]
    assert [line["outcome"] for line in lines] == ["stored", "noop"]
    assert lines[0]["imported_at_utc"] != lines[1]["imported_at_utc"]
    # Times live only in imports.jsonl, so replay of index and report is time-free.
    for name, data in after.items():
        if name != "imports.jsonl":
            assert b"2026-11-20" not in data and b"2026-12-20" not in data


def test_cas_index_hash_round_trip(world: ck.World) -> None:
    outcome = world.import_()
    raw, index = raw_path(world), index_path(world)
    assert raw.name == f"{outcome.raw_sha256}.csv.gz"
    assert index.name == f"v1-{outcome.index_sha256}.json"
    assert hashlib.sha256(index.read_bytes()).hexdigest() == outcome.index_sha256
    again = cs.build_index(
        cs.parse_snapshot(raw.read_bytes()), session_date=D, source_file_name=ck.snapshot_name(D),
        raw_sha256=outcome.raw_sha256, entry=cas_class.approved_entry(load_procedure_log(), 1),
    )
    assert cs.index_bytes(again) == index.read_bytes()
    assert cs.verify_stored(world.root, D, 1) == []


# ---- the approval log and procedure identity ---------------------------------------------


def test_cas_procedure_requires_operator_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = ck.make_world(tmp_path, monkeypatch, approve=False)
    assert refusal(lambda: world.import_()) == "procedure_unapproved"
    assert not (world.root / "cas").exists()
    # Approval for another identity does not count.
    world.log.approve(1, spec="0" * 64)
    assert refusal(lambda: world.import_()) == "procedure_identity_differs"
    world.log.revoke(1)
    world.log.approve(2, impl="1" * 64)
    assert refusal(lambda: world.import_(version=2)) == "procedure_identity_differs"
    assert refusal(lambda: world.import_(version=1)) == "procedure_unapproved"  # revoked
    assert refusal(lambda: world.import_(version=3)) == "procedure_unapproved"
    assert refusal(lambda: cs.reindex(world.root, D, 1)) == "procedure_unapproved"
    assert not (world.root / "cas").exists()


def test_cas_committed_procedure_log_loads_and_chains() -> None:
    log = parse_procedure_log(json.loads((REPO / "backend/costs/schedules/nse_cas_procedure.json").read_text()))
    previous = None
    for raw in log.raw_entries:
        assert raw["prev_sha256"] == previous
        previous = entry_sha256(raw)


def test_cas_procedure_unapproved_resolution_unknown(world: ck.World) -> None:
    world.import_()
    assert isinstance(world.resolve("RELIND"), CasClassRow)
    world.log.revoke(1)
    verdict = world.resolve("RELIND")
    assert isinstance(verdict, CasUnresolved)
    assert (verdict.sub_reason, verdict.detail) == ("procedure_unapproved", "procedure_version_revoked")
    shown = cs.resolve_symbol(world.root, D, 1, "RELIANCE")
    assert isinstance(shown, CasUnresolved) and shown.sub_reason == "procedure_unapproved"
    # The entry removed altogether, and an unreadable log, are unapproved too.
    world.log.entries.clear()
    world.log.flush()
    assert world.resolve("RELIND").detail == "procedure_version_absent"
    world.log.path.write_text("{ not json")
    assert world.resolve("RELIND").detail == "procedure_file_invalid"


def test_cas_procedure_impl_change_requires_approval(world: ck.World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.import_()
    spec, impl = cs.running_identity()
    assert isinstance(world.resolve("RELIND"), CasClassRow)

    original = market_models.is_valid_isin

    def is_valid_isin_changed(value: object) -> bool:  # same behaviour, different source
        return bool(original(value)) is True

    monkeypatch.setattr(market_models, "is_valid_isin", is_valid_isin_changed)
    assert cs.running_identity() == (spec, cs.running_identity()[1])
    assert cs.running_identity()[1] != impl
    # An import under the old approval is refused, and a resolution against the old index says so.
    assert refusal(lambda: world.import_(D_NEXT)) == "procedure_identity_differs"
    verdict = world.resolve("RELIND")
    assert isinstance(verdict, CasUnresolved)
    assert (verdict.sub_reason, verdict.detail) == ("procedure_unapproved", "running_implementation_differs_from_entry")
    assert refusal(lambda: cs.reindex(world.root, D, 1)) == "procedure_identity_differs"
    # Approving the new identity as a new version is what makes it run again.
    world.log.approve(2)
    assert isinstance(world.resolve("RELIND", version=2), CasUnresolved)  # no version 2 index yet
    cs.reindex(world.root, D, 2)
    assert isinstance(world.resolve("RELIND", version=2), CasClassRow)


def test_cas_procedure_isin_shape_change_requires_approval(world: ck.World, monkeypatch: pytest.MonkeyPatch) -> None:
    shape = market_models._ISIN_SHAPE
    # The validator pattern is pinned. A change to it is a new procedure version, never a silent edit.
    assert (shape.pattern, int(shape.flags)) == ("IN[EF9][A-Z0-9]{8}[0-9]", 32)
    assert cs.impl_parts()["market_data.models._ISIN_SHAPE"] == {"pattern": shape.pattern, "flags": 32}
    world.import_()
    _, impl = cs.running_identity()
    monkeypatch.setattr(market_models, "_ISIN_SHAPE", re.compile("IN[EF9][A-Z0-9]{8}[0-9]|ZZ"))
    assert cs.running_identity()[1] != impl
    assert refusal(lambda: world.import_(D_NEXT)) == "procedure_identity_differs"
    assert world.resolve("RELIND").detail == "running_implementation_differs_from_entry"


def _global_names(code: types.CodeType) -> set[str]:
    found = set(code.co_names)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            found |= _global_names(const)
    return found


def test_cas_procedure_fingerprint_covers_validator_globals() -> None:
    parts = cs.impl_parts()
    functions = cs.fingerprinted_functions()
    covered = {fn.__name__ for fn in functions.values()} | {"_ISIN_SHAPE", "_ROW_COLUMNS"} | set(cs.INERT_GLOBALS)
    for name, fn in functions.items():
        assert name in parts, name
        module_globals = fn.__globals__
        used = {n for n in _global_names(fn.__code__) if n in module_globals and not n.startswith("__")}
        used -= {n for n in used if isinstance(module_globals[n], types.ModuleType)}
        assert used <= covered, f"{name} reads globals outside the fingerprint: {sorted(used - covered)}"
    for name in ("pilot_data/cas_snapshots.py", "costs/cas_class.py", "market_data.models._ISIN_SHAPE",
                 "security_master._ROW_COLUMNS"):
        assert name in parts
    assert len(parts) == 10


PINNED_SOURCE_SHA = {
    "security_master.snapshot_on": "e3bc15ed0ccb31150b8d3e94306df04d9227316bb50a0db96b17157d2159471b",
    "security_master.snapshot_by_sha": "4a61b5bb034e5a70f618e60ea8ca2507b70c542665c175a58141707fd5d29e18",
    "security_master.lookup_stock_code": "4ee54877c30c632b0787ebd09ddfe79772c2dc5b5648c9ddaeb6f9b002970418",
    "security_master._row_from_db": "50fe499f1f471dc694848be660478e8d93d04e00e848aaae51f37991ad782fee",
    "security_master._snapshot_from_db": "db162247251bf6b997fbf7c1eb6d37a6017d81ae917353530de9439bc9b6a3b5",
    "market_data.models.is_valid_isin": "2f651134fd1dfe4984c0372c4e6c42b5b1502d25bfb30c0e48181f3c3dd1622b",
}


def test_cas_procedure_icici_read_change_requires_approval(world: ck.World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ICICI master read is part of the approved implementation. Any edit shows here first.

    The pins below are the approved reading. A deliberate change updates them in the same PR as a new procedure
    version for the operator to approve; it never lands as a quiet edit.
    """
    parts = cs.impl_parts()
    for name, pinned in PINNED_SOURCE_SHA.items():
        assert parts[name] == pinned, f"{name} changed: a new procedure version needs the operator's approval"
    assert parts["security_master._ROW_COLUMNS"] == (
        "token, stock_code, series, company_name, tick_size_raw, isin, nse_symbol, listing_raw, delisting_raw, "
        "delete_flag_raw"
    )
    world.import_()
    _, impl = cs.running_identity()
    real = sm.snapshot_on

    def snapshot_on_changed(store: Any, day: date) -> Any:
        return real(store, day)

    monkeypatch.setattr(sm, "snapshot_on", snapshot_on_changed)
    assert cs.running_identity()[1] != impl
    assert world.resolve("RELIND").detail == "running_implementation_differs_from_entry"


def test_cas_procedure_file_append_only(world: ck.World) -> None:
    world.log.approve(2)
    world.log.revoke(1)
    base = load_procedure_log().raw_entries
    assert len(base) == 3 and base[0]["prev_sha256"] is None
    assert base[1]["prev_sha256"] == entry_sha256(base[0])
    assert is_append_only_extension(base[:1], base) and is_append_only_extension(base, base)
    assert not is_append_only_extension(base, base[:2])
    assert not is_append_only_extension(base, (base[1], base[0], base[2]))
    edited = [dict(base[0], u24_status="verified"), *base[1:]]
    assert not is_append_only_extension(base, tuple(edited))
    # The chain makes an edited earlier entry unreadable, not just detectable by a diff.
    world.log.entries[0]["u24_status"] = "verified"
    world.log.flush()
    with pytest.raises(ScheduleError):
        load_procedure_log()
    world.log.entries[0]["u24_status"] = "unverified"
    world.log.entries.pop(1)
    world.log.flush()
    with pytest.raises(ScheduleError):
        load_procedure_log()


def test_cas_procedure_file_append_only_against_main() -> None:
    """On CI with full history the committed file must extend the one on origin/main. A shallow checkout skips."""
    path = "backend/costs/schedules/nse_cas_procedure.json"
    try:
        probe = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", "-q", "origin/main"],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available")
    if probe.returncode != 0:
        pytest.skip("origin/main is not in this checkout (shallow clone); the unit test above still runs")
    shown = subprocess.run(["git", "-C", str(REPO), "show", f"origin/main:{path}"], capture_output=True, timeout=30)
    base: tuple[dict[str, Any], ...] = ()
    if shown.returncode == 0:
        base = parse_procedure_log(json.loads(shown.stdout)).raw_entries
    branch = parse_procedure_log(json.loads((REPO / path).read_text())).raw_entries
    assert is_append_only_extension(base, branch)


def test_cas_u24_status_from_procedure_entry(world: ck.World) -> None:
    world.import_()
    assert world.resolve("RELIND").u24_status == "unverified"
    world.log.approve(2, u24_status="verified", u24_evidence_ref="a" * 64)
    cs.reindex(world.root, D, 2)
    row = world.resolve("RELIND", version=2)
    assert (row.u24_status, row.u24_evidence_ref) == ("verified", "a" * 64)
    assert world.resolve("RELIND").u24_status == "unverified"
    # Verified without the saved circular's hash does not load, so nothing resolves.
    world.log.approve(3, u24_status="verified")
    verdict = world.resolve("RELIND")
    assert isinstance(verdict, CasUnresolved) and verdict.detail == "procedure_file_invalid"
    # CasClassRow has no default status to fall back on.
    with pytest.raises(TypeError):
        CasClassRow(isin=ck.ISIN_REL)  # type: ignore[call-arg]


def test_cas_no_default_class_is_defined_anywhere() -> None:
    assert SUB_REASONS == (
        "snapshot_missing", "snapshot_wrong_date", "snapshot_hash_mismatch", "procedure_unapproved",
        "series_unsupported", "identity_invalid", "identity_mismatch", "identity_ambiguous", "flag_unknown",
        "flag_conflict",
    )
    assert not hasattr(cas_class, "DEFAULT_CLASS") and not hasattr(cs, "DEFAULT_CLASS")
    for field in ("cas_class", "isin", "nse_symbol", "snapshot_sha256", "u24_status"):
        assert field not in {name for name, p in inspect.signature(CasClassRow).parameters.items()
                             if p.default is not inspect.Parameter.empty}
    assert "cas_class" not in {f for f in CasUnresolved.__dataclass_fields__}
    for fn in (cs.resolve_broker, cs.resolve_symbol, cs.import_snapshot, cs.reindex):
        assert inspect.signature(fn).parameters["procedure_version"].default is inspect.Parameter.empty
    assert inspect.signature(cs.resolve_broker).parameters["icici_pin"].default is inspect.Parameter.empty


def _sc_missing(w: ck.World) -> Any:
    return w.resolve("RELIND")


def _sc_wrong_date(w: ck.World) -> Any:
    w.import_()
    shutil.copytree(cs.cas_dir(w.root, D), cs.cas_dir(w.root, D_NEXT))
    return w.resolve("RELIND", D_NEXT)


def _sc_hash(w: ck.World) -> Any:
    w.import_()
    raw_path(w).write_bytes(b"tampered")
    return w.resolve("RELIND")


def _sc_unapproved(w: ck.World) -> Any:
    w.import_()
    w.log.revoke(1)
    return w.resolve("RELIND")


def _imported(code: str) -> Callable[[ck.World], Any]:
    def run(w: ck.World) -> Any:
        w.import_()
        return w.resolve(code)

    return run


SCENARIOS: dict[str, tuple[Callable[[ck.World], Any], str]] = {
    "snapshot_missing": (_sc_missing, "snapshot_missing"),
    "snapshot_wrong_date": (_sc_wrong_date, "snapshot_wrong_date"),
    "snapshot_hash_mismatch": (_sc_hash, "snapshot_hash_mismatch"),
    "procedure_unapproved": (_sc_unapproved, "procedure_unapproved"),
    "series_unsupported": (_imported("HDFBE"), "series_unsupported"),
    "identity_invalid": (_imported("DUMMYC"), "identity_invalid"),
    "identity_mismatch": (_imported("TATOLD"), "identity_mismatch"),
    "identity_ambiguous": (_imported("AMBIG"), "identity_ambiguous"),
    "flag_unknown": (_imported("UNKA"), "flag_unknown"),
    "flag_conflict": (_imported("CONFA"), "flag_conflict"),
}


def _sc_icici_missing(w: ck.World) -> Any:
    w.import_(D_PREV)  # the NSE snapshot exists, the ICICI master for that date does not
    return w.resolve("RELIND", D_PREV)


def _sc_index_missing(w: ck.World) -> Any:
    w.import_()
    w.log.approve(2)
    return w.resolve("RELIND", version=2)


def _sc_log_absent(w: ck.World) -> Any:
    w.import_()
    w.log.entries.clear()
    w.log.flush()
    return w.resolve("RELIND")


# Every branch of ``resolve_broker`` that ends without a class, by (sub_reason, detail).
BRANCHES: dict[str, tuple[Callable[[ck.World], Any], str, str]] = {
    "no_snapshot_for_date": (_sc_missing, "snapshot_missing", "no_snapshot_for_date"),
    "icici_master_missing": (_sc_icici_missing, "snapshot_missing", "icici_master_missing"),
    "index_missing_for_procedure": (_sc_index_missing, "snapshot_missing", "index_missing_for_procedure"),
    "stored_entry_is_for_another_date": (_sc_wrong_date, "snapshot_wrong_date", "stored_entry_is_for_another_date"),
    "raw_bytes_do_not_match_their_hash": (_sc_hash, "snapshot_hash_mismatch", "raw_bytes_do_not_match_their_hash"),
    "procedure_version_revoked": (_sc_unapproved, "procedure_unapproved", "procedure_version_revoked"),
    "procedure_version_absent": (_sc_log_absent, "procedure_unapproved", "procedure_version_absent"),
    "icici_series_BE": (_imported("HDFBE"), "series_unsupported", "icici_series_BE"),
    "icici_isin_fails_validator": (_imported("DUMMYC"), "identity_invalid", "icici_isin_fails_validator"),
    "icici_unmapped": (_imported("NOSUCH"), "identity_mismatch", "icici_unmapped"),
    "isin_not_in_snapshot": (_imported("ABSENT"), "identity_mismatch", "isin_not_in_snapshot"),
    "symbol_has_no_active_row": (_imported("TATOLD"), "identity_mismatch", "no_active_row"),
    "isin_has_no_active_row": (_imported("GONECO"), "identity_mismatch", "no_active_row"),
    "stock_code_ambiguous": (_imported("TWINC"), "identity_ambiguous", "stock_code_ambiguous"),
    "symbol_carries_more_than_one_isin": (_imported("AMBIG"), "identity_ambiguous", "symbol_carries_more_than_one_isin"),
    "flag_unknown_usable_first": (_imported("UNKA"), "flag_unknown", "flag_unknown_for_isin"),
    "flag_unknown_unknown_first": (_imported("UNKD"), "flag_unknown", "flag_unknown_for_isin"),
    "flag_conflict": (_imported("CONFA"), "flag_conflict", "flag_conflict_for_isin"),
}


def test_cas_no_default_class(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every unresolved branch, run for real, is a CasUnresolved with its reason. Nothing falls back to a class."""
    assert {sub for _, sub, _ in BRANCHES.values()} == set(SUB_REASONS)
    for name, (run, sub_reason, detail) in BRANCHES.items():
        folder = tmp_path / name
        folder.mkdir()
        verdict = run(ck.make_world(folder, monkeypatch, icici_days=(D, D_NEXT)))
        assert isinstance(verdict, CasUnresolved), f"{name} resolved to {verdict!r}"
        assert (verdict.sub_reason, verdict.detail) == (sub_reason, detail), name


def test_cas_scenarios_cover_every_sub_reason() -> None:
    assert tuple(SCENARIOS) == SUB_REASONS


@pytest.mark.parametrize("name", SUB_REASONS)
def test_cas_every_sub_reason_is_unresolved_with_no_class(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One trigger per sub-reason, each a typed verdict with one reason and no class anywhere."""
    world = ck.make_world(tmp_path, monkeypatch, icici_days=(D, D_NEXT))
    run, expected = SCENARIOS[name]
    verdict = run(world)
    assert isinstance(verdict, CasUnresolved), f"{name} resolved to {verdict!r}"
    assert not isinstance(verdict, CasClassRow)
    assert verdict.sub_reason == expected
    assert verdict.detail
    record = cas_class.cas_record(verdict, "RELIANCE")
    assert record["cas_class"] is None and record["sub_reason"] == expected
    assert cs.describe(verdict).startswith(f"session_scope_unknown {expected} ")


# ---- the ICICI master pin ----------------------------------------------------------------


def test_cas_icici_master_exact_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = ck.make_world(tmp_path, monkeypatch, icici_days=(D_PREV, D_NEXT))
    world.import_()
    verdict = world.resolve("RELIND")
    assert isinstance(verdict, CasUnresolved)
    assert (verdict.sub_reason, verdict.detail) == ("snapshot_missing", "icici_master_missing")
    with PilotDataStore(world.root, workspace="india", read_only=True) as store:
        assert sm.snapshot_on(store, D) is None
        assert sm.snapshot_on(store, D_PREV).snapshot_date == D_PREV
        assert sm.snapshot_on(store, D_NEXT).snapshot_date == D_NEXT
        assert sm.snapshot_by_sha(store, world.icici_sha[D_PREV], D) is None
    # No store at all is also a missing master, not an error.
    bare = tmp_path / "bare"
    other = ck.World(bare, tmp_path, world.calendar, world.log)
    other_dir = cs.cas_dir(bare, D)
    shutil.copytree(cs.cas_dir(world.root, D), other_dir)
    assert other.resolve("RELIND").detail == "icici_master_missing"


def test_cas_icici_same_bytes_two_dates_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = ck.make_world(tmp_path, monkeypatch, icici_days=(D, D_NEXT))
    assert world.icici_sha[D] == world.icici_sha[D_NEXT]  # the same bytes saved on two dates
    world.import_(D)
    world.import_(D_NEXT)
    for day in (D, D_NEXT):
        chosen = world.resolve("RELIND", day)
        assert chosen.icici_ref.snapshot_date == day
        pin = cs.IciciPin.recorded(chosen.icici_ref.snapshot_sha256, chosen.icici_ref.row_sha256)
        replay = world.resolve("RELIND", day, pin=pin)
        assert replay == chosen and replay.icici_ref.snapshot_date == day


def test_cas_icici_pin_replay_stable(world: ck.World) -> None:
    world.import_()
    first = world.resolve("RELIND")
    pin = cs.IciciPin.recorded(first.icici_ref.snapshot_sha256, first.icici_ref.row_sha256)
    changed = [dict(row) for row in ck.ICICI_ROWS]
    changed.append(kit.master_row(9999, "LATE", "EQ", "LATE CO", "0.01", "1", ck.isin_with_check("INE0LAT0100"), "LATE"))
    newer = ck.ingest_icici(world.root, D, changed, fetched_at=datetime(2026, 11, 3, 12, tzinfo=timezone.utc))
    assert newer != world.icici_sha[D]
    assert world.resolve("RELIND", pin=pin) == first  # recorded pin ignores the newer same-date snapshot
    selected = world.resolve("RELIND")
    assert selected.icici_ref.snapshot_sha256 in (newer, world.icici_sha[D])


def test_cas_icici_pin_wrong_date_refused(world: ck.World) -> None:
    world.import_()
    wrong_day = world.resolve("RELIND", D, pin=cs.IciciPin.select(D_NEXT))
    assert isinstance(wrong_day, CasUnresolved) and wrong_day.detail == "icici_pin_date_mismatch"
    ghost = cs.IciciPin.recorded("f" * 64, "f" * 64)
    missing = world.resolve("RELIND", D, pin=ghost)
    assert isinstance(missing, CasUnresolved) and missing.sub_reason == "snapshot_missing"
    row_sha = world.resolve("RELIND").icici_ref.row_sha256
    off = world.resolve("RELIND", D, pin=cs.IciciPin.recorded(world.icici_sha[D], "0" * 64))
    assert isinstance(off, CasUnresolved)
    assert (off.sub_reason, off.detail) == ("snapshot_hash_mismatch", "icici_row_differs_from_recorded")
    assert row_sha != "0" * 64
    for bad in ("select", "recorded", "other"):
        assert refusal(lambda bad=bad: cs.IciciPin(bad)) == "icici_pin_invalid"
    assert refusal(lambda: world.resolve("RELIND", pin="latest")) == "icici_pin_required"  # type: ignore[arg-type]


def test_cas_icici_blob_and_row_corruption_refused(world: ck.World) -> None:
    world.import_()
    sha = world.icici_sha[D]
    blob = world.root / "blobs" / "sha256" / sha[:2] / sha
    original = blob.read_bytes()
    blob.chmod(0o644)
    blob.write_bytes(original + b"\n")
    verdict = world.resolve("RELIND")
    assert isinstance(verdict, CasUnresolved)
    assert (verdict.sub_reason, verdict.detail) == ("snapshot_hash_mismatch", "icici_blob_blob_integrity")
    blob.write_bytes(original)
    assert isinstance(world.resolve("RELIND"), CasClassRow)
    con = duckdb.connect(str(world.root / DB_FILE_NAME))
    try:
        con.execute("UPDATE security_master_rows SET company_name = 'EDITED' WHERE stock_code = 'RELIND'")
    finally:
        con.close()
    verdict = world.resolve("RELIND")
    assert isinstance(verdict, CasUnresolved)
    assert (verdict.sub_reason, verdict.detail) == ("snapshot_hash_mismatch", "icici_row_does_not_match_its_hash")


# ---- inputs, formats and the importer ----------------------------------------------------


def test_cas_explicit_procedure_version_required(world: ck.World) -> None:
    world.import_()
    pin = cs.IciciPin.select(D)
    for bad in (None, True, "1", 1.0):
        assert refusal(lambda bad=bad: cs.resolve_broker(world.root, D, bad, "RELIND", pin)) == "procedure_version_required"
        assert refusal(lambda bad=bad: cs.resolve_symbol(world.root, D, bad, "RELIANCE")) == "procedure_version_required"
    assert refusal(lambda: world.import_(version=0)) == "procedure_version_required"
    assert refusal(lambda: cs.resolve_broker(world.root, datetime(2026, 10, 29), 1, "RELIND", pin)) == "session_date_invalid"
    assert refusal(lambda: cs.resolve_broker(world.root, D, 1, " ", pin)) == "stock_code_invalid"
    sink = io.StringIO()
    assert cs.main(["check-broker", "--root", str(world.root), "--date", D.isoformat(), "--stock-codes", "RELIND"],
                   out=sink) == 2


def test_cas_import_refuses_wrong_names_and_non_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = ck.make_world(tmp_path, monkeypatch, icici_days=(D,))
    wrong = ck.write_snapshot(tmp_path / "wrong", D, name=ck.snapshot_name(D_PREV))
    assert refusal(lambda: cs.import_snapshot(world.root, wrong, D, 1, calendar=world.calendar)) == "file_name_date_mismatch"
    odd = ck.write_snapshot(tmp_path / "odd", D, name="security.csv.gz")
    assert refusal(lambda: cs.import_snapshot(world.root, odd, D, 1, calendar=world.calendar)) == "file_name_date_mismatch"
    saturday = ck.write_snapshot(tmp_path / "sat", SATURDAY)
    assert refusal(lambda: cs.import_snapshot(world.root, saturday, SATURDAY, 1, calendar=world.calendar)) == "not_a_session"
    (tmp_path / "h").mkdir()
    holiday_calendar = ck.calendar_for(tmp_path / "h", holidays=(D,))
    good = ck.write_snapshot(tmp_path / "good", D)
    assert refusal(lambda: cs.import_snapshot(world.root, good, D, 1, calendar=holiday_calendar)) == "not_a_session"
    assert refusal(lambda: cs.import_snapshot(world.root, good, D, 1, calendar=None)) == "not_a_session"
    assert not (world.root / "cas").exists()
    assert cs.file_date("NSE_CM_security_29102026.csv.gz") == D
    assert cs.file_date("NSE_CM_security_31022026.csv.gz") is None


def test_cas_weekend_holiday_need_no_snapshot(world: ck.World) -> None:
    """Non-session days are refused at import, so no stored snapshot exists for them and none is demanded."""
    saturday = ck.write_snapshot(world.tmp / "sat", SATURDAY)
    assert refusal(lambda: cs.import_snapshot(world.root, saturday, SATURDAY, 1, calendar=world.calendar)) == "not_a_session"
    assert not (cs.cas_dir(world.root, SATURDAY)).exists()


def _gz(text: str) -> bytes:
    import gzip

    return gzip.compress(text.encode("utf-8"), mtime=0)


FORMAT_CASES = {
    "snapshot_not_gzip": b"plain text, not gzip",
    "snapshot_columns": ck.snapshot_bytes([ck.nse_row("A", "EQ", ck.ISIN_REL, "1")], header=("TckrSymb", "SctySrs", "DelFlg")),
    "snapshot_columns_twice": ck.snapshot_bytes([], header=ck.HEADER + ("ISIN",)),
    "snapshot_row_width": _gz(",".join(ck.HEADER) + "\nshort,row\n"),
    "snapshot_empty": _gz(",".join(ck.HEADER) + "\n"),
    "snapshot_not_utf8": __import__("gzip").compress(b"\xff\xfe\x00bad", mtime=0),
}


@pytest.mark.parametrize("case", sorted(FORMAT_CASES))
def test_cas_malformed_snapshot_refused_and_nothing_stored(world: ck.World, case: str) -> None:
    folder = world.tmp / "inbox-bad"
    folder.mkdir()
    path = folder / ck.snapshot_name(D)
    path.write_bytes(FORMAT_CASES[case])
    code = refusal(lambda: cs.import_snapshot(world.root, path, D, 1, calendar=world.calendar))
    assert code == case.removesuffix("_twice")
    assert not (world.root / "cas").exists()


def test_cas_snapshot_on_unit(world: ck.World) -> None:
    with PilotDataStore(world.root, workspace="india", read_only=True) as store:
        assert sm.snapshot_on(store, D).snapshot_sha256 == world.icici_sha[D]
        assert sm.snapshot_on(store, date(2026, 10, 27)) is None


def test_cas_decision_paths_use_resolve_broker_only() -> None:
    """``resolve_symbol`` is for ``show``. No other module calls it, so no decision path can."""
    offenders: list[str] = []
    roots = [REPO / "backend", REPO / "gateway", REPO / "scripts"]
    for root in roots:
        for path in sorted(root.rglob("*.py")) if root.is_dir() else []:
            if path.name == "cas_snapshots.py" or ".venv" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and node.attr == "resolve_symbol") or (
                    isinstance(node, ast.Name) and node.id == "resolve_symbol"
                ) or (isinstance(node, ast.alias) and node.name == "resolve_symbol"):
                    offenders.append(str(path.relative_to(REPO)))
    assert offenders == []
    probe = (REPO / "backend/brokers/breeze_gateway/intraday_probe.py").read_text()
    assert "resolve_broker" in probe


def test_cas_loader_no_network() -> None:
    forbidden = {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "ssl", "ftplib", "smtplib"}
    for path in (REPO / "backend/pilot_data/cas_snapshots.py", REPO / "backend/costs/cas_class.py"):
        tree = ast.parse(path.read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        assert not imported & forbidden, path.name


def test_cas_network_sentinel_trips_on_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AssertionError, match="network used"):
        socket.getaddrinfo("example.invalid", 80)


# ---- command line ------------------------------------------------------------------------


def run_cli(*argv: str) -> tuple[int, str]:
    sink = io.StringIO()
    code = cs.main(list(argv), out=sink)
    return code, sink.getvalue()


def test_cas_cli_procedure_prints_the_entry_to_approve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = ck.make_world(tmp_path, monkeypatch, approve=False)
    spec, impl = cs.running_identity()
    code, text = run_cli("procedure")
    assert code == 0 and f"spec_sha256 {spec}" in text and f"impl_sha256 {impl}" in text
    entry_text = text.split("approval_entry_to_add", 1)[1].split("\n", 1)[1]
    entry = json.loads(entry_text)
    assert (entry["procedure_version"], entry["prev_sha256"], entry["u24_status"]) == (1, None, "unverified")
    assert entry["spec_sha256"] == spec and entry["impl_sha256"] == impl
    world.log.approve(1)
    again = json.loads(run_cli("procedure")[1].split("approval_entry_to_add", 1)[1].split("\n", 1)[1])
    assert again["procedure_version"] == 2 and again["prev_sha256"] == entry_sha256(world.log.entries[0])


def test_cas_cli_import_show_check_verify_reindex(world: ck.World) -> None:
    calendar_file = world.tmp / "calendar.json"
    calendar_sha = hashlib.sha256(calendar_file.read_bytes()).hexdigest()
    path = ck.write_snapshot(world.tmp / "cli", D)
    required = world.tmp / "required.csv"
    required.write_text("stock_code,isin,nse_symbol,series\nRELIND," + ck.ISIN_REL + ",RELIANCE,EQ\nTATOLD,"
                        + ck.ISIN_TMPV + ",TATAMOTORS,EQ\n")
    code, text = run_cli("import", "--root", str(world.root), "--file", str(path), "--session-date", D.isoformat(),
                         "--procedure-version", "1", "--calendar", str(calendar_file), "--calendar-sha256", calendar_sha,
                         "--required", str(required))
    assert code == 0 and text.startswith(f"import date {D.isoformat()} procedure_version 1 raw_sha256 ")
    report = json.loads(text.split("\n", 1)[1])
    assert [item["stock_code"] for item in report["required_unresolved"]] == ["TATOLD"]
    assert report["required_unresolved"][0]["sub_reason"] == "identity_mismatch"
    common = ["--root", str(world.root), "--date", D.isoformat(), "--procedure-version", "1"]
    code, text = run_cli("show", *common, "--symbols", "RELIANCE", "TATAMOTORS")
    lines = text.splitlines()
    assert code == 0 and "class cas nse_symbol RELIANCE" in lines[0]
    assert lines[1] == "symbol TATAMOTORS session_scope_unknown identity_mismatch symbol_has_no_active_row"
    code, text = run_cli("check-broker", *common, "--stock-codes", "RELIND", "TATOLD")
    lines = text.splitlines()
    assert code == 0 and lines[0].startswith("stock_code RELIND class cas nse_symbol RELIANCE")
    assert f"icici_pin {D.isoformat()}:{world.icici_sha[D]}:" in lines[0]
    assert lines[1] == "stock_code TATOLD session_scope_unknown identity_mismatch no_active_row"
    assert run_cli("verify", *common) == (0, f"verify date {D.isoformat()} procedure_version 1 ok\n")
    raw_path(world).write_bytes(b"x")
    code, text = run_cli("verify", *common)
    assert code == 1 and "FAILED raw_bytes_do_not_match_their_hash" in text
    raw_path(world).write_bytes(path.read_bytes())
    world.log.approve(2)
    code, text = run_cli("reindex", "--root", str(world.root), "--session-date", D.isoformat(), "--procedure-version", "2")
    assert code == 0 and text.startswith(f"reindex date {D.isoformat()} procedure_version 2")
    code, text = run_cli("reindex", "--root", str(world.root), "--session-date", D.isoformat(), "--procedure-version", "9")
    assert (code, text) == (1, "refused procedure_unapproved\n")
    code, text = run_cli("import", "--root", str(world.root), "--file", str(path), "--session-date", D.isoformat(),
                         "--procedure-version", "1", "--calendar", str(calendar_file), "--calendar-sha256", "0" * 64)
    assert code == 1 and text.startswith("refused ")

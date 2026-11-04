"""D-34 CAS class from session-dated security-master snapshots (64-02 Task 2b).

Every snapshot here is a small synthetic file built in the test. No test reads a real NSE file, and the
network sentinel below fails any socket use. The plants in the PR body (28 to 35) are each caught by one of
the tests named in them.

The snapshot_on and snapshot_by_sha unit tests live here, not in the security-master test file, because they exist
only for this procedure and are pinned by its fingerprint.
"""

from __future__ import annotations

import ast
import sys
import dataclasses
import functools
import hashlib
import inspect
import io
import json
import re
import shutil
import socket
import subprocess
import textwrap
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
import pilot_data.core
from market_data import models as market_models
from pilot_data import cas_snapshots as cs
from pilot_data import core as pd_core
from pilot_data import models as pd_models
from pilot_data import security_master as sm
from pilot_data.core import PilotDataError
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
    assert (stale.sub_reason, stale.detail) == ("identity_mismatch", "icici_symbol_not_active_for_isin")
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


FLIPPED_FLAG_MAP = {"1": NON_CAS, "0": CAS, "anything_else": "unknown"}


def test_cas_resolve_never_reparses(world: ck.World, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real spec change is approved as v2. Replaying v1 must still give the stored v1 class, so it cannot re-derive."""
    world.import_()
    expected = world.resolve("RELIND")
    assert isinstance(expected, CasClassRow) and expected.cas_class == CAS
    monkeypatch.setitem(cs.SPEC, "flag_map", FLIPPED_FLAG_MAP)  # a re-parse under the new spec would say non_cas
    world.log.approve(2)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("resolution re-parsed the raw snapshot")

    monkeypatch.setattr(cs, "parse_snapshot", boom)
    monkeypatch.setattr(cs, "build_index", boom)
    monkeypatch.setattr(cs, "_group_verdict", boom)
    monkeypatch.setattr(cs.gzip, "GzipFile", boom)
    monkeypatch.setattr(cs.csv, "reader", boom)
    assert world.resolve("RELIND") == expected
    shown = cs.resolve_symbol(world.root, D, 1, "RELIANCE")
    assert isinstance(shown, CasClassRow) and shown.cas_class == CAS and shown.isin == ck.ISIN_REL


def test_cas_reprocess_under_new_procedure(world: ck.World, monkeypatch: pytest.MonkeyPatch) -> None:
    """v2 is a real spec change (the flag map flips). v1 still resolves to its stored class, v2 to its own."""
    world.import_()
    first = world.resolve("RELIND")
    first_nif = world.resolve("NIFBEE")
    assert (first.cas_class, first_nif.cas_class) == (CAS, NON_CAS)
    v1_file = index_path(world)
    old_index = v1_file.read_bytes()
    old_spec = cs.running_identity()[0]

    monkeypatch.setitem(cs.SPEC, "flag_map", FLIPPED_FLAG_MAP)
    assert cs.running_identity()[0] != old_spec
    # Code nobody approved never resolves, even for the version it was once approved for.
    unapproved = world.resolve("RELIND")
    assert isinstance(unapproved, CasUnresolved)
    assert (unapproved.sub_reason, unapproved.detail) == ("procedure_unapproved", "running_implementation_differs_from_entry")
    assert refusal(lambda: cs.reindex(world.root, D, 1)) == "procedure_identity_differs"

    world.log.approve(2, u24_status="verified", u24_evidence_ref="e" * 64)
    assert cs.reindex(world.root, D, 2).index_written
    second = world.resolve("RELIND", version=2)
    assert isinstance(second, CasClassRow)
    assert (second.procedure_version, second.u24_status, second.u24_evidence_ref) == (2, "verified", "e" * 64)
    assert second.cas_class == NON_CAS and second.cas_class != first.cas_class  # its own class
    assert world.resolve("NIFBEE", version=2).cas_class == CAS
    assert second.snapshot_sha256 == first.snapshot_sha256
    assert (second.spec_sha256, second.index_sha256) != (first.spec_sha256, first.index_sha256)
    # The recorded result under version 1 is unchanged and its index is byte-identical.
    assert world.resolve("RELIND") == first
    assert world.resolve("NIFBEE") == first_nif
    assert index_path(world) == v1_file and v1_file.read_bytes() == old_index
    # A reindex of a version that is not approved, or already stored, changes nothing.
    assert refusal(lambda: cs.reindex(world.root, D, 3)) == "procedure_unapproved"
    assert cs.reindex(world.root, D, 2).index_written is False


def test_cas_v1_replays_after_an_implementation_change_is_approved(
    world: ck.World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repro of O1: import under v1, change the implementation, approve v2, reindex. v1 must still resolve."""
    world.import_()
    first = world.resolve("RELIND")
    original = market_models.is_valid_isin

    def is_valid_isin_v2(value: object) -> bool:  # same behaviour, different source
        return bool(original(value)) is True

    monkeypatch.setattr(market_models, "is_valid_isin", is_valid_isin_v2)
    assert world.resolve("RELIND").detail == "running_implementation_differs_from_entry"
    world.log.approve(2)
    cs.reindex(world.root, D, 2)
    second = world.resolve("RELIND", version=2)
    assert isinstance(second, CasClassRow) and second.impl_sha256 != first.impl_sha256
    assert world.resolve("RELIND") == first  # v1 replay, recorded under the older implementation
    assert refusal(lambda: world.import_(D_NEXT)) == "procedure_identity_differs"  # import still wants entry 1 exactly

    def is_valid_isin_v3(value: object) -> bool:  # a third source, never approved
        return bool(original(value)) is True

    monkeypatch.setattr(market_models, "is_valid_isin", is_valid_isin_v3)
    for version in (1, 2):
        verdict = world.resolve("RELIND", version=version)
        assert isinstance(verdict, CasUnresolved)
        assert (verdict.sub_reason, verdict.detail) == ("procedure_unapproved", "running_implementation_differs_from_entry")
    world.log.revoke(2)  # v2's identity is no longer approved, and v3 never was
    assert isinstance(world.resolve("RELIND"), CasUnresolved)


def _tighten_isin_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """A behaviour-changing validator: the shape now admits INF and IN9 ids only, so every INE id turns invalid."""
    monkeypatch.setattr(market_models, "_ISIN_SHAPE", re.compile("IN[F9][A-Z0-9]{8}[0-9]"))


def test_cas_v1_replay_ignores_a_newer_validator(world: ck.World, monkeypatch: pytest.MonkeyPatch) -> None:
    """T1: replay under the recorded version returns what that version's index decided, not what the running validator says.

    Terra's repro: import under v1, approve a v2 whose ISIN pattern rejects INE ids, replay v1. v1 stays a class, v2 has
    its own verdict, and the v1 result is unchanged field for field.
    """
    world.import_()
    first = world.resolve("RELIND")
    first_dvr = world.resolve("DVRCO")
    assert isinstance(first, CasClassRow) and first.cas_class == CAS and isinstance(first_dvr, CasClassRow)
    before = index_path(world).read_bytes()
    _tighten_isin_shape(monkeypatch)
    assert not market_models.is_valid_isin(ck.ISIN_REL) and market_models.is_valid_isin(ck.ISIN_DVR)
    world.log.approve(2)
    outcome = cs.reindex(world.root, D, 2)
    assert outcome.index_written
    second = world.resolve("RELIND", version=2)
    assert isinstance(second, CasUnresolved)
    assert (second.sub_reason, second.detail) == ("identity_invalid", "isin_fails_validator")
    assert isinstance(world.resolve("DVRCO", version=2), CasClassRow)  # the id the new pattern still accepts
    replay = world.resolve("RELIND")
    assert replay == first and cas_class.cas_record(replay, "RELIANCE") == cas_class.cas_record(first, "RELIANCE")
    assert world.resolve("DVRCO") == first_dvr
    assert index_path(world).read_bytes() == before
    # What differs is only the evidence of which code ran: the entry's hash stays v1's, the running one is v2's.
    assert replay.impl_sha256 == first.impl_sha256 != cs.impl_sha256()
    assert replay.running_impl_sha256 == cs.impl_sha256() != first.running_impl_sha256
    assert second.impl_sha256 == cs.impl_sha256()


def test_cas_v1_replay_keeps_a_recorded_invalid_verdict_under_a_looser_validator(
    world: ck.World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: v1 recorded the id as invalid, a later validator accepts it, v1 replay stays invalid."""
    committed_shape = market_models._ISIN_SHAPE
    _tighten_isin_shape(monkeypatch)
    world.log.entries.clear()
    world.log.flush()
    world.log.approve(1)
    world.import_()
    recorded = world.resolve("RELIND")
    assert isinstance(recorded, CasUnresolved) and recorded.sub_reason == "identity_invalid"
    monkeypatch.setattr(market_models, "_ISIN_SHAPE", committed_shape)  # back to the committed validator
    assert market_models.is_valid_isin(ck.ISIN_REL)
    world.log.approve(2)
    cs.reindex(world.root, D, 2)
    assert isinstance(world.resolve("RELIND", version=2), CasClassRow)
    replay = world.resolve("RELIND")
    assert isinstance(replay, CasUnresolved)
    assert (replay.sub_reason, replay.detail) == ("identity_invalid", "isin_fails_validator")


def test_cas_v1_replay_after_a_newer_approval_still_refuses_a_tampered_index(
    world: ck.World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay trusts the recorded verdict only because the index hash is checked: edit a v1 row and it refuses."""
    world.import_()
    _tighten_isin_shape(monkeypatch)
    world.log.approve(2)
    cs.reindex(world.root, D, 2)
    assert isinstance(world.resolve("RELIND"), CasClassRow)
    v1 = index_path(world)
    doc = json.loads(v1.read_bytes())
    doc["isins"][ck.ISIN_REL]["sub_reason"] = "identity_invalid"  # turn a recorded class into a recorded refusal
    doc["isins"][ck.ISIN_REL]["cas_class"] = None
    v1.write_bytes(cs.index_bytes(doc))
    tampered = world.resolve("RELIND")
    assert isinstance(tampered, CasUnresolved) and tampered.sub_reason == "snapshot_hash_mismatch"
    assert isinstance(world.resolve("DVRCO", version=2), CasClassRow)  # v2 is a separate, intact index


def test_cas_replay_records_which_implementation_ran(world: ck.World, monkeypatch: pytest.MonkeyPatch) -> None:
    """O-P3a: ``impl_sha256`` is the entry's, ``running_impl_sha256`` is the code that ran the ICICI cross-check."""
    world.import_()
    row = world.resolve("RELIND")
    assert isinstance(row, CasClassRow)
    assert row.impl_sha256 == row.running_impl_sha256 == cs.impl_sha256()
    original = market_models.is_valid_isin

    def is_valid_isin_v2(value: object) -> bool:  # same behaviour, different source
        return bool(original(value)) is True

    monkeypatch.setattr(market_models, "is_valid_isin", is_valid_isin_v2)
    world.log.approve(2)
    replay = world.resolve("RELIND")
    assert isinstance(replay, CasClassRow) and replay == row
    assert replay.impl_sha256 == row.impl_sha256 != cs.impl_sha256()
    assert replay.running_impl_sha256 == cs.impl_sha256()
    shown = cs.resolve_symbol(world.root, D, 1, "RELIANCE")
    assert isinstance(shown, CasClassRow) and shown.running_impl_sha256 == cs.impl_sha256()
    assert f"running_impl_sha256 {cs.impl_sha256()}" in cs.describe(replay)
    # The shared R-28 and R-31 field list is unchanged: the running hash is evidence on the row, not in cas_record.
    assert "running_impl_sha256" not in cas_class.cas_record(replay, "RELIANCE")
    with pytest.raises(cas_class.InputError):
        dataclasses.replace(replay, running_impl_sha256="not-a-hash")


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


_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _outermost_function_nodes(tree: ast.AST) -> list[ast.AST]:
    """Every def, async def and lambda under ``tree`` at any depth (nested classes included). A function found is not
    descended into: walking it already covers whatever it contains."""
    found: list[ast.AST] = []
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, _FUNCTION_NODES):
            found.append(child)
        else:
            found += _outermost_function_nodes(child)
    return found


def _ast_names(node: ast.AST) -> set[str]:
    """Names and attribute names a subtree reads: the AST counterpart of ``co_names`` for source that has no function."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
    return names


def _units_of(target: Any) -> list[tuple[set[str], ast.AST, dict[str, Any]]]:
    """The code the CAS procedure fingerprint walks for one fingerprinted object, as (names read, syntax tree, globals).

    A module-level function is one unit. A class is read from its whole source, not from its members, so a property
    setter or deleter, a ``functools.cached_property``, a pydantic validator descriptor and a method of a nested class
    are all walked; only the functions a framework attaches to the class at runtime (not in its source) are left out.
    """
    if inspect.isfunction(target):
        tree = ast.parse(textwrap.dedent(inspect.getsource(target)))
        return [(_global_names(target.__code__), tree, target.__globals__)]
    scope = vars(sys.modules[target.__module__])
    tree = ast.parse(textwrap.dedent(inspect.getsource(target)))
    units = [(_ast_names(node), node, scope) for node in _outermost_function_nodes(tree)]
    return units + [unit for fn in _assigned_members(target) for unit in _units_of(fn)]


def _assigned_members(target: type) -> list[Any]:
    """Same-file functions bound into the class by assignment (``method = helper``): members with no def in its source."""
    home = inspect.getsourcefile(target)
    found = []
    for member in vars(target).values():
        if isinstance(member, (classmethod, staticmethod)):
            member = member.__func__
        if (inspect.isfunction(member) and member.__code__.co_filename == home
                and not member.__qualname__.startswith(f"{target.__qualname__}.")):
            found.append(member)
    return found


OWNER_CLASSES = (PilotDataStore, pd_models.SecurityMasterRow)
READ_THROUGH = {"_sm", "_models", "store", "_store", "_core", "_pd_models"}


def uncovered_dependencies() -> list[str]:
    """Globals and store or row methods that a fingerprinted callable reads but the fingerprint does not cover."""
    covered_attrs = {attr for _, _, attr in cs.FINGERPRINTED}
    covered = covered_attrs | set(cs.FINGERPRINTED_VALUE_NAMES) | set(cs.INERT_GLOBALS)
    whole_classes = {getattr(owner, attr) for _, owner, attr in cs.FINGERPRINTED if inspect.isclass(getattr(owner, attr))}
    problems: list[str] = []
    for name, owner, attr in cs.FINGERPRINTED:
        for names, _, module_globals in _units_of(getattr(owner, attr)):
            used = {n for n in names if n in module_globals and not n.startswith("__")
                    and not isinstance(module_globals[n], types.ModuleType)}
            problems += [f"{name}: global {n}" for n in sorted(used - covered)]
            for cls in OWNER_CLASSES:
                for n in sorted(names):
                    if (inspect.isfunction(vars(cls).get(n)) and n not in covered_attrs
                            and n not in cs.INERT_METHODS and cls not in whole_classes):
                        problems.append(f"{name}: method {cls.__name__}.{n}")
    return problems


def uncovered_module_reads(source: str | None = None) -> list[str]:
    """Attributes cas_snapshots.py reads from another module or from the store that nothing fingerprints."""
    tree = ast.parse(Path(cs.__file__).read_text() if source is None else source)
    covered = {attr for _, _, attr in cs.FINGERPRINTED} | set(cs.FINGERPRINTED_VALUE_NAMES)
    covered |= set(cs.INERT_SM_ATTRS) | set(cs.INERT_METHODS)
    problems = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in READ_THROUGH
                and node.attr not in covered):
            problems.append(f"{node.value.id}.{node.attr}")
    return sorted(set(problems))


REPO_BACKEND = REPO / "backend"


def _repo_local(module: types.ModuleType) -> bool:
    """Under ``backend/`` and not a virtualenv: the uv venv lives in ``backend/.venv``, so third parties sit below it too."""
    home = getattr(module, "__file__", None)
    if home is None or not Path(home).resolve().is_relative_to(REPO_BACKEND):
        return False
    return not {".venv", "site-packages"} & set(Path(home).resolve().relative_to(REPO_BACKEND).parts)


def _module_object(node: ast.expr, scope: dict[str, Any]) -> types.ModuleType | None:
    """The module a ``name`` or ``name.sub`` expression denotes in ``scope``, else None (instances are not followed)."""
    if isinstance(node, ast.Name):
        found = scope.get(node.id)
    elif isinstance(node, ast.Attribute):
        parent = _module_object(node.value, scope)
        found = None if parent is None else getattr(parent, node.attr, None)
    else:
        return None
    return found if isinstance(found, types.ModuleType) else None


def uncovered_module_attribute_reads() -> list[str]:
    """T2: ``somemodule.helper`` read by any fingerprinted callable, where somemodule is repo-local and helper is not in
    the fingerprint. ``uncovered_dependencies`` skips module-typed globals, so this closes that gap. A read that lands on
    a fingerprinted callable is fine (that callable is walked itself, so the closure is transitive); a nested module
    (``pkg.sub``) is followed to the attribute read through it.
    """
    covered_objects = {id(getattr(owner, attr)) for _, owner, attr in cs.FINGERPRINTED}
    covered_names = set(cs.FINGERPRINTED_VALUE_NAMES) | set(cs.INERT_GLOBALS) | set(cs.INERT_METHODS)
    problems: list[str] = []
    for name, owner, attr in cs.FINGERPRINTED:
        for _, tree, module_globals in _units_of(getattr(owner, attr)):
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                module = _module_object(node.value, module_globals)
                if module is None or not _repo_local(module) or node.attr in covered_names:
                    continue
                target = getattr(module, node.attr, None)
                if isinstance(target, types.ModuleType) or id(target) in covered_objects:
                    continue
                problems.append(f"{name}: module read {ast.unparse(node)}")
    return sorted(set(problems))


def local_imports_in_fingerprinted() -> list[str]:
    """An ``import`` inside a fingerprinted callable binds a name the two walks above cannot resolve (it is not in the
    function's globals), so a helper reached that way could change without changing impl_sha256. None is allowed: a
    fingerprinted callable takes its dependencies as module globals, where the walks see them.
    """
    problems: list[str] = []
    for name, owner, attr in cs.FINGERPRINTED:
        for _, tree, _ in _units_of(getattr(owner, attr)):
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    problems.append(f"{name}: local import {ast.unparse(node)}")
    return sorted(set(problems))


def uncovered_helper() -> None:  # a global of this test module, used by the planted dependency below
    return None


def test_cas_procedure_fingerprint_covers_dependencies() -> None:
    """Called methods count, not just modules: store open, query, blob read, the row digest and the helpers under it."""
    parts = cs.impl_parts()
    assert set(parts) == {name for name, _, _ in cs.FINGERPRINTED} | {
        "pilot_data/cas_snapshots.py", "costs/cas_class.py", "market_data.models._ISIN_SHAPE",
        "security_master._ROW_COLUMNS", "store._HASH_RE", "store.DB_FILE_NAME",
    }
    assert len(parts) == 22
    assert set(cs.fingerprinted_functions()) == {name for name, _, _ in cs.FINGERPRINTED}
    assert {"store.PilotDataStore.read_blob", "models.SecurityMasterRow", "core.canonical_sha256"} <= set(parts)
    assert uncovered_dependencies() == []
    assert uncovered_module_reads() == []
    assert uncovered_module_attribute_reads() == []
    assert local_imports_in_fingerprinted() == []


def test_cas_procedure_fingerprint_walk_catches_a_local_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 3 counterexample: a local import hides its helper from both walks, so it is reported on its own."""

    def lookup_with_a_local_import(store: Any, snapshot: Any, stock_code: str) -> Any:
        import pilot_data.core as helper

        return helper.utc_now()

    assert local_imports_in_fingerprinted() == []
    monkeypatch.setattr(sm, "lookup_stock_code", lookup_with_a_local_import)
    assert local_imports_in_fingerprinted() == [
        "security_master.lookup_stock_code: local import import pilot_data.core as helper"
    ]
    assert uncovered_module_attribute_reads() == []  # the gap this check closes


class _DescriptorHolder:  # defined in this file, so inspect.getsource reads it like a real fingerprinted class
    @classmethod
    def as_classmethod(cls) -> Any:
        import pilot_data.core as helper

        return helper.utc_now()

    @staticmethod
    def as_staticmethod() -> Any:
        import pilot_data.core as helper

        return helper.utc_now()

    @property
    def as_property(self) -> Any:
        import pilot_data.core as helper

        return helper.utc_now()


def _function_names(target: Any) -> set[str]:
    return {
        node.name
        for _, tree, _ in _units_of(target)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _fingerprint_only(monkeypatch: pytest.MonkeyPatch, label: str, holder: type) -> None:
    monkeypatch.setattr(cs, "FINGERPRINTED", ((label, sys.modules[__name__], holder.__name__),))


def test_cas_procedure_fingerprint_walk_sees_descriptor_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 4 counterexample: classmethod, staticmethod and property members of a fingerprinted class are walked."""
    assert _function_names(_DescriptorHolder) == {"as_classmethod", "as_staticmethod", "as_property"}
    _fingerprint_only(monkeypatch, "test.DescriptorHolder", _DescriptorHolder)
    assert local_imports_in_fingerprinted() == [
        "test.DescriptorHolder: local import import pilot_data.core as helper"
    ]


class _PropertySetterHolder:
    @property
    def value(self) -> int:
        return 1

    @value.setter
    def value(self, new: int) -> None:
        import pilot_data.core as helper

        helper.utc_now()


class _PropertyDeleterHolder:
    @property
    def value(self) -> int:
        return 1

    @value.deleter
    def value(self) -> None:
        import pilot_data.core as helper

        helper.utc_now()


class _CachedPropertyHolder:
    @functools.cached_property
    def value(self) -> Any:
        import pilot_data.core as helper

        return helper.utc_now()


class _NestedClassHolder:
    class Inner:
        def method(self) -> Any:
            import pilot_data.core as helper

            return helper.utc_now()


class _ImportFromHolder:
    def method(self) -> Any:
        from pilot_data.core import utc_now

        return utc_now()


class _AsyncMethodHolder:
    async def method(self) -> Any:
        import pilot_data.core as helper

        return helper.utc_now()


class _LambdaHolder:
    reader = lambda self: pd_core.utc_now()  # noqa: E731  (a lambda is a function the walk must see)


@pytest.mark.parametrize(
    ("holder", "planted"),
    [
        (_PropertySetterHolder, "import pilot_data.core as helper"),
        (_PropertyDeleterHolder, "import pilot_data.core as helper"),
        (_CachedPropertyHolder, "import pilot_data.core as helper"),
        (_NestedClassHolder, "import pilot_data.core as helper"),
        (_ImportFromHolder, "from pilot_data.core import utc_now"),
        (_AsyncMethodHolder, "import pilot_data.core as helper"),
    ],
    ids=["property_setter", "property_deleter", "cached_property", "nested_class_method", "import_from", "async_method"],
)
def test_cas_procedure_fingerprint_walk_reads_the_whole_class_source(
    monkeypatch: pytest.MonkeyPatch, holder: type, planted: str
) -> None:
    """#625 P3: a class is walked from its source, so a local import in any function inside it is reported, whatever
    descriptor or nesting wraps the function. The ImportFrom form is pinned here too: nothing else exercised it."""
    assert local_imports_in_fingerprinted() == []
    _fingerprint_only(monkeypatch, "test.Holder", holder)
    assert local_imports_in_fingerprinted() == [f"test.Holder: local import {planted}"]


def _assigned_helper(self: Any) -> Any:
    import pilot_data.core as helper

    return helper.utc_now()


class _AssignedMethodHolder:
    method = _assigned_helper
    as_static = staticmethod(_assigned_helper)


def test_cas_procedure_fingerprint_walk_reads_a_same_file_function_assigned_into_a_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#627 round 1 counterexample: ``method = helper`` has no def in the class source but is still walked."""
    assert "_assigned_helper" in _function_names(_AssignedMethodHolder)
    _fingerprint_only(monkeypatch, "test.AssignedMethodHolder", _AssignedMethodHolder)
    assert local_imports_in_fingerprinted() == [
        "test.AssignedMethodHolder: local import import pilot_data.core as helper"
    ]


def test_cas_procedure_fingerprint_walk_reads_a_lambda_in_a_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lambda assigned in a class body is a function the walk reads: its module read is reported."""
    assert uncovered_module_attribute_reads() == []
    _fingerprint_only(monkeypatch, "test.Holder", _LambdaHolder)
    assert uncovered_module_attribute_reads() == ["test.Holder: module read pd_core.utc_now"]


def test_cas_procedure_fingerprint_walk_catches_an_uncovered_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    def lookup_with_extra_reads(store: Any, snapshot: Any, stock_code: str) -> Any:
        uncovered_helper()  # a global nobody fingerprinted
        store.table_exists("security_master_rows")  # a store method nobody fingerprinted
        return None

    monkeypatch.setattr(sm, "lookup_stock_code", lookup_with_extra_reads)
    problems = uncovered_dependencies()
    assert "security_master.lookup_stock_code: global uncovered_helper" in problems
    assert "security_master.lookup_stock_code: method PilotDataStore.table_exists" in problems
    planted = Path(cs.__file__).read_text() + "\n_x = _sm.require_fresh_snapshot\n_y = store.table_exists\n"
    assert uncovered_module_reads(planted) == ["_sm.require_fresh_snapshot", "store.table_exists"]


def test_cas_procedure_fingerprint_walk_catches_a_repo_local_module_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """T2 counterexample: a fingerprinted callable that calls ``somemodule.helper()`` outside the fingerprint is reported."""

    def lookup_calling_module_helper(store: Any, snapshot: Any, stock_code: str) -> Any:
        pd_core.utc_now()  # repo-local module, function nobody fingerprinted
        sm.latest_snapshot(store)  # another module, another unfingerprinted function
        pd_models.SecurityMasterRow  # a repo-local module attribute that is fingerprinted: not a problem
        return sm.snapshot_by_sha(store, snapshot, stock_code)  # fingerprinted: not a problem

    assert uncovered_module_attribute_reads() == []
    monkeypatch.setattr(sm, "lookup_stock_code", lookup_calling_module_helper)
    problems = uncovered_module_attribute_reads()
    assert problems == [
        "security_master.lookup_stock_code: module read pd_core.utc_now",
        "security_master.lookup_stock_code: module read sm.latest_snapshot",
    ]
    # The global-name walk alone never sees it: both are module-typed globals, which it skips by design.
    assert not any("pd_core" in p or "latest_snapshot" in p for p in uncovered_dependencies())


def test_cas_procedure_fingerprint_walk_follows_nested_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    def lookup_through_a_package(store: Any, snapshot: Any, stock_code: str) -> Any:
        return pilot_data.core.utc_now()  # reached as package.module.attribute

    monkeypatch.setattr(sm, "lookup_stock_code", lookup_through_a_package)
    assert uncovered_module_attribute_reads() == [
        "security_master.lookup_stock_code: module read pilot_data.core.utc_now"
    ]


def _changed_function(*_args: Any, **_kwargs: Any) -> Any:
    return None


class _ChangedClass:
    pass


@pytest.mark.parametrize("name", [name for name, _, _ in cs.FINGERPRINTED])
def test_cas_procedure_fingerprint_changes_when_each_callable_changes(
    name: str, world: ck.World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch each fingerprinted callable's source: ``impl_sha256`` must move, so the old approval no longer fits."""
    (_, owner, attr), = [entry for entry in cs.FINGERPRINTED if entry[0] == name]
    world.import_()
    _, impl = cs.running_identity()
    assert isinstance(world.resolve("RELIND"), CasClassRow)
    replacement = _ChangedClass if inspect.isclass(getattr(owner, attr)) else _changed_function
    monkeypatch.setattr(owner, attr, replacement)
    assert cs.running_identity()[1] != impl, f"patching {name} left impl_sha256 unchanged"


def test_cas_unchecked_blob_read_is_not_the_approved_implementation(world: ck.World, monkeypatch: pytest.MonkeyPatch) -> None:
    """S1: ``read_blob`` verifies the ICICI master bytes. Replacing it with an unchecked read voids the approval."""
    world.import_()
    sha = world.icici_sha[D]
    blob = world.root / "blobs" / "sha256" / sha[:2] / sha
    blob.chmod(0o644)
    blob.write_bytes(blob.read_bytes() + b"\n")
    assert world.resolve("RELIND").detail == "icici_blob_blob_integrity"

    def read_blob_unchecked(self: Any, source_sha256: str) -> bytes:
        return self._blob_path(source_sha256).read_bytes()

    monkeypatch.setattr(PilotDataStore, "read_blob", read_blob_unchecked)
    verdict = world.resolve("RELIND")
    assert not isinstance(verdict, CasClassRow), "a corrupt ICICI blob resolved under the existing approval"
    assert isinstance(verdict, CasUnresolved)
    assert (verdict.sub_reason, verdict.detail) == ("procedure_unapproved", "running_implementation_differs_from_entry")


PINNED_SOURCE_SHA = {
    "security_master.snapshot_on": "e3bc15ed0ccb31150b8d3e94306df04d9227316bb50a0db96b17157d2159471b",
    "security_master.snapshot_by_sha": "4a61b5bb034e5a70f618e60ea8ca2507b70c542665c175a58141707fd5d29e18",
    "security_master.lookup_stock_code": "4ee54877c30c632b0787ebd09ddfe79772c2dc5b5648c9ddaeb6f9b002970418",
    "security_master._row_from_db": "50fe499f1f471dc694848be660478e8d93d04e00e848aaae51f37991ad782fee",
    "security_master._snapshot_from_db": "db162247251bf6b997fbf7c1eb6d37a6017d81ae917353530de9439bc9b6a3b5",
    "market_data.models.is_valid_isin": "2f651134fd1dfe4984c0372c4e6c42b5b1502d25bfb30c0e48181f3c3dd1622b",
    "store.PilotDataStore.__init__": "77bc9a41360053e004eafde3e3a079ddc6d9cabfdee5dfa701a9cd31fe74d886",
    "store.PilotDataStore._open_core": "cca7e07c521edc54b681afa50c36511ac3efe13e4d7952d0d59a919805dab2ee",
    "store.PilotDataStore.query": "10049c2094055a63b3cb26e5e39aefb4f763f100ee01b47991e08aa828e3050d",
    "store.PilotDataStore.read_blob": "3fbbec9ff42b82b1c4659f6e49cf297d1f1e3f3b33b04977845b62bb4387862f",
    "store.PilotDataStore._blob_path": "68f399698abcc514128e47ff15e04ce49bb47d4513a16e86ad73ff5037ff3e93",
    "models._Frozen": "06adcd9412501dd59bf8f16fde5cbacdb514bee1c7a7750b13f0c860d340ef10",
    "models.SecurityMasterRow": "b3bb87d9b5cc018edff66f483840b62a7577760fce55dcac7e906e2bc61ffad7",
    "core.canonical_sha256": "3d40031e6bb46498e1c07fd9a1a9c5cb5fa0e426074a39cc85b776e15cc1bb06",
    "core._reject_inexact": "c4448e1c4dd6be20e227243fd1b8d0a8a469f82cc4dde320f976291870be5838",
    "core.sha256_hex": "dd1292a7056e396570427d4cbfdb65d7e8a0ebd8a4c5e6c4514c7850cb2325a7",
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
    assert parts["store._HASH_RE"] == {"pattern": "^[0-9a-f]{64}$", "flags": 32}
    assert parts["store.DB_FILE_NAME"] == "pilot_india.duckdb"
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


def _sc_icici_blob_missing(w: ck.World) -> Any:
    w.import_()
    sha = w.icici_sha[D]
    (w.root / "blobs" / "sha256" / sha[:2] / sha).unlink()
    return w.resolve("RELIND")


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
    "icici_blob_missing": (_sc_icici_blob_missing, "snapshot_missing", "icici_blob_missing"),
    "index_missing_for_procedure": (_sc_index_missing, "snapshot_missing", "index_missing_for_procedure"),
    "stored_entry_is_for_another_date": (_sc_wrong_date, "snapshot_wrong_date", "stored_entry_is_for_another_date"),
    "raw_bytes_do_not_match_their_hash": (_sc_hash, "snapshot_hash_mismatch", "raw_bytes_do_not_match_their_hash"),
    "procedure_version_revoked": (_sc_unapproved, "procedure_unapproved", "procedure_version_revoked"),
    "procedure_version_absent": (_sc_log_absent, "procedure_unapproved", "procedure_version_absent"),
    "icici_series_BE": (_imported("HDFBE"), "series_unsupported", "icici_series_BE"),
    "isin_fails_validator": (_imported("DUMMYC"), "identity_invalid", "isin_fails_validator"),
    "icici_unmapped": (_imported("NOSUCH"), "identity_mismatch", "icici_unmapped"),
    "isin_not_in_snapshot": (_imported("ABSENT"), "identity_mismatch", "isin_not_in_snapshot"),
    "icici_symbol_not_active_for_isin": (
        _imported("TATOLD"), "identity_mismatch", "icici_symbol_not_active_for_isin"),
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


def test_cas_required_series_disagreement_reported(world: ck.World) -> None:
    """S2: a required BE instrument with the EQ mapping's code, ISIN and symbol is a disagreement, not a pass."""
    eq = {"stock_code": "RELIND", "isin": ck.ISIN_REL, "nse_symbol": "RELIANCE", "series": "EQ"}
    be = {**eq, "series": "BE"}
    outcome = world.import_(required=[eq, be])
    assert outcome.report["required_total"] == 2
    assert outcome.report["required_unresolved"] == [
        {**be, "sub_reason": "identity_mismatch", "detail": "required_row_disagrees"}
    ]


def test_cas_icici_store_errors_are_not_hash_mismatches(world: ck.World, monkeypatch: pytest.MonkeyPatch) -> None:
    """O4: a store that cannot be opened or queried is its own detail, never snapshot_hash_mismatch."""
    world.import_()

    def failing_open(error: Exception) -> Callable[..., Any]:
        def open_store(*_args: Any, **_kwargs: Any) -> Any:
            raise error

        return open_store

    cases = [
        (PilotDataError("store_identity_corrupt", "x"), "icici_store_error_store_identity_corrupt"),
        (PilotDataError("store_workspace_mismatch", "x"), "icici_store_error_store_workspace_mismatch"),
        (duckdb.IOException("could not set lock on file"), "icici_store_error_duckdb"),
    ]
    for error, detail in cases:
        with pytest.MonkeyPatch.context() as local:
            local.setattr(cs, "PilotDataStore", failing_open(error))
            verdict = world.resolve("RELIND")
        assert isinstance(verdict, CasUnresolved)
        assert (verdict.sub_reason, verdict.detail) == ("snapshot_missing", detail)
    for error, detail in [(duckdb.IOException("busy"), "icici_store_error_duckdb"),
                          (PilotDataError("table_name_invalid", "x"), "icici_store_error_table_name_invalid")]:
        def snapshot_on_fails(store: Any, day: date, _e: Exception = error) -> Any:
            raise _e

        with pytest.MonkeyPatch.context() as local:
            local.setattr(sm, "snapshot_on", snapshot_on_fails)
            verdict = world.resolve("RELIND")
        assert isinstance(verdict, CasUnresolved) and verdict.sub_reason == "snapshot_missing"
        assert verdict.detail == detail
    # Nothing was left patched, and the blob_missing branch is its own scenario (``icici_blob_missing`` in BRANCHES).
    assert isinstance(world.resolve("RELIND"), CasClassRow)


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
    assert lines[1] == "stock_code TATOLD session_scope_unknown identity_mismatch icici_symbol_not_active_for_isin"
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

"""CAS class types and the operator approval log (64-02 Task 2b, D-34).

The class itself comes from a session-dated snapshot (tests in ``test_pilot_data_cas_snapshots.py``). This file covers
the pure side in ``costs.cas_class``: the verdict types, the one record field list and the append-only, hash-chained
approval log. Every value is synthetic.
"""

from __future__ import annotations

import copy
import json
import socket
from datetime import date, datetime

import pytest

from costs import cas_class
from costs.cas_class import (
    CAS,
    NON_CAS,
    SUB_REASONS,
    CasClassRow,
    CasUnresolved,
    IciciRef,
    approved_entry,
    cas_record,
    entry_sha256,
    is_append_only_extension,
    load_procedure_log,
    parse_procedure_log,
)
from costs.core import InputError, ScheduleError

H = {name: ch * 64 for name, ch in (("snap", "a"), ("index", "b"), ("spec", "c"), ("impl", "d"), ("row", "e"))}
D = date(2026, 10, 29)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network used by the CAS-class loader")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def row(**changes):
    values = dict(
        isin="INE0REL01005", nse_symbol="RELIANCE", series="EQ", cas_class=CAS, snapshot_date=D,
        snapshot_sha256=H["snap"], index_sha256=H["index"], procedure_version=1, spec_sha256=H["spec"],
        impl_sha256=H["impl"], u24_status="unverified", u24_evidence_ref=None, stock_code="RELIND",
        icici_ref=IciciRef(D, H["snap"], H["row"]),
    )
    values.update(changes)
    return CasClassRow(**values)


def unresolved(sub_reason="snapshot_missing", **changes):
    values = dict(sub_reason=sub_reason, detail="why", session_date=D, procedure_version=1)
    values.update(changes)
    return CasUnresolved(**values)


def approval(version=1, prev=None, **changes):
    value = {
        "procedure_version": version, "spec_sha256": H["spec"], "impl_sha256": H["impl"], "u24_status": "unverified",
        "u24_evidence_ref": None, "operator_approval": {"line": "approved after reading the diff", "date": "2026-11-05"},
        "prev_sha256": prev,
    }
    value.update(changes)
    return value


def revocation(version, prev):
    return {"revokes": version, "operator_approval": {"line": "revoked", "date": "2026-11-06"}, "prev_sha256": prev}


def chain(*items):
    """Build entries with a correct hash chain from templates that omit ``prev_sha256``."""
    out, prev = [], None
    for item in items:
        entry = dict(item, prev_sha256=prev)
        out.append(entry)
        prev = entry_sha256(entry)
    return out


def log_of(entries):
    return {"schema": cas_class.CAS_PROCEDURE_SCHEMA, "entries": entries}


def test_cas_loader_no_network(tmp_path, monkeypatch):
    """A load of the committed log and of a fixture log must not trip the autouse socket refusal."""
    with pytest.raises(AssertionError, match="network used"):
        socket.socket()
    path = tmp_path / "log.json"
    path.write_text(json.dumps(log_of(chain(approval()))))
    monkeypatch.setattr(cas_class, "PROCEDURE_PATH", path)
    assert approved_entry(load_procedure_log(), 1) is not None


# ---- the committed log -------------------------------------------------------------------


def test_committed_log_is_a_valid_chain_and_the_old_table_is_gone():
    committed = cas_class.PROCEDURE_PATH
    assert committed.name == "nse_cas_procedure.json"
    parsed = parse_procedure_log(json.loads(committed.read_text(encoding="utf-8")))
    previous = None
    for entry in parsed.raw_entries:
        assert entry["prev_sha256"] == previous
        previous = entry_sha256(entry)
    assert not (committed.parent / "nse_cas_class.json").exists()
    for gone in ("CAS_TABLE_PATH", "CasClassUnavailable", "committed_cas_table", "load_cas_table", "resolve_cas_class"):
        assert not hasattr(cas_class, gone), gone


def test_the_scope_code_and_sub_reason_order():
    assert cas_class.SCOPE_UNKNOWN_CODE == "session_scope_unknown"
    assert len(SUB_REASONS) == len(set(SUB_REASONS)) == 10
    assert SUB_REASONS[0] == "snapshot_missing" and SUB_REASONS[-1] == "flag_conflict"


# ---- verdict types -----------------------------------------------------------------------


def test_cas_class_row_carries_the_provenance_and_has_no_default():
    value = row()
    assert (value.cas_class, value.procedure_version, value.u24_status) == (CAS, 1, "unverified")
    assert row(cas_class=NON_CAS).cas_class == NON_CAS
    with pytest.raises(TypeError):
        CasClassRow(isin="INE0REL01005")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "changes",
    [
        {"cas_class": "unknown"}, {"cas_class": ""}, {"cas_class": None}, {"cas_class": "CAS"},
        {"isin": ""}, {"nse_symbol": " "}, {"series": None},
        {"snapshot_date": datetime(2026, 10, 29)}, {"snapshot_date": "2026-10-29"},
        {"snapshot_sha256": "A" * 64}, {"index_sha256": "abc"}, {"spec_sha256": None}, {"impl_sha256": "g" * 64},
        {"procedure_version": True}, {"procedure_version": "1"}, {"u24_status": "assumed"}, {"icici_ref": "x"},
    ],
)
def test_cas_class_row_refuses_bad_fields(changes):
    with pytest.raises(InputError):
        row(**changes)


@pytest.mark.parametrize("sub_reason", SUB_REASONS)
def test_cas_unresolved_takes_exactly_the_named_sub_reasons(sub_reason):
    value = unresolved(sub_reason)
    assert not hasattr(value, "cas_class") and value.sub_reason == sub_reason


@pytest.mark.parametrize(
    "changes",
    [{"sub_reason": "other"}, {"sub_reason": ""}, {"detail": ""}, {"detail": None}, {"session_date": "2026-10-29"},
     {"session_date": datetime(2026, 10, 29)}, {"procedure_version": None}, {"procedure_version": True}],
)
def test_cas_unresolved_refuses_bad_fields(changes):
    with pytest.raises(InputError):
        unresolved(**changes)


def test_icici_ref_checks_its_fields():
    assert IciciRef(D, H["snap"], H["row"]).snapshot_date == D
    for args in ((datetime(2026, 10, 29), H["snap"], H["row"]), (D, "abc", H["row"]), (D, H["snap"], "G" * 64)):
        with pytest.raises(InputError):
            IciciRef(*args)


def test_cas_record_is_one_field_list_for_both_verdicts():
    resolved = cas_record(row(), "RELIANCE")
    assert set(resolved) == set(cas_record(unresolved(), "RELIANCE"))
    assert resolved["cas_class"] == CAS and resolved["sub_reason"] is None
    assert resolved["snapshot_date"] == "2026-10-29" and resolved["procedure_version"] == 1
    assert resolved["icici_ref"] == {"row_sha256": H["row"], "snapshot_date": "2026-10-29", "snapshot_sha256": H["snap"]}
    stopped = cas_record(unresolved("flag_unknown", detail="x", snapshot_sha256=H["snap"]), "RELIANCE")
    assert (stopped["cas_class"], stopped["sub_reason"], stopped["detail"]) == (None, "flag_unknown", "x")
    assert stopped["snapshot_sha256"] == H["snap"] and stopped["nse_symbol"] == "RELIANCE"
    missing = cas_record(None, "RELIANCE")
    assert missing["cas_class"] is None and missing["sub_reason"] is None and missing["nse_symbol"] == "RELIANCE"


# ---- the approval log --------------------------------------------------------------------


def test_cas_procedure_requires_operator_approval():
    """An entry without a complete operator approval line and date refuses the whole log."""
    missing = approval()
    del missing["operator_approval"]
    with pytest.raises(ScheduleError):
        parse_procedure_log(log_of([missing]))
    for bad in (None, {}, {"line": "", "date": "2026-11-05"}, {"line": "ok"}, {"date": "2026-11-05"},
                {"line": "  ", "date": "2026-11-05"}, {"line": "ok", "date": "not-a-date"},
                {"line": "ok", "date": "2026-11-05", "extra": 1}, {"line": " padded ", "date": "2026-11-05"}):
        with pytest.raises(ScheduleError):
            parse_procedure_log(log_of([approval(operator_approval=bad)]))
    assert parse_procedure_log(log_of([])).approvals == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"procedure_version": 0}, {"procedure_version": True}, {"procedure_version": "1"},
        {"spec_sha256": "abc"}, {"impl_sha256": "A" * 64}, {"impl_sha256": None},
        {"u24_status": "assumed"}, {"u24_status": "verified"},  # verified needs the evidence ref
        {"u24_status": "verified", "u24_evidence_ref": "short"},
        {"u24_evidence_ref": "a" * 64},  # a ref while unverified
        {"unknown": 1},
    ],
)
def test_approval_entry_fields_are_checked(changes):
    with pytest.raises(ScheduleError):
        parse_procedure_log(log_of(chain(approval(**changes))))


def test_approval_entry_needs_every_key():
    for key in cas_class._APPROVAL_ENTRY_KEYS:
        broken = approval()
        del broken[key]
        with pytest.raises(ScheduleError):
            parse_procedure_log(log_of([broken]))


def test_verified_u24_carries_the_saved_circular_hash():
    parsed = parse_procedure_log(log_of(chain(approval(u24_status="verified", u24_evidence_ref="f" * 64))))
    assert (parsed.approvals[0].u24_status, parsed.approvals[0].u24_evidence_ref) == ("verified", "f" * 64)


def test_log_chain_refuses_edits_removals_and_reordering():
    entries = chain(approval(1), approval(2), approval(3))
    assert [a.procedure_version for a in parse_procedure_log(log_of(entries)).approvals] == [1, 2, 3]
    edited = copy.deepcopy(entries)
    edited[0]["u24_status"] = "verified"
    edited[0]["u24_evidence_ref"] = "a" * 64
    with pytest.raises(ScheduleError):
        parse_procedure_log(log_of(edited))
    with pytest.raises(ScheduleError):
        parse_procedure_log(log_of([entries[0], entries[2]]))
    with pytest.raises(ScheduleError):
        parse_procedure_log(log_of([entries[1], entries[0], entries[2]]))
    with pytest.raises(ScheduleError):
        parse_procedure_log(log_of(chain(approval(1))[:0] + [approval(1, prev="a" * 64)]))  # first must be null


def test_versions_strictly_increase():
    for versions in ((1, 1), (2, 1)):
        with pytest.raises(ScheduleError):
            parse_procedure_log(log_of(chain(*(approval(v) for v in versions))))


def test_revocation_rules():
    first = chain(approval(1))
    entries = first + [revocation(1, entry_sha256(first[0]))]
    parsed = parse_procedure_log(log_of(entries))
    assert parsed.revoked == {1} and approved_entry(parsed, 1) is None
    # An unknown target, and a second revocation of the same version, refuse.
    with pytest.raises(ScheduleError):
        parse_procedure_log(log_of(first + [revocation(2, entry_sha256(first[0]))]))
    with pytest.raises(ScheduleError):
        parse_procedure_log(log_of(entries + [revocation(1, entry_sha256(entries[1]))]))
    for target in (True, "1", None):
        with pytest.raises(ScheduleError):
            parse_procedure_log(log_of(first + [revocation(target, entry_sha256(first[0]))]))


def test_approved_entry_lookup():
    parsed = parse_procedure_log(log_of(chain(approval(1), approval(2))))
    assert approved_entry(parsed, 2).procedure_version == 2
    assert approved_entry(parsed, 3) is None
    with pytest.raises(InputError):
        approved_entry(parsed, True)  # type: ignore[arg-type]
    with pytest.raises(InputError):
        approved_entry(parsed, "1")  # type: ignore[arg-type]


@pytest.mark.parametrize("changes", [{"schema": "growin.costs.cas_procedure/2"}, {"extra": 1}])
def test_file_level_refusals(changes):
    with pytest.raises(ScheduleError):
        parse_procedure_log(dict(log_of([]), **changes))
    for raw in ([], "x", {"schema": cas_class.CAS_PROCEDURE_SCHEMA}, log_of({})):
        with pytest.raises(ScheduleError):
            parse_procedure_log(raw)


def test_loader_reads_the_path_each_call_and_refuses_bad_json(tmp_path, monkeypatch):
    path = tmp_path / "log.json"
    monkeypatch.setattr(cas_class, "PROCEDURE_PATH", path)
    path.write_text(json.dumps(log_of(chain(approval()))))
    assert approved_entry(load_procedure_log(), 1) is not None
    entries = chain(approval())
    path.write_text(json.dumps(log_of(entries + [revocation(1, entry_sha256(entries[0]))])))
    assert approved_entry(load_procedure_log(), 1) is None  # a revocation takes effect at once
    path.write_text("not json")
    with pytest.raises(ScheduleError):
        load_procedure_log()
    path.write_text(json.dumps(log_of([])).replace('"schema":', '"schema": "x", "schema":', 1))
    with pytest.raises(ScheduleError):
        load_procedure_log()  # duplicate keys


def test_append_only_extension_unit():
    base = tuple(chain(approval(1), approval(2)))
    longer = tuple(chain(approval(1), approval(2), approval(3)))
    assert is_append_only_extension((), base) and is_append_only_extension(base, base)
    assert is_append_only_extension(base, longer)
    assert not is_append_only_extension(longer, base)
    assert not is_append_only_extension(base, (base[1], base[0]))
    assert not is_append_only_extension(base, (dict(base[0], u24_status="verified"), base[1]))


def test_log_entry_dict_round_trips():
    parsed = parse_procedure_log(log_of(chain(approval(1), approval(2))))
    for entry, raw in zip(parsed.approvals, parsed.raw_entries):
        assert entry.as_dict() == raw

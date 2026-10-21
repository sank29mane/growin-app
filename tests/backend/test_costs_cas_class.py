"""Dated CAS-class table: schema, loader and fail-closed resolver (64-02 Task 2b).

Every row here is a SYNTHETIC fixture. The committed table ships unpopulated; the operator
sources the real rows from a saved NSE publication later.
"""

from __future__ import annotations

import copy
import json
import socket
from datetime import date
from pathlib import Path

import pytest

from costs import cas_class
from costs.cas_class import (
    CAS_TABLE_PATH,
    CasClassUnavailable,
    committed_cas_table,
    load_cas_table,
    resolve_cas_class,
)
from costs.core import InputError, ScheduleError

SHA = "a" * 64
ISIN_A, ISIN_B = "INE0TEST0001", "INE0TEST0002"


def version(n=1, start="2026-08-03", end=None, rows=None, **changes):
    value = {
        "version": f"cas-test-v{n}",
        "revision": f"r{n}",
        "effective_from": start,
        "effective_to": end,
        "publication_id": f"PUB-TEST-{n}",
        "publication_date": "2026-07-30",
        "source_sha256": SHA,
        "operator_approval": {"line": "Approved by the operator after reading the saved file.", "date": "2026-07-31"},
        "rows": rows if rows is not None else [
            {"isin": ISIN_A, "symbol": "AAA", "class": "cas"},
            {"isin": ISIN_B, "symbol": "BBB", "class": "non_cas"},
        ],
    }
    value.update(changes)
    return value


def table_file(tmp_path: Path, versions, **changes) -> Path:
    raw = {"schema": cas_class.CAS_SCHEMA, "source_id": "test-source", "status": "TEST FIXTURE", "versions": versions}
    raw.update(changes)
    path = tmp_path / "cas.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def load(tmp_path, versions, **changes):
    return load_cas_table(table_file(tmp_path, versions, **changes))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network used by the CAS-class loader")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_cas_loader_no_network(tmp_path):
    """The autouse fixture refuses every socket. A load and a resolve, committed and fixture, must not trip it."""
    with pytest.raises(AssertionError, match="network used"):
        socket.socket()
    with pytest.raises(AssertionError, match="network used"):
        socket.create_connection(("127.0.0.1", 9))
    resolve_cas_class(ISIN_A, date(2026, 10, 6), table=load(tmp_path, [version()]))
    committed_cas_table()
    with pytest.raises(CasClassUnavailable):
        resolve_cas_class(ISIN_A, date(2026, 10, 6))


# ---- committed table ----------------------------------------------------------------------


def test_committed_table_ships_unpopulated():
    table = committed_cas_table()
    assert table.schema == cas_class.CAS_SCHEMA
    assert table.versions == ()
    assert table.status.startswith("UNPOPULATED")
    assert CAS_TABLE_PATH.read_text(encoding="utf-8").count('"isin"') == 0


@pytest.mark.parametrize("isin", [ISIN_A, "INE002A01018", "INE0TEST0003"])
@pytest.mark.parametrize("session", [date(2026, 8, 3), date(2026, 10, 6), date(2030, 1, 1)])
def test_committed_table_fails_closed_for_every_lookup(isin, session):
    with pytest.raises(CasClassUnavailable) as raised:
        resolve_cas_class(isin, session)
    assert raised.value.code == "session_scope_unknown"


# ---- resolver ------------------------------------------------------------------------------


def test_cas_row_resolves_by_isin_and_date(tmp_path):
    table = load(tmp_path, [version()])
    row = resolve_cas_class(ISIN_A, date(2026, 10, 6), table=table)
    assert (row.cas_class, row.symbol, row.session_date) == ("cas", "AAA", date(2026, 10, 6))
    assert (row.table_version, row.table_revision) == ("cas-test-v1", "r1")
    assert (row.publication_id, row.source_sha256) == ("PUB-TEST-1", SHA)
    assert row.approval_date == date(2026, 7, 31) and len(row.version_hash) == 64
    assert resolve_cas_class(ISIN_B, date(2026, 10, 6), table=table).cas_class == "non_cas"


def test_cas_no_default_class(tmp_path):
    """Plant 28: an ISIN with no row never gets a default class, on any date."""
    table = load(tmp_path, [version()])
    for isin in ("INE0TEST0009", "", "ine0test0001"):
        with pytest.raises(CasClassUnavailable):
            resolve_cas_class(isin, date(2026, 10, 6), table=table)
    with pytest.raises(CasClassUnavailable):
        resolve_cas_class(ISIN_A, date(2026, 8, 2), table=table)  # before the first version
    ended = load(tmp_path, [version(end="2026-09-30")])
    with pytest.raises(CasClassUnavailable):
        resolve_cas_class(ISIN_A, date(2026, 10, 1), table=ended)  # after the last, closed version


def test_resolver_inputs_are_checked(tmp_path):
    table = load(tmp_path, [version()])
    for isin, session in [(None, date(2026, 10, 6)), (ISIN_A, "2026-10-06"), (ISIN_A, None)]:
        with pytest.raises(CasClassUnavailable):
            resolve_cas_class(isin, session, table=table)  # type: ignore[arg-type]
    from datetime import datetime

    with pytest.raises(CasClassUnavailable):
        resolve_cas_class(ISIN_A, datetime(2026, 10, 6, 10, 0), table=table)


def test_edge_dates_pick_the_right_version(tmp_path):
    v1 = version(1, "2026-08-03", "2026-09-30", rows=[{"isin": ISIN_A, "symbol": "AAA", "class": "cas"}])
    v2 = version(2, "2026-10-01", None, rows=[{"isin": ISIN_A, "symbol": "AAA", "class": "non_cas"}])
    table = load(tmp_path, [v2, v1])  # file order does not matter
    assert resolve_cas_class(ISIN_A, date(2026, 9, 30), table=table).cas_class == "cas"
    assert resolve_cas_class(ISIN_A, date(2026, 10, 1), table=table).cas_class == "non_cas"
    assert resolve_cas_class(ISIN_A, date(2026, 8, 3), table=table).table_version == "cas-test-v1"


def test_cas_missing_row_raises(tmp_path):
    v1 = version(1, "2026-08-03", "2026-09-30")
    v2 = version(2, "2026-10-01", None, rows=[{"isin": ISIN_B, "symbol": "BBB", "class": "non_cas"}])
    table = load(tmp_path, [v1, v2])
    with pytest.raises(CasClassUnavailable):
        resolve_cas_class(ISIN_A, date(2026, 10, 6), table=table)


# ---- approval, hash and provenance ---------------------------------------------------------


def test_cas_version_requires_operator_approval(tmp_path):
    """Plant 29: a version without an operator approval line refuses to load."""
    missing = version()
    del missing["operator_approval"]
    with pytest.raises(ScheduleError):
        load(tmp_path, [missing])
    for approval in (None, {}, {"line": "", "date": "2026-07-31"}, {"line": "ok"}, {"date": "2026-07-31"},
                     {"line": "  ", "date": "2026-07-31"}, {"line": "ok", "date": "not-a-date"},
                     {"line": "ok", "date": "2026-07-31", "extra": 1}):
        with pytest.raises(ScheduleError):
            load(tmp_path, [version(operator_approval=approval)])


def test_cas_version_requires_source_and_sha256(tmp_path):
    """A version names its source publication and carries a lowercase 64-hex sha256 of the saved file."""
    for key in ("publication_id", "source_sha256"):
        broken = version()
        del broken[key]
        with pytest.raises(ScheduleError):
            load(tmp_path, [broken])
    for bad in ("", None, "abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65, 123):
        with pytest.raises(ScheduleError):
            load(tmp_path, [version(source_sha256=bad)])
    for bad in ("", None):
        with pytest.raises(ScheduleError):
            load(tmp_path, [version(publication_id=bad)])
    table = load(tmp_path, [version()])
    assert table.versions[0].source_sha256 == SHA


@pytest.mark.parametrize(
    "changes",
    [
        {"source_sha256": ""}, {"source_sha256": "abc"}, {"source_sha256": "A" * 64}, {"source_sha256": "g" * 64},
        {"publication_id": ""}, {"publication_date": "2026-13-01"}, {"version": ""}, {"revision": ""},
        {"effective_from": "08/03/2026"}, {"effective_to": "2026-07-01"},  # precedes effective_from
        {"unknown": 1},
    ],
)
def test_provenance_fields_are_required_and_checked(tmp_path, changes):
    with pytest.raises(ScheduleError):
        load(tmp_path, [version(**changes)])


def test_missing_provenance_keys(tmp_path):
    for key in ("publication_id", "publication_date", "source_sha256", "effective_from", "effective_to", "version"):
        broken = version()
        del broken[key]
        with pytest.raises(ScheduleError):
            load(tmp_path, [broken])


# ---- rows ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rows",
    [
        [],  # a version needs rows
        [{"isin": ISIN_A, "symbol": "AAA"}],  # no class
        [{"isin": ISIN_A, "symbol": "AAA", "class": "unknown"}],
        [{"isin": ISIN_A, "symbol": "AAA", "class": ""}],
        [{"isin": ISIN_A, "symbol": "AAA", "class": "CAS"}],
        [{"isin": "short", "symbol": "AAA", "class": "cas"}],
        [{"isin": "ine0test0001", "symbol": "AAA", "class": "cas"}],
        [{"isin": ISIN_A, "symbol": "", "class": "cas"}],
        [{"isin": ISIN_A, "symbol": "AAA", "class": "cas"}, {"isin": ISIN_A, "symbol": "AAA", "class": "non_cas"}],
        [{"isin": ISIN_A, "symbol": "AAA", "class": "cas", "extra": "x"}],
        ["not-an-object"],
    ],
)
def test_bad_rows_refuse_the_load(tmp_path, rows):
    with pytest.raises(ScheduleError):
        load(tmp_path, [version(rows=rows)])


def test_unverified_rows_refuse_the_load(tmp_path):
    for status in ("UNVERIFIED", "unverified", "pending", "", None):
        rows = [{"isin": ISIN_A, "symbol": "AAA", "class": "cas", "status": status}]
        with pytest.raises(ScheduleError):
            load(tmp_path, [version(rows=rows)])
    ok = [{"isin": ISIN_A, "symbol": "AAA", "class": "cas", "status": "verified"}]
    assert resolve_cas_class(ISIN_A, date(2026, 10, 6), table=load(tmp_path, [version(rows=ok)])).cas_class == "cas"


# ---- version chain -------------------------------------------------------------------------


def test_cas_version_dates_contiguous_no_overlap(tmp_path):
    base = dict(rows=[{"isin": ISIN_A, "symbol": "AAA", "class": "cas"}])
    load(tmp_path, [version(1, "2026-08-03", "2026-09-30", **base), version(2, "2026-10-01", None, **base)])
    with pytest.raises(ScheduleError):  # a gap
        load(tmp_path, [version(1, "2026-08-03", "2026-09-29", **base), version(2, "2026-10-01", None, **base)])
    with pytest.raises(ScheduleError):  # an overlap
        load(tmp_path, [version(1, "2026-08-03", "2026-10-01", **base), version(2, "2026-10-01", None, **base)])
    with pytest.raises(ScheduleError):  # an open-ended version that is not the last
        load(tmp_path, [version(1, "2026-08-03", None, **base), version(2, "2026-10-01", None, **base)])
    with pytest.raises(ScheduleError):  # duplicate id
        load(tmp_path, [version(1, "2026-08-03", "2026-09-30", **base), version(1, "2026-10-01", None, **base)])


# ---- file level ----------------------------------------------------------------------------


def test_unpopulated_file_loads_and_resolves_nothing(tmp_path):
    table = load(tmp_path, [])
    assert table.versions == ()
    with pytest.raises(CasClassUnavailable):
        resolve_cas_class(ISIN_A, date(2026, 10, 6), table=table)


@pytest.mark.parametrize(
    "changes",
    [{"schema": "growin.costs.cas_class/2"}, {"source_id": ""}, {"status": ""}, {"extra": 1}],
)
def test_file_level_refusals(tmp_path, changes):
    with pytest.raises(ScheduleError):
        load_cas_table(table_file(tmp_path, [version()], **changes))


def test_versions_must_be_a_list(tmp_path):
    path = table_file(tmp_path, [])
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["versions"] = {}
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ScheduleError):
        load_cas_table(path)


def test_duplicate_keys_and_floats_are_refused(tmp_path):
    path = table_file(tmp_path, [version()])
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"schema":', '"schema": "x", "schema":', 1), encoding="utf-8")
    with pytest.raises(ScheduleError):
        load_cas_table(path)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ScheduleError):
        load_cas_table(path)


def test_the_unavailable_error_is_an_input_error_with_the_scope_code():
    assert issubclass(CasClassUnavailable, InputError)
    assert CasClassUnavailable.code == cas_class.SCOPE_UNKNOWN_CODE == "session_scope_unknown"


def test_loader_does_not_mutate_or_cache_a_passed_table(tmp_path):
    raw = [version()]
    snapshot = copy.deepcopy(raw)
    table = load(tmp_path, raw)
    assert resolve_cas_class(ISIN_A, date(2026, 10, 6), table=table).cas_class == "cas"
    assert raw == snapshot
    with pytest.raises(CasClassUnavailable):
        resolve_cas_class(ISIN_A, date(2026, 10, 6))  # the committed table is still empty

"""Security master snapshot history, change diff, freshness gate and series-aware lookups."""

from datetime import date, datetime, timezone

import pytest

from pilot_data.core import PilotDataError, SourceDescriptor
from pilot_data.security_master import (
    ingest_security_master,
    latest_snapshot,
    lookup_isin,
    lookup_stock_code,
    master_rows,
    parse_security_master,
    parse_security_master_full,
    require_fresh_snapshot,
)
from pilot_data.store import PilotDataStore

import pilot_data_testkit as kit

D1, D2, D3 = date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)
FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)

TATMOT_OLD = kit.master_row(3456, "TATMOT", "EQ", "TATA MOTORS LTD", "0.01", "2", "INE155A01014", "TATAMOTORS")


def desc(name="NSEScripMaster.txt", fetched=FETCHED) -> SourceDescriptor:
    return SourceDescriptor(source="local_file", kind="security_master", locator=name, fetched_at=fetched)


def ingest(store, rows, snapshot_date, basis="test_fixture", fetched=FETCHED):
    return ingest_security_master(
        store, desc(fetched=fetched), kit.security_master_bytes(rows), snapshot_date=snapshot_date,
        snapshot_date_basis=basis,
    )


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


BASE = [kit.MASTER_RELIND, kit.MASTER_NIFBEE, kit.MASTER_HDFBAN, kit.MASTER_HDFWA2, kit.MASTER_ACRTEC]


def test_change_diff_records_isin_symbol_token_and_membership_changes(store):
    old_rows = BASE + [TATMOT_OLD, kit.MASTER_JAIBAL, dict(kit.MASTER_CANBAN, Token="10794")]
    new_rows = BASE + [
        kit.MASTER_TATMOT,  # same stock_code and series, new ISIN and new ExchangeCode
        kit.MASTER_CANBAN | {"Token": "99999"},  # token changed
        kit.master_row(5000, "NEWCO", "EQ", "NEW CO", "0.01", "1", "INE002A01018", "NEWCO"),  # added
    ]
    ingest(store, old_rows, D1)
    outcome = ingest(store, new_rows, D2)
    kinds = sorted((c.change_kind, c.stock_code) for c in outcome.changes)
    assert kinds == sorted(
        [
            ("stock_code_isin_changed", "TATMOT"),
            ("stock_code_symbol_changed", "TATMOT"),
            ("token_changed", "CANBAN"),
            ("row_removed", "JAIBAL"),
            ("row_added", "NEWCO"),
        ]
    )
    isin_change = next(c for c in outcome.changes if c.change_kind == "stock_code_isin_changed")
    assert (isin_change.old_value, isin_change.new_value) == ("INE155A01014", "INE155A01022")
    stored = store.query("SELECT change_kind, stock_code FROM security_master_changes ORDER BY change_kind, stock_code")
    assert len(stored) == 5
    assert outcome.prev_snapshot_sha256 is not None


def test_same_bytes_with_a_later_date_adds_a_snapshot_not_rows_or_changes(store):
    first = ingest(store, BASE, D1)
    second = ingest(store, BASE, D2)
    assert first.snapshot_sha256 == second.snapshot_sha256
    assert second.rows_inserted == 0 and second.changes == ()
    assert store.query("SELECT count(*) FROM security_master_snapshots")[0][0] == 2
    assert store.query("SELECT count(*) FROM security_master_rows")[0][0] == len(BASE)
    assert store.query("SELECT count(*) FROM security_master_changes")[0][0] == 0


def test_freshness_gate(store):
    with pytest.raises(PilotDataError) as missing:
        require_fresh_snapshot(store, today_ist=D3)
    assert missing.value.code == "security_master_missing"
    ingest(store, BASE, D2)
    with pytest.raises(PilotDataError) as stale:
        require_fresh_snapshot(store, today_ist=D3)
    assert stale.value.code == "security_master_stale"
    ingest(store, BASE + [TATMOT_OLD], D3)
    fresh = require_fresh_snapshot(store, today_ist=D3)
    assert fresh.snapshot_date == D3 and fresh.row_count == len(BASE) + 1


def test_future_snapshot_date_is_rejected(store):
    with pytest.raises(PilotDataError) as caught:
        ingest(store, BASE, date(2026, 10, 3))
    assert caught.value.code == "snapshot_date_invalid"


def test_latest_snapshot_respects_the_cutoff(store):
    ingest(store, BASE, D1)
    ingest(store, BASE + [TATMOT_OLD], D3)
    assert latest_snapshot(store, on_or_before=D2).snapshot_date == D1
    assert latest_snapshot(store, on_or_before=D3).snapshot_date == D3
    assert latest_snapshot(store, on_or_before=date(2020, 1, 1)) is None


def test_lookups_are_series_aware_and_never_return_dead_tokens(store):
    ingest(store, BASE, D3)
    snap = latest_snapshot(store, on_or_before=D3)
    assert lookup_isin(store, snap, "INE002A01018", series="EQ").stock_code == "RELIND"
    assert lookup_isin(store, snap, "INE040A01034", series="EQ").stock_code == "HDFBAN"
    assert lookup_isin(store, snap, "INE040A13013", series="W3").stock_code == "HDFWA2"
    assert lookup_isin(store, snap, "INE040A01034", series="W3") is None  # series must match
    assert lookup_isin(store, snap, "INE055L01013", series="BE") is None  # ACRTEC has token 0
    assert lookup_isin(store, snap, "INE002A01019", series="EQ") is None  # bad check digit
    assert lookup_stock_code(store, snap, "RELIND").isin == "INE002A01018"
    assert lookup_stock_code(store, snap, "HDFWA2").series == "W3"
    assert lookup_stock_code(store, snap, "ACRTEC") is None
    assert lookup_stock_code(store, snap, "NOPE") is None
    assert len(master_rows(store, snap)) == len(BASE)


def test_ambiguous_live_isin_fails_closed(store):
    twin = kit.master_row(4444, "RELTWN", "EQ", "TWIN", "0.01", "10", "INE002A01018", "RELTWIN")
    ingest(store, [kit.MASTER_RELIND, twin], D3)
    snap = latest_snapshot(store, on_or_before=D3)
    with pytest.raises(PilotDataError) as caught:
        lookup_isin(store, snap, "INE002A01018", series="EQ")
    assert caught.value.code == "isin_ambiguous"


def test_zip_and_text_inputs_give_identical_rows_and_schema_errors_fail_closed():
    text_rows = parse_security_master(kit.security_master_bytes(BASE))
    assert parse_security_master(kit.security_master_zip(BASE)) == text_rows
    no_isin_column = [name for name in kit.MASTER_HEADER_T if name != "ISINCode"]
    with pytest.raises(PilotDataError) as missing_column:
        parse_security_master(kit.security_master_text(BASE, header=no_isin_column).encode())
    assert missing_column.value.code == "security_master_schema_mismatch"
    text = kit.security_master_text(BASE)
    lines = text.splitlines()
    short = "\n".join([lines[0], lines[1].rsplit(",", 3)[0]] + lines[2:]) + "\n"
    with pytest.raises(PilotDataError) as short_row:
        parse_security_master(short.encode())
    assert short_row.value.code == "security_master_schema_mismatch"
    no_member = kit._zip_bytes({"other.txt": b"x"})
    with pytest.raises(PilotDataError) as wrong_zip:
        parse_security_master(no_member)
    assert wrong_zip.value.code == "security_master_schema_mismatch"


def test_rows_with_non_numeric_tokens_are_skipped_and_quarantined(store):
    odd = [kit.master_row(0, "NIFTY", "0", "NIFTY 50", "0.01", "1", "", "NIFTY") | {"Token": "NIFTY 50"},
           kit.master_row(0, "CHECEM", "EQ", "CHEMCON", "0.01", "1", "INE002A01018", "CHEMCON") | {"Token": "NA"}]
    parsed = parse_security_master_full(kit.security_master_bytes(BASE + odd))
    assert len(parsed.rows) == len(BASE) and [s.stock_code for s in parsed.skipped] == ["NIFTY", "CHECEM"]
    outcome = ingest(store, BASE + odd, D3)
    assert outcome.skipped_rows == 2 and outcome.row_count == len(BASE)
    reasons = store.query("SELECT reason_code, count(*) FROM quarantine_records GROUP BY reason_code")
    assert reasons == [("non_numeric_token", 2)]


def test_ticksize_is_stored_as_raw_text(store):
    ingest(store, [dict(kit.MASTER_RELIND, ticksize="0.0500")], D3)
    snap = latest_snapshot(store, on_or_before=D3)
    assert master_rows(store, snap)[0].tick_size_raw == "0.0500"

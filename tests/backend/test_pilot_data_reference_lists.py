"""Nifty 500 and Smallcap 250 lists plus ASM and GSM surveillance snapshots."""

import json
from datetime import date, datetime, timezone

import httpx
import pytest

from market_data.models import is_valid_isin
from pilot_data.constituents import (
    NIFTY500_URL,
    SMALLCAP250_URL,
    fetch_index_list,
    index_members,
    ingest_index_list,
    latest_index_list,
    parse_index_list,
)
from pilot_data.core import PilotDataError, SourceDescriptor
from pilot_data.nse_http import NseHttp
from pilot_data.store import PilotDataStore
from pilot_data.surveillance import (
    ASM_URL,
    GSM_URL,
    fetch_surveillance,
    first_snapshot_date,
    ingest_surveillance,
    parse_asm,
    parse_gsm,
    snapshot_for,
    surveillance_entries,
)

import pilot_data_testkit as kit

FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)


def make_isin(n: int) -> str:
    body = f"INE{n:08d}"
    for digit in range(10):
        if is_valid_isin(body + str(digit)):
            return body + str(digit)
    raise AssertionError("no check digit found")


def members(count: int, start: int = 1) -> list[dict[str, str]]:
    return [
        {"Company Name": f"Company {n}", "Industry": "Industry", "Symbol": f"SYM{n}", "Series": "EQ",
         "ISIN Code": make_isin(n)}
        for n in range(start, start + count)
    ]


def desc(kind="index_list_nifty500", source="nse_archive", locator="https://nsearchives.nseindia.com/x"):
    return SourceDescriptor(source=source, kind=kind, locator=locator, fetched_at=FETCHED)


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


# --------------------------------------------------------------------- index lists
def test_nifty500_excludes_the_placeholder_and_stores_500_members(store):
    payload = kit.index_list_csv(members(500) + [kit.DUMMY_INDEX_ROW])
    parsed = parse_index_list(payload, list_name="nifty500")
    assert len(parsed.members) == 500 and parsed.placeholders_excluded == ("DUMMYHEG",)
    ref = ingest_index_list(store, desc(), payload, list_name="nifty500")
    assert ref.member_count == 500 and ref.placeholders_excluded == ("DUMMYHEG",)
    latest = latest_index_list(store, "nifty500")
    assert latest.source_sha256 == ref.source_sha256 and latest.placeholders_excluded == ("DUMMYHEG",)
    stored = index_members(store, latest)
    assert len(stored) == 500 and all(is_valid_isin(m.isin) for m in stored)
    assert latest_index_list(store, "smallcap250") is None


def test_wrong_counts_duplicates_and_headers_fail_closed():
    with pytest.raises(PilotDataError) as short:
        parse_index_list(kit.index_list_csv(members(499) + [kit.DUMMY_INDEX_ROW]), list_name="nifty500")
    assert short.value.code == "index_list_count_mismatch"
    duplicated = members(500)
    duplicated[1] = dict(duplicated[1], **{"ISIN Code": duplicated[0]["ISIN Code"]})
    with pytest.raises(PilotDataError) as dup:
        parse_index_list(kit.index_list_csv(duplicated), list_name="nifty500")
    assert dup.value.code == "index_list_duplicate"
    bad_header = kit.index_list_csv(members(500), header=["Company", "Industry", "Symbol", "Series", "ISIN Code"])
    with pytest.raises(PilotDataError) as header:
        parse_index_list(bad_header, list_name="nifty500")
    assert header.value.code == "index_list_schema_mismatch"
    with pytest.raises(PilotDataError):
        parse_index_list(b"", list_name="nifty500")


def test_smallcap250_uses_its_own_expected_count(store):
    payload = kit.index_list_csv(members(250, start=1000) + [kit.DUMMY_INDEX_ROW])
    ref = ingest_index_list(store, desc("index_list_smallcap250"), payload, list_name="smallcap250")
    assert ref.member_count == 250
    with pytest.raises(PilotDataError) as caught:
        parse_index_list(kit.index_list_csv(members(500)), list_name="smallcap250")
    assert caught.value.code == "index_list_count_mismatch"


def test_a_bad_check_digit_row_is_a_placeholder_not_a_member():
    rows = members(500)
    rows.append({"Company Name": "Typo Co", "Industry": "x", "Symbol": "TYPO", "Series": "EQ",
                 "ISIN Code": "INE002A01019"})
    parsed = parse_index_list(kit.index_list_csv(rows), list_name="nifty500")
    assert parsed.placeholders_excluded == ("TYPO",)


# --------------------------------------------------------------------- surveillance
def test_asm_rows_parse_with_term_stage_and_dates():
    payload = kit.asm_json(
        [kit.asm_row("AAA", make_isin(1), stage="Stage II", when="01-Oct-2026")],
        [kit.asm_row("BBB", make_isin(2), stage="Stage I", when="30-Sep-2026"), kit.asm_row("CCC", None)],
    )
    parsed = parse_asm(payload)
    by_symbol = {e.nse_symbol: e for e in parsed.entries}
    assert (by_symbol["AAA"].term, by_symbol["AAA"].stage) == ("long", "Stage II")
    assert by_symbol["BBB"].term == "short"
    assert by_symbol["CCC"].isin is None and by_symbol["CCC"].nse_symbol == "CCC"
    assert parsed.effective_date == date(2026, 10, 1) and parsed.min_date == date(2026, 9, 30)
    assert parsed.extra_keys == ()


def test_gsm_rows_parse_stage_and_date_with_time():
    parsed = parse_gsm(kit.gsm_json([kit.gsm_row("AAA", make_isin(1), stage="LVIII", when="01-Oct-2026 08:07:02"),
                                     kit.gsm_row("BBB", "not-an-isin", stage="0")]))
    assert {e.stage for e in parsed.entries} == {"LVIII", "0"}
    assert parsed.effective_date == date(2026, 10, 1)
    assert [e.isin for e in parsed.entries if e.nse_symbol == "BBB"] == [None]
    assert all(e.term is None and e.list_name == "gsm" for e in parsed.entries)


def test_empty_and_malformed_surveillance_fails_closed():
    with pytest.raises(PilotDataError) as empty_asm:
        parse_asm(kit.asm_json([], []))
    assert empty_asm.value.code == "surveillance_empty"
    with pytest.raises(PilotDataError) as empty_gsm:
        parse_gsm(kit.gsm_json([]))
    assert empty_gsm.value.code == "surveillance_empty"
    missing_key = kit.asm_row("AAA", make_isin(1))
    del missing_key["asmTime"]
    with pytest.raises(PilotDataError) as schema:
        parse_asm(kit.asm_json([missing_key], []))
    assert schema.value.code == "surveillance_schema_mismatch"
    with pytest.raises(PilotDataError):
        parse_asm(b"not json")
    with pytest.raises(PilotDataError):
        parse_asm(json.dumps({"longterm": {"data": []}}).encode())
    with pytest.raises(PilotDataError):
        parse_gsm(json.dumps({"not": "a list"}).encode())


def test_extra_keys_are_tolerated_but_reported():
    row = dict(kit.gsm_row("AAA", make_isin(1)), surprise="x")
    parsed = parse_gsm(kit.gsm_json([row]))
    assert parsed.extra_keys == ("surprise",) and len(parsed.entries) == 1


def test_mixed_dates_use_latest_as_effective_and_earliest_as_min():
    parsed = parse_gsm(kit.gsm_json([kit.gsm_row("AAA", None, when="28-Sep-2026 01:00:00"),
                                     kit.gsm_row("BBB", None, when="02-Oct-2026 09:00:00")]))
    assert (parsed.effective_date, parsed.min_date) == (date(2026, 10, 2), date(2026, 9, 28))


def test_snapshots_store_entries_and_first_snapshot_date_needs_both_lists(store):
    asm_a = ingest_surveillance(
        store, desc("surveillance_asm", "nse_api", ASM_URL),
        kit.asm_json([kit.asm_row("AAA", make_isin(1), when="29-Sep-2026")], []), list_name="asm",
    )
    assert first_snapshot_date(store) is None  # only ASM so far
    ingest_surveillance(
        store, desc("surveillance_gsm", "nse_api", GSM_URL),
        kit.gsm_json([kit.gsm_row("AAA", make_isin(1), when="30-Sep-2026 08:00:00")]), list_name="gsm",
    )
    assert first_snapshot_date(store) is None  # dates differ
    ingest_surveillance(
        store, desc("surveillance_gsm", "nse_api", GSM_URL),
        kit.gsm_json([kit.gsm_row("AAA", make_isin(1), when="29-Sep-2026 08:00:00")]), list_name="gsm",
    )
    assert first_snapshot_date(store) == date(2026, 9, 29)
    assert snapshot_for(store, "asm", date(2026, 9, 29)).source_sha256 == asm_a.source_sha256
    assert snapshot_for(store, "asm", date(2026, 10, 1)) is None
    entries = surveillance_entries(store, asm_a)
    assert [(e.nse_symbol, e.term, e.isin) for e in entries] == [("AAA", "long", make_isin(1))]


# --------------------------------------------------------------------- fetch through MockTransport
def http_for(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get(str(request.url), 404)
        return httpx.Response(200, content=route) if isinstance(route, bytes) else httpx.Response(route, content=b"x")

    return NseHttp(httpx.Client(transport=httpx.MockTransport(handler)), min_interval_seconds=0.0)


def test_fetch_index_list_registers_archive_sources_with_the_right_kinds(store):
    http = http_for(
        {
            NIFTY500_URL: kit.index_list_csv(members(500) + [kit.DUMMY_INDEX_ROW]),
            SMALLCAP250_URL: kit.index_list_csv(members(250, start=1000) + [kit.DUMMY_INDEX_ROW]),
        }
    )
    fetch_index_list(store, http, "nifty500")
    fetch_index_list(store, http, "smallcap250")
    rows = store.query("SELECT source, kind, locator FROM source_files ORDER BY kind")
    assert rows == [("nse_archive", "index_list_nifty500", NIFTY500_URL),
                    ("nse_archive", "index_list_smallcap250", SMALLCAP250_URL)]
    with pytest.raises(PilotDataError) as caught:
        fetch_index_list(store, http_for({}), "nifty500")
    assert caught.value.code == "index_list_unavailable"


def test_fetch_surveillance_registers_api_sources_and_fails_closed_on_bad_bodies(store):
    http = http_for(
        {
            ASM_URL: kit.asm_json([kit.asm_row("AAA", make_isin(1))], []),
            GSM_URL: kit.gsm_json([kit.gsm_row("AAA", make_isin(1))]),
        }
    )
    asm_ref, gsm_ref = fetch_surveillance(store, http)
    assert (asm_ref.list_name, gsm_ref.list_name) == ("asm", "gsm")
    assert store.query("SELECT source, kind FROM source_files ORDER BY kind") == [
        ("nse_api", "surveillance_asm"), ("nse_api", "surveillance_gsm")]
    with pytest.raises(PilotDataError) as unavailable:
        fetch_surveillance(store, http_for({}))
    assert unavailable.value.code == "surveillance_unavailable"
    with pytest.raises(PilotDataError) as not_json:
        fetch_surveillance(store, http_for({ASM_URL: b"<html>blocked</html>"}))
    assert not_json.value.code == "nse_unexpected_content"
    with pytest.raises(PilotDataError) as refused:
        fetch_surveillance(store, http_for({ASM_URL: 403}))
    assert refused.value.code == "nse_fetch_refused"

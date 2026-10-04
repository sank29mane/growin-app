"""Legacy CM and PR parsers plus same-date cross-format consistency quarantine."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from pilot_data.bhavcopy import (
    CM_LEGACY_HEADER,
    check_same_date_consistency,
    ingest_cm_legacy,
    ingest_pr_zip,
    ingest_udiff,
    parse_cm_legacy,
    parse_pr_zip,
    parse_udiff,
    primary_bars_on,
)
from pilot_data.core import PilotDataError, SourceDescriptor
from pilot_data.store import PilotDataStore

import pilot_data_testkit as kit

FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
D14, D15 = date(2024, 5, 14), date(2024, 5, 15)


def desc(kind: str) -> SourceDescriptor:
    return SourceDescriptor(source="nse_archive", kind=kind, locator=f"https://nsearchives.nseindia.com/{kind}",
                            fetched_at=FETCHED)


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


def test_canbk_legacy_rows_parse_with_isin_prev_close_and_last():
    first = parse_cm_legacy(kit.cm_legacy_zip(D14, [kit.CM_CANBK_20240514]), expected_trade_date=D14)
    second = parse_cm_legacy(kit.cm_legacy_zip(D15, [kit.CM_CANBK_20240515]), expected_trade_date=D15)
    a, b = first.bars[0], second.bars[0]
    assert (a.file_kind, a.isin, a.token, a.nse_symbol, a.series) == ("cm_legacy", "INE476A01014", None, "CANBK", "EQ")
    assert (b.isin, b.prev_close, b.last, b.close) == ("INE476A01022", Decimal("566.55"), Decimal("119.1"), Decimal("119"))
    assert a.last == Decimal("568.95") and a.volume == 9219466 and a.trades == 135998


def test_legacy_date_and_schema_mismatches_fail_closed():
    with pytest.raises(PilotDataError) as wrong_date:
        parse_cm_legacy(kit.cm_legacy_zip(D14, [kit.CM_CANBK_20240515]), expected_trade_date=D14)
    assert wrong_date.value.code == "bhavcopy_date_mismatch"
    member = kit.cm_legacy_member_name(D14)
    no_trailing = list(CM_LEGACY_HEADER[:-1])
    renamed = list(CM_LEGACY_HEADER)
    renamed[2] = "OPENING"
    for header in (no_trailing, renamed):
        text = kit.cm_legacy_csv_text(D14, [kit.CM_CANBK_20240514], header=header)
        with pytest.raises(PilotDataError) as caught:
            parse_cm_legacy(kit._zip_bytes({member: text.encode()}), expected_trade_date=D14)
        assert caught.value.code == "bhavcopy_schema_mismatch"
    with pytest.raises(PilotDataError) as wrong_member:
        parse_cm_legacy(kit._zip_bytes({"other.csv": b"x"}), expected_trade_date=D14)
    assert wrong_member.value.code == "bhavcopy_member_unexpected"


def test_invalid_legacy_ohlc_is_quarantined_not_stored():
    bad = dict(kit.CM_CANBK_20240514, HIGH="500")
    parsed = parse_cm_legacy(kit.cm_legacy_zip(D14, [bad]), expected_trade_date=D14)
    assert parsed.bars == ()
    assert [q.reason_code for q in parsed.parse_quarantine_inputs] == ["invalid_ohlc"]


def pr_bytes(day, pd_rows=None, bc_rows=None, etf_rows=None):
    return kit.pr_zip(
        day,
        pd_rows if pd_rows is not None else [
            kit.pd_index_row(),
            kit.pd_row("CANBK", "EQ", "555.4", "569", "553.55", "566.55", prev_close="549.35", volume="9219466",
                       value="5199036379.85", trades="135998"),
            kit.pd_row("ABC", "BE", "10.00", "11.00", ".50", "10.50", mkt="G"),
        ],
        bc_rows if bc_rows is not None else [
            kit.bc_row("EQ", "CANBK", "CANARA BANK", "FVSPLT FRM RS 10 TO RS 2", ex_date=date(2024, 5, 15),
                       record_date=date(2024, 5, 16)),
        ],
        etf_rows if etf_rows is not None else [kit.etf_row("NIFTYBEES", "NIP IND ETF NIFTY BEES", "NIFTY 50")],
    )


def test_pr_zip_parses_pd_bc_and_etf():
    day = date(2024, 5, 2)
    bundle = parse_pr_zip(pr_bytes(day), expected_trade_date=day)
    symbols = [(b.nse_symbol, b.series, b.file_kind, b.isin) for b in bundle.pd_bars]
    assert symbols == [("CANBK", "EQ", "pr_pd", None), ("ABC", "BE", "pr_pd", None)]
    canbk = bundle.pd_bars[0]
    assert (canbk.prev_close, canbk.volume, canbk.traded_value, canbk.trades) == (
        Decimal("549.35"), 9219466, Decimal("5199036379.85"), 135998)
    ca = bundle.ca_rows[0]
    assert (ca.series, ca.nse_symbol, ca.purpose_raw) == ("EQ", "CANBK", "FVSPLT FRM RS 10 TO RS 2")
    assert ca.ex_date == date(2024, 5, 15) and ca.record_date == date(2024, 5, 16)
    assert ca.bc_start is None and ca.bc_end is None and ca.nd_start is None
    assert bundle.etf_rows[0].underlying == "NIFTY 50" and bundle.etf_rows[0].nse_symbol == "NIFTYBEES"
    assert not bundle.bc_missing and not bundle.etf_missing


def test_pr_zip_skips_index_rows_and_keeps_g_series_rows():
    day = date(2024, 5, 2)
    bundle = parse_pr_zip(pr_bytes(day), expected_trade_date=day)
    names = {(b.nse_symbol, b.series) for b in bundle.pd_bars}
    assert ("CANBK", "EQ") in names
    assert ("", "") not in names and all(b.nse_symbol for b in bundle.pd_bars)
    # the G row has a low of .50 which is a valid, low-priced bar; it is kept
    assert ("ABC", "BE") in names


def test_pr_section_header_rows_are_skipped_but_unknown_mkt_securities_are_quarantined():
    day = date(2024, 5, 2)
    rows = [
        {"MKT": "", "SERIES": "BT", "SECURITY": "TRADE FOR TRADE STOCKS", "IND_SEC": "N"},  # real section header
        {"MKT": "", "SERIES": "", "SECURITY": "EXCHANGE TRADED FUND"},
        kit.pd_row("CANBK", "EQ", "555.4", "569", "553.55", "566.55"),
        kit.pd_row("ODD", "EQ", "10", "11", "9", "10", mkt="X"),
        {"MKT": "N", "SERIES": "EQ", "SYMBOL": "", "OPEN_PRICE": "1"},
    ]
    bundle = parse_pr_zip(kit.pr_zip(day, rows, [], []), expected_trade_date=day)
    assert [b.nse_symbol for b in bundle.pd_bars] == ["CANBK"]
    assert sorted(q.reason_code for q in bundle.parse_quarantine_inputs) == ["invalid_ohlc", "invalid_row"]


def test_pr_member_rules():
    day = date(2024, 5, 2)
    only_bc = kit._zip_bytes({kit.pr_member_names(day)[1]: kit.bc_csv_text([]).encode()})
    with pytest.raises(PilotDataError) as caught:
        parse_pr_zip(only_bc, expected_trade_date=day)
    assert caught.value.code == "pr_member_missing"
    pd_only = kit._zip_bytes({kit.pr_member_names(day)[0].upper(): kit.pd_csv_text([kit.pd_index_row()]).encode()})
    bundle = parse_pr_zip(pd_only, expected_trade_date=day)
    assert bundle.bc_missing and bundle.etf_missing and bundle.pd_bars == ()


def test_pr_header_mismatch_fails_closed():
    day = date(2024, 5, 2)
    text = kit.pd_csv_text([kit.pd_index_row()]).replace("SERIES", "SERIES_X", 1)
    with pytest.raises(PilotDataError) as caught:
        parse_pr_zip(kit._zip_bytes({kit.pr_member_names(day)[0]: text.encode()}), expected_trade_date=day)
    assert caught.value.code == "bhavcopy_schema_mismatch"


# ------------------------------------------------------------ consistency
D = date(2025, 1, 2)


def u(symbol, series, isin, o, h, l, c, volume="1000"):
    return kit.udiff_row(symbol, series, isin, o, h, l, c, trade_date=D, volume=volume)


def p(symbol, series, o, h, l, c, volume="1000"):
    return kit.pd_row(symbol, series, o, h, l, c, volume=volume)


def consistency_store(store):
    ingest_udiff(
        store, desc("udiff_cm"),
        kit.udiff_zip(
            D,
            [
                u("AAA", "EQ", "INE002A01018", "100", "110", "90", "105"),
                u("BBB", "EQ", "INE476A01014", "100", "110", "90", "105"),
                u("DDD", "EQ", "INE155A01022", "100", "110", "90", "105"),
                u("EEE", "EQ", "INE002A01019", "100", "110", "90", "105"),
                u("FFF", "SM", "INE040A01034", "100", "110", "90", "105"),
            ],
        ),
        trade_date=D,
    )
    ingest_pr_zip(
        store, desc("pr_zip"),
        kit.pr_zip(
            D,
            [
                p("AAA", "EQ", "100", "110", "90", "105"),
                p("BBB", "EQ", "100", "110", "90", "106"),
                p("CCC", "EQ", "100", "110", "90", "105"),
                p("EEE", "EQ", "100", "110", "90", "105"),
            ],
            [], [],
        ),
        trade_date=D,
    )
    ingest_cm_legacy(
        store, desc("cm_legacy"),
        kit.cm_legacy_zip(
            D,
            [
                kit.cm_legacy_row("AAA", "EQ", "INE002A01018", "100", "110", "90", "104", trade_date=D),
                kit.cm_legacy_row("BBB", "EQ", "INE476A01014", "100", "110", "90", "105", trade_date=D),
                kit.cm_legacy_row("GGG", "EQ", "INE091G01026", "100", "110", "90", "105", trade_date=D),
            ],
        ),
        trade_date=D,
    )


def reasons(records):
    return sorted((r.reason_code, r.nse_symbol) for r in records)


def test_consistency_records_every_disagreement(store):
    consistency_store(store)
    records = check_same_date_consistency(store, D, workspace="india")
    assert reasons(records) == sorted(
        [
            ("pd_primary_mismatch", "BBB"),
            ("pd_without_primary", "CCC"),
            ("primary_without_pd", "DDD"),
            ("isin_invalid", "EEE"),
            ("udiff_cm_mismatch", "AAA"),
            ("udiff_cm_mismatch", "DDD"),
            ("udiff_cm_mismatch", "EEE"),
            ("udiff_cm_mismatch", "FFF"),
            ("udiff_cm_mismatch", "GGG"),
        ]
    )
    assert all(r.check == "bhavcopy_consistency" and r.scope == "raw" and r.date_from == D for r in records)
    mismatch = next(r for r in records if r.reason_code == "pd_primary_mismatch")
    assert mismatch.isin == "INE476A01014" and len(mismatch.evidence_sha256s) == 2
    assert mismatch.detail["fields"] == "close"


def test_consistency_rerun_inserts_nothing_new(store):
    consistency_store(store)
    check_same_date_consistency(store, D, workspace="india")
    before = store.query("SELECT count(*) FROM quarantine_records WHERE check_name = 'bhavcopy_consistency'")[0][0]
    again = check_same_date_consistency(store, D, workspace="india")
    after = store.query("SELECT count(*) FROM quarantine_records WHERE check_name = 'bhavcopy_consistency'")[0][0]
    assert before == after and len(again) == before


def test_primary_bars_prefer_udiff_else_legacy(store):
    consistency_store(store)
    assert {b.nse_symbol for b in primary_bars_on(store, D)} == {"AAA", "BBB", "DDD", "EEE", "FFF"}
    assert all(b.file_kind == "udiff" for b in primary_bars_on(store, D))
    ingest_cm_legacy(store, desc("cm_legacy"), kit.cm_legacy_zip(D14, [kit.CM_CANBK_20240514]), trade_date=D14)
    assert [b.file_kind for b in primary_bars_on(store, D14)] == ["cm_legacy"]
    assert primary_bars_on(store, date(2020, 1, 1)) == []


def test_ingest_registers_files_and_ca_rows(store):
    day = date(2024, 5, 2)
    outcome = ingest_pr_zip(store, desc("pr_zip"), pr_bytes(day), trade_date=day)
    assert outcome.ca_rows == 1 and outcome.etf_rows == 1 and not outcome.bc_missing
    assert store.query("SELECT file_kind, row_count FROM bhavcopy_files") == [("pr_zip", 2)]
    assert store.query("SELECT purpose_raw FROM bhavcopy_ca_raw") == [("FVSPLT FRM RS 10 TO RS 2",)]
    again = ingest_pr_zip(store, desc("pr_zip"), pr_bytes(day), trade_date=day)
    assert again.pd_inserted == 0 and again.pd_identical == 2
    assert store.query("SELECT count(*) FROM bhavcopy_ca_raw")[0][0] == 1


# Early UDiFF header: Rsvd01..Rsvd04 plus a trailing comma, rows still 34 fields.
# Row taken from the real BhavCopy_NSE_CM_0_0_0_20240101_F_0000.csv.
_EARLY_HEADER_LINE = ",".join(kit.UDIFF_HEADER_T[:-4]) + ",Rsvd01,Rsvd02,Rsvd03,Rsvd04,"
_EARLY_ROW_ZOTA = (
    "2024-01-01,2024-01-01,CM,NSE,STK,11394,INE358U01012,ZOTA,EQ,,,,,ZOTA HEALTH CARE LIMITED,"
    "473.00,483.95,471.45,480.60,480.95,472.45,,480.60,,,20854,9941670.15,1906,F1,1,,,,,"
)


def _early_udiff(header_line: str) -> bytes:
    text = f"{header_line}\n{_EARLY_ROW_ZOTA}\n"
    return kit._zip_bytes({kit.udiff_member_name(date(2024, 1, 1)): text.encode("utf-8")})


def test_early_udiff_header_variant_parses():
    assert len(_EARLY_ROW_ZOTA.split(",")) == len(kit.UDIFF_HEADER_T)
    parsed = parse_udiff(_early_udiff(_EARLY_HEADER_LINE), expected_trade_date=date(2024, 1, 1))
    (bar,) = parsed.bars
    assert (bar.isin, bar.open, bar.high, bar.low, bar.close, bar.prev_close) == (
        "INE358U01012", Decimal("473.00"), Decimal("483.95"), Decimal("471.45"), Decimal("480.60"), Decimal("472.45")
    )


@pytest.mark.parametrize(
    "header_line",
    [
        _EARLY_HEADER_LINE.rstrip(","),  # padded names without the trailing comma
        ",".join(kit.UDIFF_HEADER_T) + ",",  # current names with a trailing comma
        _EARLY_HEADER_LINE.replace("Rsvd04", "Rsvd05"),
    ],
)
def test_other_udiff_header_variants_still_fail_closed(header_line):
    with pytest.raises(PilotDataError) as caught:
        parse_udiff(_early_udiff(header_line), expected_trade_date=date(2024, 1, 1))
    assert caught.value.code == "bhavcopy_schema_mismatch"


# From July 2025 NSE's PR etf member carries a Windows-1252 en dash (0x96)
# in an index name, e.g. "NIFTY 50 INDEX \x96 TRI".
def _pr_with_etf_bytes(day: date, etf_bytes: bytes) -> bytes:
    pd_name, bc_name, etf_name = kit.pr_member_names(day)
    return kit._zip_bytes(
        {
            pd_name: kit.pd_csv_text([kit.pd_row("CANBK", "EQ", "555.4", "569", "553.55", "566.55")]).encode("utf-8"),
            bc_name: kit.bc_csv_text([]).encode("utf-8"),
            etf_name: etf_bytes,
        }
    )


def test_pr_etf_member_in_windows_1252_parses():
    day = date(2025, 7, 23)
    text = kit.etf_csv_text([kit.etf_row("NIFTYBEES", "NIP IND ETF NIFTY BEES", "NIFTY 50 INDEX – TRI")])
    bundle = parse_pr_zip(_pr_with_etf_bytes(day, text.encode("cp1252")), expected_trade_date=day)
    assert [b.nse_symbol for b in bundle.pd_bars] == ["CANBK"]
    assert [r.underlying for r in bundle.etf_rows] == ["NIFTY 50 INDEX – TRI"]


def test_pr_member_neither_utf8_nor_windows_1252_still_fails_closed():
    day = date(2025, 7, 23)
    text = kit.etf_csv_text([kit.etf_row("NIFTYBEES", "NIP IND ETF NIFTY BEES", "X")]).encode("utf-8")
    bad = text.replace(b",X", b",\x81\xff")  # 0x81 is undefined in Windows-1252
    with pytest.raises(PilotDataError) as caught:
        parse_pr_zip(_pr_with_etf_bytes(day, bad), expected_trade_date=day)
    assert caught.value.code == "bhavcopy_schema_mismatch"


# From November 2025 NSE names PR members in lower case with a four-digit year
# (pd03112025.csv) and writes Bc dates as yyyy-mm-dd.
_BC_HEADER_LINE = "SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE"


def _pr_nov2025(day: date, *, bc_line: str, extra: dict[str, bytes] | None = None) -> bytes:
    stamp = f"{day:%d%m%Y}"
    members = {
        f"pd{stamp}.csv": kit.pd_csv_text([kit.pd_row("CANBK", "EQ", "555.4", "569", "553.55", "566.55")]).encode(),
        f"bc{stamp}.csv": f"{_BC_HEADER_LINE}\n{bc_line}\n".encode(),
        f"etf{stamp}.csv": kit.etf_csv_text([kit.etf_row("NIFTYBEES", "NIP IND ETF NIFTY BEES", "NIFTY 50")]).encode(),
    }
    members.update(extra or {})
    return kit._zip_bytes(members)


def test_pr_november_2025_member_names_and_iso_bc_dates_parse():
    day = date(2025, 11, 3)
    bc = "EQ,CANBK,Canara Bank,2025-11-14,,,2025-11-14,,,DIVIDEND - RS 4 PER SHARE"
    bundle = parse_pr_zip(_pr_nov2025(day, bc_line=bc), expected_trade_date=day)
    assert [b.nse_symbol for b in bundle.pd_bars] == ["CANBK"]
    assert [r.underlying for r in bundle.etf_rows] == ["NIFTY 50"]
    (ca,) = bundle.ca_rows
    assert (ca.ex_date, ca.record_date) == (date(2025, 11, 14), date(2025, 11, 14))


def test_pr_with_both_member_name_forms_is_refused():
    day = date(2025, 11, 3)
    old_pd = kit.pd_csv_text([kit.pd_row("CANBK", "EQ", "1", "1", "1", "1")]).encode()
    content = _pr_nov2025(day, bc_line="EQ,CANBK,Canara Bank,,,,,,,AGM", extra={f"Pd{day:%d%m%y}.csv": old_pd})
    with pytest.raises(PilotDataError) as caught:
        parse_pr_zip(content, expected_trade_date=day)
    assert caught.value.code == "bhavcopy_schema_mismatch"


@pytest.mark.parametrize("bad", ["2025-13-01", "2025/11/14", "14-11-2025"])
def test_pr_bc_bad_dates_are_not_parsed_as_dates(bad):
    day = date(2025, 11, 3)
    bc = f"EQ,CANBK,Canara Bank,{bad},,,{bad},,,DIVIDEND - RS 4 PER SHARE"
    try:
        bundle = parse_pr_zip(_pr_nov2025(day, bc_line=bc), expected_trade_date=day)
    except PilotDataError as exc:
        assert exc.code in {"date_invalid", "bhavcopy_schema_mismatch"}
        return
    # Rows with a bad date are quarantined, never stored with a guessed date.
    assert bundle.ca_rows == () and bundle.parse_quarantine_inputs

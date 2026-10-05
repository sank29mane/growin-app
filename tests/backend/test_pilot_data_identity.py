"""ISIN identity, the IndiaListedSecurity sibling model, and the testkit's format builders."""

import csv
import io
import json
import zipfile
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from market_data.models import IndiaInstrument, IndiaListedSecurity, is_valid_isin
from pilot_data.core import PilotDataError, SourceDescriptor
from pilot_data.security_master import ingest_security_master, lineage_from_master, parse_security_master
from pilot_data.store import PilotDataStore

import pilot_data_testkit as kit

VALID = ["INE002A01018", "INF204KB14I2", "INE155A01022", "INE091G01018", "INE091G01026", "INE476A01014",
         "INE476A01022"]


@pytest.mark.parametrize("isin", VALID)
def test_valid_isins_pass(isin):
    assert is_valid_isin(isin)


@pytest.mark.parametrize("isin", ["INE002A01019", "DUM545A01024", "IN0020170091", "", "ine002a01018",
                                  "INE002A0101", "INE002A010188"])
def test_invalid_isins_fail(isin):
    assert not is_valid_isin(isin)


# Real NSE DVR ISINs (Future Enterprises, Jain Irrigation, Tata Motors); IN9 prefix, valid ISO 6166 check digit.
DVR = ["IN9623B01058", "IN9175A01010", "IN9155A01020"]


@pytest.mark.parametrize("isin", DVR)
def test_dvr_in9_isins_pass(isin):
    assert is_valid_isin(isin)


@pytest.mark.parametrize("isin", ["IN9623B01059", "IN9175A01011", "IN9155A01021", "IN9155A0102", "IN9155A010200",
                                  "in9155a01020", "IN8155A01020", "IN7155A01020", "IN0155A01020", "INA155A01020"])
def test_in9_with_bad_check_digit_or_other_prefix_fails(isin):
    assert not is_valid_isin(isin)


def test_non_string_isin_is_invalid():
    assert not is_valid_isin(None)  # type: ignore[arg-type]


def security(**overrides):
    values = {"workspace": "india", "symbol": "RELIANCE", "series": "EQ", "isin": "INE002A01018"}
    values.update(overrides)
    return IndiaListedSecurity(**values)


def test_listed_security_accepts_valid_isin_and_builds_key():
    item = security()
    assert item.key == "india:NSE:CASH:INE002A01018:EQ"
    assert item.instrument() == IndiaInstrument(symbol="RELIANCE")
    assert {security(isin=isin).isin for isin in VALID} == set(VALID)


@pytest.mark.parametrize("isin", ["INE002A01019", "DUM545A01024", "IN0020170091"])
def test_listed_security_rejects_invalid_isin(isin):
    with pytest.raises(ValidationError, match="invalid ISIN"):
        security(isin=isin)


@pytest.mark.parametrize("isin", DVR)
def test_listed_security_accepts_dvr_isin(isin):
    assert security(symbol="TATAMTRDVR", isin=isin).key == f"india:NSE:CASH:{isin}:EQ"


def test_listed_security_rejects_dvr_isin_with_bad_check_digit():
    with pytest.raises(ValidationError, match="invalid ISIN"):
        security(symbol="TATAMTRDVR", isin="IN9155A01021")


def test_listed_security_requires_workspace_and_is_frozen():
    with pytest.raises(ValidationError):
        IndiaListedSecurity(symbol="RELIANCE", series="EQ", isin="INE002A01018")
    with pytest.raises(ValidationError):
        security(workspace="uk")
    with pytest.raises(ValidationError):
        security(series="EQUITY")
    with pytest.raises(ValidationError):
        security().symbol = "TCS"  # type: ignore[misc]


FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)


def ingest(store, rows):
    return ingest_security_master(
        store,
        SourceDescriptor(source="local_file", kind="security_master", locator="NSEScripMaster.txt",
                         fetched_at=FETCHED),
        kit.security_master_bytes(rows), snapshot_date=date(2025, 1, 1), snapshot_date_basis="test_fixture",
    )


def lineage(store, code):
    return lineage_from_master(store, stock_code=code, as_of=date(2025, 1, 2), window_start=date(2025, 1, 2),
                               window_end=date(2025, 1, 3), workspace="india")


def test_lineage_from_master_validates_check_digit(tmp_path):
    bad = dict(kit.MASTER_RELIND, ISINCode="INE002A01019")
    with PilotDataStore(tmp_path / "a", workspace="india") as store:
        ingest(store, [bad])
        with pytest.raises(PilotDataError) as caught:
            lineage(store, "RELIND")
        assert caught.value.code == "isin_invalid"
    with PilotDataStore(tmp_path / "b", workspace="india") as store:
        ingest(store, kit.MASTER_SAMPLE_ROWS)
        result = lineage(store, "RELIND")
        assert result.anchor_isin == "INE002A01018" and result.anchor_series == "EQ"
        assert result.isin_on(date(2025, 1, 2)) == "INE002A01018"
        assert result.isin_on(date(2025, 1, 4)) is None
        with pytest.raises(PilotDataError) as dead:
            lineage(store, "ACRTEC")
        assert dead.value.code == "stock_code_unmapped"


# ------------------------------------------------------------ testkit format fidelity
def test_security_master_builder_matches_the_verified_layout():
    assert len(kit.MASTER_HEADER_T) == 61
    text = kit.security_master_text([kit.MASTER_RELIND])
    header_line, row_line = text.splitlines()
    assert header_line.startswith('"Token", "ShortName", "Series", "CompanyName", "ticksize"')
    assert row_line.startswith('"2885","RELIND","EQ","RELIANCE INDUSTRIES","0.01"')
    rows = parse_security_master(text.encode("utf-8"))
    assert (rows[0].token, rows[0].stock_code, rows[0].isin, rows[0].nse_symbol) == (
        2885, "RELIND", "INE002A01018", "RELIANCE")
    assert parse_security_master(kit.security_master_zip([kit.MASTER_RELIND])) == rows


def test_cm_legacy_builder_header_member_and_rows():
    day = date(2024, 5, 14)
    payload = kit.cm_legacy_zip(day, [kit.CM_CANBK_20240514])
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == ["cm14MAY2024bhav.csv"]
        lines = archive.read("cm14MAY2024bhav.csv").decode().splitlines()
    assert lines[0] == "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,"
    assert lines[1] == "CANBK,EQ,555.4,569,553.55,566.55,568.95,549.35,9219466,5199036379.85,14-MAY-2024,135998,INE476A01014,"
    assert len(next(csv.reader([lines[0]]))) == 14


def test_pr_builder_member_names_headers_and_padding():
    day = date(2024, 5, 2)
    payload = kit.pr_zip(
        day,
        [kit.pd_index_row(), kit.pd_row("CANBK", "EQ", "555.4", "569", "553.55", "566.55")],
        [kit.bc_row("EQ", "CANBK", "CANARA BANK", "FVSPLT FRM RS 10 TO RS 2", ex_date=date(2024, 5, 15))],
        [kit.etf_row("NIFTYBEES", "NIP IND ETF NIFTY BEES", "NIFTY 50")],
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        assert {"Pd020524.csv", "Bc020524.csv", "etf020524.csv"} <= set(names)
        pd_lines = archive.read("Pd020524.csv").decode().splitlines()
        bc_lines = archive.read("Bc020524.csv").decode().splitlines()
        etf_lines = archive.read("etf020524.csv").decode().splitlines()
    assert pd_lines[0] == ",".join(kit.PD_HEADER_T) and pd_lines[0].startswith("MKT,SERIES,SYMBOL,SECURITY,PREV_CL_PR")
    assert pd_lines[2].split(",")[4].startswith(" ")  # numeric values are left-padded
    assert bc_lines[0] == "SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE"
    cells = bc_lines[1].split(",")
    assert cells[3] == " " and cells[6] == "15/05/2024"  # blank is one space, dates are dd/mm/yyyy
    assert cells[-1].startswith("FVSPLT FRM RS 10 TO RS 2") and cells[-1].endswith(" ")  # right-padded
    assert etf_lines[0].startswith("MARKET,SERIES,SYMBOL,SECURITY,PREVIOUS CLOSE PRICE") and etf_lines[0].endswith(",UNDERLYING")


def test_index_and_surveillance_builders_match_the_verified_shapes():
    text = kit.index_list_csv([kit.DUMMY_INDEX_ROW]).decode()
    assert text.splitlines()[0] == "Company Name,Industry,Symbol,Series,ISIN Code"
    assert text.splitlines()[1] == "Dummy HEG Ltd.,Metals & Mining,DUMMYHEG,EQ,DUM545A01024"
    asm = json.loads(kit.asm_json([kit.asm_row("AAA", "INE002A01018")], [kit.asm_row("BBB", None)]))
    assert set(asm) == {"longterm", "shortterm"}
    assert tuple(asm["longterm"]["data"][0]) == kit.ASM_ROW_KEYS
    assert asm["shortterm"]["data"][0]["isin"] is None
    gsm = json.loads(kit.gsm_json([kit.gsm_row("CCC", "INE002A01018")]))
    assert isinstance(gsm, list) and tuple(gsm[0]) == kit.GSM_ROW_KEYS


def test_breeze_builder_emits_bare_numbers_with_the_eight_keys():
    payload = kit.breeze_v2_json([kit.breeze_row("RELIND", "2025-01-02 00:00:00", "1", "2", "1", "1.50", 7)])
    assert b'"close":1.50' in payload
    body = json.loads(payload)
    assert set(body) == {"Success", "Status", "Error"} and body["Status"] == 200
    assert set(body["Success"][0]) == set(kit.BREEZE_ROW_KEYS)

"""Byte builders for every verified NSE, ICICI master and Breeze v2 format.

This is a helper module (not a test file). It holds public market data only.
Builders produce the exact bytes the parsers in backend/pilot_data accept.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from decimal import Decimal
from typing import Iterable, Mapping

# --------------------------------------------------------------------------- UDiFF
UDIFF_HEADER_T = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
    "PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
    "TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
).split(",")

UDIFF_RELIANCE_20250102 = {
    "TradDt": "2025-01-02", "BizDt": "2025-01-02", "Sgmt": "CM", "Src": "NSE", "FinInstrmTp": "STK",
    "FinInstrmId": "2885", "ISIN": "INE002A01018", "TckrSymb": "RELIANCE", "SctySrs": "EQ",
    "FinInstrmNm": "RELIANCE INDUSTRIES LTD", "OpnPric": "1221.25", "HghPric": "1244.45",
    "LwPric": "1220.00", "ClsPric": "1241.80", "LastPric": "1240.55", "PrvsClsgPric": "1221.25",
    "SttlmPric": "1241.80", "TtlTradgVol": "15486276", "TtlTrfVal": "19115027208.35",
    "TtlNbOfTxsExctd": "271108", "SsnId": "F1", "NewBrdLotQty": "1",
}
UDIFF_NIFTYBEES_20250102 = {
    "TradDt": "2025-01-02", "BizDt": "2025-01-02", "Sgmt": "CM", "Src": "NSE", "FinInstrmTp": "STK",
    "FinInstrmId": "10576", "ISIN": "INF204KB14I2", "TckrSymb": "NIFTYBEES", "SctySrs": "EQ",
    "FinInstrmNm": "NIP IND ETF NIFTY BEES", "OpnPric": "266.99", "HghPric": "270.23",
    "LwPric": "265.20", "ClsPric": "269.89", "LastPric": "269.90", "PrvsClsgPric": "265.59",
    "SttlmPric": "269.89", "TtlTradgVol": "4665341", "TtlTrfVal": "1251261200.74",
    "TtlNbOfTxsExctd": "54578", "SsnId": "F1", "NewBrdLotQty": "1",
}


def udiff_member_name(trade_date: date) -> str:
    return f"BhavCopy_NSE_CM_0_0_0_{trade_date:%Y%m%d}_F_0000.csv"


def udiff_row(
    symbol: str,
    series: str,
    isin: str,
    open_: str,
    high: str,
    low: str,
    close: str,
    *,
    trade_date: date,
    token: str = "1",
    prev_close: str | None = None,
    volume: str = "1000",
    value: str = "100000.00",
    trades: str = "10",
) -> dict[str, str]:
    """A UDiFF row dict for tests that need synthetic symbols."""
    return {
        "TradDt": trade_date.isoformat(), "BizDt": trade_date.isoformat(), "Sgmt": "CM", "Src": "NSE",
        "FinInstrmTp": "STK", "FinInstrmId": token, "ISIN": isin, "TckrSymb": symbol, "SctySrs": series,
        "FinInstrmNm": symbol, "OpnPric": open_, "HghPric": high, "LwPric": low, "ClsPric": close,
        "LastPric": close, "PrvsClsgPric": prev_close if prev_close is not None else open_,
        "SttlmPric": close, "TtlTradgVol": volume, "TtlTrfVal": value, "TtlNbOfTxsExctd": trades,
        "SsnId": "F1", "NewBrdLotQty": "1",
    }


def udiff_csv_text(trade_date: date, rows: Iterable[Mapping[str, str]], header: Iterable[str] = UDIFF_HEADER_T) -> str:
    header = list(header)
    lines = [",".join(header)]
    for row in rows:
        merged = {"TradDt": trade_date.isoformat(), "BizDt": trade_date.isoformat(), "Sgmt": "CM", "Src": "NSE",
                  "FinInstrmTp": "STK"}
        merged.update(row)
        lines.append(",".join(merged.get(name, "") for name in header))
    return "\n".join(lines) + "\n"


def _zip_bytes(members: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def udiff_zip(trade_date: date, rows: Iterable[Mapping[str, str]]) -> bytes:
    """UDiFF CM bhavcopy zip. Missing Sgmt/Src/FinInstrmTp/TradDt/BizDt cells default to the real values."""
    text = udiff_csv_text(trade_date, rows)
    return _zip_bytes({udiff_member_name(trade_date): text.encode("utf-8")})


# --------------------------------------------------------------------------- ICICI master
MASTER_HEADER_T = (
    "Token,ShortName,Series,CompanyName,ticksize,Lotsize,DateOfListing,DateOfDeListing,IssuePrice,"
    "FaceValue,ISINCode,52WeeksHigh,52WeeksLow,LifeTimeHigh,LifeTimeLow,HighDate,LowDate,Symbol,"
    "InstrumentType,PermittedToTrade,IssueCapital,WarningPercent,FreezePercent,CreditRating,"
    "IssueRate,IssueStartDate,InterestPaymentDate,IssueMaturityDate,BoardLotQty,Name,ListingDate,"
    "ExpulsionDate,ReAdmissionDate,RecordDate,ExpiryDate,NoDeliveryStartDate,NoDeliveryEndDate,"
    "MFill,AON,ParticipantInMarketIndex,BookClsStartDate,BookClsEndDate,EGM,AGM,Interest,Bonus,"
    "Rights,Dividends,LocalUpdateDateTime,DeleteFlag,Remarks,NormalMarketStatus,OddLotMarketStatus,"
    "SpotMarketStatus,AuctionMarketStatus,NormalMarketEligibility,OddLotlMarketEligibility,"
    "SpotMarketEligibility,AuctionlMarketEligibility,MarginPercentage,ExchangeCode"
).split(",")

_MASTER_NUMERIC = {
    "Token", "ticksize", "Lotsize", "IssuePrice", "FaceValue", "52WeeksHigh", "52WeeksLow", "LifeTimeHigh",
    "LifeTimeLow", "IssueCapital", "WarningPercent", "FreezePercent", "IssueRate", "BoardLotQty",
    "MarginPercentage",
}


def master_row(token: int, short_name: str, series: str, company: str, ticksize: str, face_value: str,
               isin: str, exchange_code: str) -> dict[str, str]:
    return {
        "Token": str(token), "ShortName": short_name, "Series": series, "CompanyName": company,
        "ticksize": ticksize, "FaceValue": face_value, "ISINCode": isin, "ExchangeCode": exchange_code,
    }


MASTER_RELIND = master_row(2885, "RELIND", "EQ", "RELIANCE INDUSTRIES", "0.01", "10", "INE002A01018", "RELIANCE")
MASTER_NIFBEE = master_row(10576, "NIFBEE", "EQ", "NIPPON INDIA ETF NIFTY 50 BEES", "0.01", "10",
                           "INF204KB14I2", "NIFTYBEES")
MASTER_TATMOT = master_row(3456, "TATMOT", "EQ", "TATA MOTORS PAX VEHICLES LTD", "0.01", "2",
                           "INE155A01022", "TMPV")
MASTER_CANBAN = master_row(10794, "CANBAN", "EQ", "CANARA BANK", "0.01", "2", "INE476A01022", "CANBK")
MASTER_JAIBAL = master_row(11256, "JAIBAL", "EQ", "JAI BALAJI INDUSTRIES LIMITED", "0.01", "2",
                           "INE091G01026", "JAIBALAJI")
MASTER_HDFBAN = master_row(1333, "HDFBAN", "EQ", "HDFC BANK LIMITED", "0.01", "1", "INE040A01034", "HDFCBANK")
MASTER_HDFWA2 = master_row(17320, "HDFWA2", "W3", "HDFC BANK WARRANT AUG 23", "0.01", "2",
                           "INE040A13013", "HDFCBANK")
MASTER_ACRTEC = master_row(0, "ACRTEC", "BE", "ACROPETAL TECHNOLOGIES LIMITED", "0.01", "10",
                           "INE055L01013", "ACROPETAL")
MASTER_SAMPLE_ROWS = (MASTER_RELIND, MASTER_NIFBEE, MASTER_TATMOT, MASTER_CANBAN, MASTER_JAIBAL,
                      MASTER_HDFBAN, MASTER_HDFWA2, MASTER_ACRTEC)


def security_master_text(rows: Iterable[Mapping[str, str]], header: Iterable[str] = MASTER_HEADER_T) -> str:
    """Quoted header joined by comma plus space, quoted rows joined by comma only (as the real file)."""
    header = list(header)
    lines = [", ".join(f'"{name}"' for name in header)]
    for row in rows:
        cells = []
        for name in header:
            default = "0" if name in _MASTER_NUMERIC else ""
            cells.append(f'"{row.get(name, default)}"')
        lines.append(",".join(cells))
    return "\n".join(lines) + "\n"


def security_master_bytes(rows: Iterable[Mapping[str, str]]) -> bytes:
    return security_master_text(rows).encode("utf-8")


def security_master_zip(rows: Iterable[Mapping[str, str]]) -> bytes:
    return _zip_bytes({"NSEScripMaster.txt": security_master_bytes(rows)})


# --------------------------------------------------------------------------- Breeze v2 JSON
BREEZE_ROW_KEYS = ("close", "datetime", "exchange_code", "high", "low", "open", "stock_code", "volume")


def breeze_row(stock_code: str, stamp: str, open_: str, high: str, low: str, close: str, volume: int = 1000,
               exchange_code: str = "NSE") -> dict[str, object]:
    return {
        "close": Decimal(close), "datetime": stamp, "exchange_code": exchange_code, "high": Decimal(high),
        "low": Decimal(low), "open": Decimal(open_), "stock_code": stock_code, "volume": volume,
    }


def _json_dumps(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return "{" + ",".join(f"{json.dumps(str(k))}:{_json_dumps(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json_dumps(v) for v in value) + "]"
    return json.dumps(value)


def breeze_v2_json(rows: Iterable[Mapping[str, object]], status: int = 200, error: object = None) -> bytes:
    """Breeze v2 historical envelope with Decimal prices emitted as bare JSON numbers."""
    envelope = {"Success": list(rows), "Status": status, "Error": error}
    return _json_dumps(envelope).encode("utf-8")


# --------------------------------------------------------------------------- legacy CM bhavcopy
CM_LEGACY_HEADER_T = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,"
).split(",")  # the trailing comma makes an empty 14th column
assert len(CM_LEGACY_HEADER_T) == 14 and CM_LEGACY_HEADER_T[-1] == ""

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def legacy_stamp(day: date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def cm_legacy_member_name(day: date) -> str:
    return f"cm{day.day:02d}{_MONTHS[day.month - 1]}{day.year}bhav.csv"


CM_CANBK_20240514 = {
    "SYMBOL": "CANBK", "SERIES": "EQ", "OPEN": "555.4", "HIGH": "569", "LOW": "553.55", "CLOSE": "566.55",
    "LAST": "568.95", "PREVCLOSE": "549.35", "TOTTRDQTY": "9219466", "TOTTRDVAL": "5199036379.85",
    "TIMESTAMP": "14-MAY-2024", "TOTALTRADES": "135998", "ISIN": "INE476A01014",
}
CM_CANBK_20240515 = {
    "SYMBOL": "CANBK", "SERIES": "EQ", "OPEN": "116.25", "HIGH": "119.6", "LOW": "116", "CLOSE": "119",
    "LAST": "119.1", "PREVCLOSE": "566.55", "TOTTRDQTY": "58148316", "TOTTRDVAL": "6868210896.15",
    "TIMESTAMP": "15-MAY-2024", "TOTALTRADES": "214102", "ISIN": "INE476A01022",
}


def cm_legacy_row(symbol: str, series: str, isin: str, open_: str, high: str, low: str, close: str, *,
                  trade_date: date, prev_close: str | None = None, volume: str = "1000",
                  value: str = "100000.00", trades: str = "10") -> dict[str, str]:
    return {
        "SYMBOL": symbol, "SERIES": series, "OPEN": open_, "HIGH": high, "LOW": low, "CLOSE": close,
        "LAST": close, "PREVCLOSE": prev_close if prev_close is not None else open_, "TOTTRDQTY": volume,
        "TOTTRDVAL": value, "TIMESTAMP": legacy_stamp(trade_date), "TOTALTRADES": trades, "ISIN": isin,
    }


def cm_legacy_csv_text(trade_date: date, rows: Iterable[Mapping[str, str]],
                       header: Iterable[str] = CM_LEGACY_HEADER_T) -> str:
    header = list(header)
    lines = [",".join(header)]
    for row in rows:
        merged = {"TIMESTAMP": legacy_stamp(trade_date)}
        merged.update(row)
        lines.append(",".join(merged.get(name, "") for name in header))
    return "\n".join(lines) + "\n"


def cm_legacy_zip(trade_date: date, rows: Iterable[Mapping[str, str]]) -> bytes:
    text = cm_legacy_csv_text(trade_date, rows)
    return _zip_bytes({cm_legacy_member_name(trade_date): text.encode("utf-8")})


# --------------------------------------------------------------------------- legacy PR zip
PD_HEADER_T = (
    "MKT,SERIES,SYMBOL,SECURITY,PREV_CL_PR,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,CLOSE_PRICE,NET_TRDVAL,"
    "NET_TRDQTY,IND_SEC,CORP_IND,TRADES,HI_52_WK,LO_52_WK"
).split(",")
BC_HEADER_T = (
    "SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE"
).split(",")
ETF_HEADER_T = (
    "MARKET,SERIES,SYMBOL,SECURITY,PREVIOUS CLOSE PRICE,OPEN PRICE,HIGH PRICE,LOW PRICE,CLOSE PRICE,"
    "NET TRADED VALUE,NET TRADED QTY,TRADES,52 WEEK HIGH,52 WEEK LOW,UNDERLYING"
).split(",")


def pr_stamp(day: date) -> str:
    return f"{day.day:02d}{day.month:02d}{day.year % 100:02d}"


def pr_member_names(day: date) -> tuple[str, str, str]:
    stamp = pr_stamp(day)
    return f"Pd{stamp}.csv", f"Bc{stamp}.csv", f"etf{stamp}.csv"


def pd_row(symbol: str, series: str, open_: str, high: str, low: str, close: str, *, prev_close: str | None = None,
           volume: str = "1000", value: str = "100000.00", trades: str = "10", mkt: str = "N",
           security: str | None = None) -> dict[str, str]:
    return {
        "MKT": mkt, "SERIES": series, "SYMBOL": symbol, "SECURITY": security or symbol,
        "PREV_CL_PR": prev_close if prev_close is not None else open_, "OPEN_PRICE": open_, "HIGH_PRICE": high,
        "LOW_PRICE": low, "CLOSE_PRICE": close, "NET_TRDVAL": value, "NET_TRDQTY": volume, "IND_SEC": "",
        "CORP_IND": "", "TRADES": trades, "HI_52_WK": high, "LO_52_WK": low,
    }


def pd_index_row(name: str = "NIFTY 50") -> dict[str, str]:
    """An index row (MKT Y): blank SERIES and SYMBOL, as in the real file."""
    row = {key: "" for key in PD_HEADER_T}
    row.update({"MKT": "Y", "SECURITY": name, "PREV_CL_PR": "23000.00", "OPEN_PRICE": "23100.00",
                "HIGH_PRICE": "23200.00", "LOW_PRICE": "23000.00", "CLOSE_PRICE": "23150.00"})
    return row


def bc_row(series: str, symbol: str, security: str, purpose: str, *, ex_date: date | None = None,
           record_date: date | None = None) -> dict[str, object]:
    return {"SERIES": series, "SYMBOL": symbol, "SECURITY": security, "RECORD_DT": record_date, "EX_DT": ex_date,
            "PURPOSE": purpose}


def etf_row(symbol: str, security: str, underlying: str, close: str = "100.00", series: str = "EQ") -> dict[str, str]:
    return {"MARKET": "N", "SERIES": series, "SYMBOL": symbol, "SECURITY": security,
            "PREVIOUS CLOSE PRICE": close, "OPEN PRICE": close, "HIGH PRICE": close, "LOW PRICE": close,
            "CLOSE PRICE": close, "NET TRADED VALUE": "100000.00", "NET TRADED QTY": "1000", "TRADES": "10",
            "52 WEEK HIGH": close, "52 WEEK LOW": close, "UNDERLYING": underlying}


def _pr_cell(value: object, *, pad_left: int = 0, pad_right: int = 0) -> str:
    if isinstance(value, date):
        text = f"{value.day:02d}/{value.month:02d}/{value.year}"
    else:
        text = "" if value is None else str(value)
    return text.rjust(pad_left) if pad_left else text.ljust(pad_right) if pad_right else text


def pd_csv_text(rows: Iterable[Mapping[str, str]]) -> str:
    lines = [",".join(PD_HEADER_T)]
    text_columns = {"MKT", "SERIES", "SYMBOL", "SECURITY", "IND_SEC", "CORP_IND"}
    for row in rows:
        cells = []
        for name in PD_HEADER_T:
            value = row.get(name, "")
            cells.append(_pr_cell(value) if name in text_columns else _pr_cell(value, pad_left=12))
        lines.append(",".join(cells))
    return "\n".join(lines) + "\n"


def bc_csv_text(rows: Iterable[Mapping[str, object]]) -> str:
    lines = [",".join(BC_HEADER_T)]
    for row in rows:
        cells = []
        for name in BC_HEADER_T:
            value = row.get(name)
            if name == "PURPOSE":
                cells.append(_pr_cell(value, pad_right=40))
            elif value is None or value == "":
                cells.append(" ")
            else:
                cells.append(_pr_cell(value))
        lines.append(",".join(cells))
    return "\n".join(lines) + "\n"


def etf_csv_text(rows: Iterable[Mapping[str, str]]) -> str:
    lines = [",".join(ETF_HEADER_T)]
    for row in rows:
        lines.append(",".join(str(row.get(name, "")) for name in ETF_HEADER_T))
    return "\n".join(lines) + "\n"


def pr_zip(trade_date: date, pd_rows: Iterable[Mapping[str, str]], bc_rows: Iterable[Mapping[str, object]],
           etf_rows: Iterable[Mapping[str, str]]) -> bytes:
    pd_name, bc_name, etf_name = pr_member_names(trade_date)
    return _zip_bytes(
        {
            pd_name: pd_csv_text(pd_rows).encode("utf-8"),
            bc_name: bc_csv_text(bc_rows).encode("utf-8"),
            etf_name: etf_csv_text(etf_rows).encode("utf-8"),
            f"HL{pr_stamp(trade_date)}.csv": b"UNRELATED,FILE\n1,2\n",
        }
    )


# --------------------------------------------------------------------------- index lists and surveillance
INDEX_LIST_HEADER_T = "Company Name,Industry,Symbol,Series,ISIN Code".split(",")
DUMMY_INDEX_ROW = {"Company Name": "Dummy HEG Ltd.", "Industry": "Metals & Mining", "Symbol": "DUMMYHEG",
                   "Series": "EQ", "ISIN Code": "DUM545A01024"}


def index_list_csv(rows: Iterable[Mapping[str, str]], header: Iterable[str] = INDEX_LIST_HEADER_T) -> bytes:
    import csv as _csv

    header = list(header)
    buffer = io.StringIO()
    writer = _csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(name, "") for name in header])
    return buffer.getvalue().encode("utf-8")


ASM_ROW_KEYS = ("asmSurvIndicator", "asmTime", "companyName", "isin", "series", "survCode", "survDesc",
                "symbol", "srno")
GSM_ROW_KEYS = ("companyName", "gsmStage", "gsmTime", "isin", "survCode", "survDesc", "symbol", "srno")


def asm_row(symbol: str, isin: str | None, *, stage: str = "Stage I", when: str = "01-Oct-2026",
            serial: int = 1) -> dict[str, object]:
    return {"asmSurvIndicator": stage, "asmTime": when, "companyName": f"{symbol} LTD", "isin": isin,
            "series": None, "survCode": 1, "survDesc": "ASM", "symbol": symbol, "srno": serial}


def gsm_row(symbol: str, isin: str | None, *, stage: str = "I", when: str = "01-Oct-2026 08:07:02",
            serial: int = 1) -> dict[str, object]:
    return {"companyName": f"{symbol} LTD", "gsmStage": stage, "gsmTime": when, "isin": isin, "survCode": 2,
            "survDesc": "GSM", "symbol": symbol, "srno": serial}


def asm_json(longterm_rows: Iterable[Mapping[str, object]], shortterm_rows: Iterable[Mapping[str, object]]) -> bytes:
    return json.dumps(
        {"longterm": {"data": list(longterm_rows)}, "shortterm": {"data": list(shortterm_rows)}}
    ).encode("utf-8")


def gsm_json(rows: Iterable[Mapping[str, object]]) -> bytes:
    return json.dumps(list(rows)).encode("utf-8")

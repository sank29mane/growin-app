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

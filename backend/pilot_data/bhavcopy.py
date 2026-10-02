"""NSE bhavcopy parsing and append-only ingest (UDiFF here; legacy formats are added by plan 59-02)."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Mapping

from pydantic import ValidationError

from .core import PilotDataError, SourceDescriptor, canonical_sha256, parse_decimal, read_zip_members, utc_naive
from .models import BhavcopyBar, ParseQuarantineInput, QuarantineRecord
from .store import PilotDataStore

UDIFF_HEADER: tuple[str, ...] = tuple(
    (
        "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
        "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
        "PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
        "TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
    ).split(",")
)

PRICE_PLACES = 4
VALUE_PLACES = 2

BARS_DDL = (
    "CREATE TABLE IF NOT EXISTS bhavcopy_bars("
    "file_kind VARCHAR NOT NULL, trade_date DATE NOT NULL, nse_symbol VARCHAR NOT NULL, "
    "series VARCHAR NOT NULL, isin VARCHAR, token BIGINT, open DECIMAL(18,4) NOT NULL, "
    "high DECIMAL(18,4) NOT NULL, low DECIMAL(18,4) NOT NULL, close DECIMAL(18,4) NOT NULL, "
    "last DECIMAL(18,4), prev_close DECIMAL(18,4) NOT NULL, volume BIGINT NOT NULL, "
    "traded_value DECIMAL(24,2) NOT NULL, trades BIGINT, adjustment_basis VARCHAR NOT NULL, "
    "source_sha256 VARCHAR NOT NULL, row_sha256 VARCHAR NOT NULL, "
    "PRIMARY KEY(file_kind, trade_date, nse_symbol, series))"
)
FILES_DDL = (
    "CREATE TABLE IF NOT EXISTS bhavcopy_files("
    "source_sha256 VARCHAR PRIMARY KEY, file_kind VARCHAR NOT NULL, trade_date DATE NOT NULL, "
    "row_count BIGINT NOT NULL, parsed_at_utc TIMESTAMP NOT NULL, row_sha256 VARCHAR NOT NULL)"
)
BAR_COLUMNS = (
    "file_kind", "trade_date", "nse_symbol", "series", "isin", "token", "open", "high", "low", "close",
    "last", "prev_close", "volume", "traded_value", "trades", "adjustment_basis",
)


def ensure_bhavcopy_tables(store: PilotDataStore) -> None:
    store.ensure_table(
        "bhavcopy_bars", BARS_DDL, key_columns=("file_kind", "trade_date", "nse_symbol", "series")
    )
    store.ensure_table("bhavcopy_files", FILES_DDL, key_columns=("source_sha256",))


@dataclass(frozen=True)
class UdiffParse:
    bars: tuple[BhavcopyBar, ...]
    parse_quarantine_inputs: tuple[ParseQuarantineInput, ...]
    skipped_non_stk: int


@dataclass(frozen=True)
class BhavcopyIngestOutcome:
    source_sha256: str
    trade_date: date
    bars_inserted: int
    bars_identical: int
    conflicts: int
    quarantined: int
    skipped_non_stk: int = 0


def decode_text(data: bytes, *, code: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PilotDataError(code, "file is not valid UTF-8") from exc


def _places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return max(0, -int(exponent)) if isinstance(exponent, int) else 0


def validate_ohlc(
    open_: Decimal, high: Decimal, low: Decimal, close: Decimal, prev_close: Decimal | None = None,
    last: Decimal | None = None, traded_value: Decimal | None = None,
) -> str | None:
    """Return a problem description, or None when the bar is internally consistent."""
    for name, value in (("open", open_), ("high", high), ("low", low), ("close", close)):
        if value <= 0:
            return f"{name} is not positive"
    if not (low <= min(open_, close) <= max(open_, close) <= high):
        return "low <= min(open, close) <= max(open, close) <= high fails"
    for name, value in (("open", open_), ("high", high), ("low", low), ("close", close),
                        ("prev_close", prev_close), ("last", last)):
        if value is not None and _places(value) > PRICE_PLACES:
            return f"{name} has more than {PRICE_PLACES} decimal places"
    if traded_value is not None and _places(traded_value) > VALUE_PLACES:
        return f"traded value has more than {VALUE_PLACES} decimal places"
    return None


def parse_int(text: str, *, field_name: str) -> int:
    cleaned = text.strip()
    if not cleaned.isdigit():
        raise PilotDataError("integer_invalid", f"field {field_name}: not a non-negative integer")
    return int(cleaned)


def bar_to_row(bar: BhavcopyBar, source_sha256: str) -> dict[str, object]:
    row = {name: getattr(bar, name) for name in BAR_COLUMNS}
    row["source_sha256"] = source_sha256
    row["row_sha256"] = bar.row_sha256()
    return row


def parse_udiff(content: bytes, *, expected_trade_date: date) -> UdiffParse:
    members = read_zip_members(content)
    expected_name = f"BhavCopy_NSE_CM_0_0_0_{expected_trade_date:%Y%m%d}_F_0000.csv"
    if list(members) != [expected_name]:
        raise PilotDataError("bhavcopy_member_unexpected", "zip must hold exactly the dated UDiFF member")
    text = decode_text(members[expected_name], code="bhavcopy_schema_mismatch")
    reader = csv.reader(io.StringIO(text))
    try:
        header = tuple(next(reader))
    except StopIteration as exc:
        raise PilotDataError("bhavcopy_schema_mismatch", "UDiFF file is empty") from exc
    if header != UDIFF_HEADER:
        raise PilotDataError("bhavcopy_schema_mismatch", "UDiFF header differs from the verified header")
    bars: list[BhavcopyBar] = []
    quarantines: list[ParseQuarantineInput] = []
    skipped = 0
    for raw in reader:
        if not raw:
            continue
        if len(raw) != len(UDIFF_HEADER):
            raise PilotDataError("bhavcopy_schema_mismatch", "UDiFF row has the wrong field count")
        row: Mapping[str, str] = dict(zip(UDIFF_HEADER, raw))
        if row["Sgmt"] != "CM" or row["Src"] != "NSE":
            raise PilotDataError("bhavcopy_schema_mismatch", "UDiFF row is not CM/NSE")
        if row["FinInstrmTp"] != "STK":
            skipped += 1
            continue
        if row["TradDt"] != expected_trade_date.isoformat():
            raise PilotDataError("bhavcopy_date_mismatch", "UDiFF TradDt differs from the expected date")
        symbol, series = row["TckrSymb"].strip(), row["SctySrs"].strip()
        isin = row["ISIN"].strip() or None
        base = {"trade_date": expected_trade_date, "nse_symbol": symbol or None, "series": series or None,
                "isin": isin}
        try:
            open_ = parse_decimal(row["OpnPric"], field="OpnPric")
            high = parse_decimal(row["HghPric"], field="HghPric")
            low = parse_decimal(row["LwPric"], field="LwPric")
            close = parse_decimal(row["ClsPric"], field="ClsPric")
            prev_close = parse_decimal(row["PrvsClsgPric"], field="PrvsClsgPric")
            last = parse_decimal(row["LastPric"], field="LastPric") if row["LastPric"].strip() else None
            volume = parse_int(row["TtlTradgVol"], field_name="TtlTradgVol")
            traded_value = parse_decimal(row["TtlTrfVal"], field="TtlTrfVal")
            trades = parse_int(row["TtlNbOfTxsExctd"], field_name="TtlNbOfTxsExctd") if row[
                "TtlNbOfTxsExctd"].strip() else None
        except PilotDataError as exc:
            quarantines.append(ParseQuarantineInput(reason_code="invalid_ohlc", detail={"problem": exc.code}, **base))
            continue
        problem = validate_ohlc(open_, high, low, close, prev_close, last, traded_value)
        if problem is not None:
            quarantines.append(ParseQuarantineInput(reason_code="invalid_ohlc", detail={"problem": problem}, **base))
            continue
        token_text = row["FinInstrmId"].strip()
        try:
            bars.append(
                BhavcopyBar(
                    file_kind="udiff", trade_date=expected_trade_date, nse_symbol=symbol, series=series,
                    isin=isin, token=int(token_text) if token_text.isdigit() else None, open=open_,
                    high=high, low=low, close=close, prev_close=prev_close, last=last, volume=volume,
                    traded_value=traded_value, trades=trades,
                )
            )
        except ValidationError as exc:
            quarantines.append(
                ParseQuarantineInput(reason_code="invalid_row", detail={"problem": exc.errors()[0]["msg"]}, **base)
            )
    return UdiffParse(tuple(bars), tuple(quarantines), skipped)


def quarantines_from_inputs(
    inputs: tuple[ParseQuarantineInput, ...], *, check: str, source_sha256: str, default_date: date | None = None
) -> list[QuarantineRecord]:
    records: list[QuarantineRecord] = []
    for item in inputs:
        day = item.trade_date or default_date
        records.append(
            QuarantineRecord(
                workspace="india", check=check, reason_code=item.reason_code, scope="raw", isin=item.isin,
                nse_symbol=item.nse_symbol, stock_code=item.stock_code, series=item.series, date_from=day,
                date_to=day, detail=dict(item.detail), evidence_sha256s=(source_sha256,),
            )
        )
    return records


def append_bhavcopy_file_row(
    store: PilotDataStore, *, source_sha256: str, file_kind: str, trade_date: date, row_count: int
) -> None:
    row_hash = canonical_sha256(
        {"file_kind": file_kind, "trade_date": trade_date.isoformat(), "row_count": row_count}
    )
    store.append_rows(
        "bhavcopy_files",
        [
            {
                "source_sha256": source_sha256, "file_kind": file_kind, "trade_date": trade_date,
                "row_count": row_count, "parsed_at_utc": utc_naive(datetime.now(timezone.utc)),
                "row_sha256": row_hash,
            }
        ],
        check="bhavcopy_file",
    )


def ingest_udiff(
    store: PilotDataStore, descriptor: SourceDescriptor, content: bytes, *, trade_date: date
) -> BhavcopyIngestOutcome:
    ensure_bhavcopy_tables(store)
    ref = store.register_source(descriptor, content)
    parsed = parse_udiff(content, expected_trade_date=trade_date)
    outcome = store.append_rows(
        "bhavcopy_bars", [bar_to_row(bar, ref.source_sha256) for bar in parsed.bars], check="bhavcopy_bars"
    )
    records = quarantines_from_inputs(
        parsed.parse_quarantine_inputs, check="bhavcopy_parse", source_sha256=ref.source_sha256,
        default_date=trade_date,
    )
    quarantined = store.record_quarantine(records)
    append_bhavcopy_file_row(
        store, source_sha256=ref.source_sha256, file_kind="udiff", trade_date=trade_date,
        row_count=len(parsed.bars),
    )
    return BhavcopyIngestOutcome(
        source_sha256=ref.source_sha256, trade_date=trade_date, bars_inserted=outcome.inserted,
        bars_identical=outcome.identical, conflicts=outcome.conflicts, quarantined=quarantined,
        skipped_non_stk=parsed.skipped_non_stk,
    )

"""NSE bhavcopy parsing and append-only ingest for UDiFF, legacy CM and the legacy PR zip."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal, Mapping

from market_data.models import is_valid_isin
from pydantic import BaseModel, ConfigDict, ValidationError

from .core import (
    PilotDataError,
    SourceDescriptor,
    canonical_sha256,
    parse_decimal,
    read_zip_members,
    utc_naive,
)
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
# NSE UDiFF files from January to June 2024 (sampled monthly) name the reserved
# columns Rsvd01 to Rsvd04 and end the header line with a comma, while each data
# row still has the same 34 fields. Only this exact variant is accepted besides
# UDIFF_HEADER; the reserved columns are never read.
UDIFF_HEADER_EARLY: tuple[str, ...] = UDIFF_HEADER[:-4] + ("Rsvd01", "Rsvd02", "Rsvd03", "Rsvd04", "")
CM_LEGACY_HEADER: tuple[str, ...] = tuple(
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,".split(",")
)  # 13 names plus the empty column the trailing comma creates
PD_HEADER: tuple[str, ...] = tuple(
    (
        "MKT,SERIES,SYMBOL,SECURITY,PREV_CL_PR,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,CLOSE_PRICE,NET_TRDVAL,"
        "NET_TRDQTY,IND_SEC,CORP_IND,TRADES,HI_52_WK,LO_52_WK"
    ).split(",")
)
BC_HEADER: tuple[str, ...] = tuple(
    "SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE".split(",")
)
ETF_HEADER: tuple[str, ...] = tuple(
    (
        "MARKET,SERIES,SYMBOL,SECURITY,PREVIOUS CLOSE PRICE,OPEN PRICE,HIGH PRICE,LOW PRICE,CLOSE PRICE,"
        "NET TRADED VALUE,NET TRADED QTY,TRADES,52 WEEK HIGH,52 WEEK LOW,UNDERLYING"
    ).split(",")
)
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
TRADE_SERIES = ("EQ", "BE", "BZ")

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
CA_RAW_DDL = (
    "CREATE TABLE IF NOT EXISTS bhavcopy_ca_raw("
    "row_sha256 VARCHAR PRIMARY KEY, source_sha256 VARCHAR NOT NULL, file_date DATE NOT NULL, "
    "series VARCHAR NOT NULL, nse_symbol VARCHAR NOT NULL, security VARCHAR, record_date DATE, "
    "bc_start DATE, bc_end DATE, ex_date DATE, nd_start DATE, nd_end DATE, purpose_raw VARCHAR NOT NULL)"
)
ETF_INFO_DDL = (
    "CREATE TABLE IF NOT EXISTS bhavcopy_etf_info("
    "file_date DATE NOT NULL, nse_symbol VARCHAR NOT NULL, series VARCHAR NOT NULL, security VARCHAR, "
    "underlying VARCHAR, source_sha256 VARCHAR NOT NULL, row_sha256 VARCHAR NOT NULL, "
    "PRIMARY KEY(file_date, nse_symbol, series))"
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


def ensure_pr_tables(store: PilotDataStore) -> None:
    ensure_bhavcopy_tables(store)
    store.ensure_table("bhavcopy_ca_raw", CA_RAW_DDL, key_columns=("row_sha256",))
    store.ensure_table(
        "bhavcopy_etf_info", ETF_INFO_DDL, key_columns=("file_date", "nse_symbol", "series")
    )


@dataclass(frozen=True)
class UdiffParse:
    bars: tuple[BhavcopyBar, ...]
    parse_quarantine_inputs: tuple[ParseQuarantineInput, ...]
    skipped_non_stk: int


@dataclass(frozen=True)
class CmLegacyParse:
    bars: tuple[BhavcopyBar, ...]
    parse_quarantine_inputs: tuple[ParseQuarantineInput, ...]


@dataclass(frozen=True)
class BhavcopyIngestOutcome:
    source_sha256: str
    trade_date: date
    bars_inserted: int
    bars_identical: int
    conflicts: int
    quarantined: int
    skipped_non_stk: int = 0


def decode_text(data: bytes, *, code: str, cp1252_fallback: bool = False) -> str:
    """Decode UTF-8; PR archive members may fall back to strict Windows-1252.

    NSE's PR text members are Windows-1252 in places (from July 2025 the etf
    member carries a 0x96 en dash in an index name). The fallback is strict, so
    bytes undefined in Windows-1252 still fail closed.
    """
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        if cp1252_fallback:
            try:
                return data.decode("cp1252")
            except UnicodeDecodeError:
                pass
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


def _build_bar(
    *,
    file_kind: Literal["udiff", "cm_legacy", "pr_pd"],
    trade_date: date,
    symbol: str,
    series: str,
    isin: str | None,
    token: int | None,
    texts: Mapping[str, str],
    quarantines: list[ParseQuarantineInput],
) -> BhavcopyBar | None:
    """Parse one row's text cells into a bar, or append a quarantine input and return None."""
    base = {"trade_date": trade_date, "nse_symbol": symbol or None, "series": series or None, "isin": isin}
    try:
        open_ = parse_decimal(texts["open"], field="open")
        high = parse_decimal(texts["high"], field="high")
        low = parse_decimal(texts["low"], field="low")
        close = parse_decimal(texts["close"], field="close")
        prev_close = parse_decimal(texts["prev_close"], field="prev_close")
        last = parse_decimal(texts["last"], field="last") if texts.get("last", "").strip() else None
        volume = parse_int(texts["volume"], field_name="volume")
        traded_value = parse_decimal(texts["traded_value"], field="traded_value")
        trades = parse_int(texts["trades"], field_name="trades") if texts.get("trades", "").strip() else None
    except PilotDataError as exc:
        quarantines.append(ParseQuarantineInput(reason_code="invalid_ohlc", detail={"problem": exc.code}, **base))
        return None
    problem = validate_ohlc(open_, high, low, close, prev_close, last, traded_value)
    if problem is not None:
        quarantines.append(ParseQuarantineInput(reason_code="invalid_ohlc", detail={"problem": problem}, **base))
        return None
    try:
        return BhavcopyBar(
            file_kind=file_kind, trade_date=trade_date, nse_symbol=symbol, series=series, isin=isin,
            token=token, open=open_, high=high, low=low, close=close, prev_close=prev_close, last=last,
            volume=volume, traded_value=traded_value, trades=trades,
        )
    except ValidationError as exc:
        quarantines.append(
            ParseQuarantineInput(reason_code="invalid_row", detail={"problem": exc.errors()[0]["msg"]}, **base)
        )
        return None


def _read_csv(
    text: str,
    expected_header: tuple[str, ...],
    *,
    label: str,
    strip_header: bool = False,
    also_accept: tuple[tuple[str, ...], ...] = (),
):
    reader = csv.reader(io.StringIO(text))
    try:
        header = tuple(next(reader))
    except StopIteration as exc:
        raise PilotDataError("bhavcopy_schema_mismatch", f"{label} file is empty") from exc
    if strip_header:
        header = tuple(cell.strip() for cell in header)
    if header != expected_header and header not in also_accept:
        raise PilotDataError("bhavcopy_schema_mismatch", f"{label} header differs from the verified header")
    return reader


def parse_udiff(content: bytes, *, expected_trade_date: date) -> UdiffParse:
    members = read_zip_members(content)
    expected_name = f"BhavCopy_NSE_CM_0_0_0_{expected_trade_date:%Y%m%d}_F_0000.csv"
    if list(members) != [expected_name]:
        raise PilotDataError("bhavcopy_member_unexpected", "zip must hold exactly the dated UDiFF member")
    text = decode_text(members[expected_name], code="bhavcopy_schema_mismatch")
    reader = _read_csv(text, UDIFF_HEADER, label="UDiFF", also_accept=(UDIFF_HEADER_EARLY,))
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
        token_text = row["FinInstrmId"].strip()
        bar = _build_bar(
            file_kind="udiff", trade_date=expected_trade_date, symbol=row["TckrSymb"].strip(),
            series=row["SctySrs"].strip(), isin=row["ISIN"].strip() or None,
            token=int(token_text) if token_text.isdigit() else None,
            texts={
                "open": row["OpnPric"], "high": row["HghPric"], "low": row["LwPric"], "close": row["ClsPric"],
                "prev_close": row["PrvsClsgPric"], "last": row["LastPric"], "volume": row["TtlTradgVol"],
                "traded_value": row["TtlTrfVal"], "trades": row["TtlNbOfTxsExctd"],
            },
            quarantines=quarantines,
        )
        if bar is not None:
            bars.append(bar)
    return UdiffParse(tuple(bars), tuple(quarantines), skipped)


def legacy_stamp(day: date) -> str:
    return f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"


def cm_legacy_member_name(day: date) -> str:
    return f"cm{day.day:02d}{MONTHS[day.month - 1]}{day.year}bhav.csv"


def parse_cm_legacy(content: bytes, *, expected_trade_date: date) -> CmLegacyParse:
    members = read_zip_members(content)
    expected_name = cm_legacy_member_name(expected_trade_date)
    if list(members) != [expected_name]:
        raise PilotDataError("bhavcopy_member_unexpected", "zip must hold exactly the dated legacy CM member")
    text = decode_text(members[expected_name], code="bhavcopy_schema_mismatch")
    reader = _read_csv(text, CM_LEGACY_HEADER, label="legacy CM")
    bars: list[BhavcopyBar] = []
    quarantines: list[ParseQuarantineInput] = []
    for raw in reader:
        if not raw:
            continue
        if len(raw) != len(CM_LEGACY_HEADER):
            raise PilotDataError("bhavcopy_schema_mismatch", "legacy CM row has the wrong field count")
        row = dict(zip(CM_LEGACY_HEADER, raw))
        if row["TIMESTAMP"].strip() != legacy_stamp(expected_trade_date):
            raise PilotDataError("bhavcopy_date_mismatch", "legacy CM TIMESTAMP differs from the expected date")
        bar = _build_bar(
            file_kind="cm_legacy", trade_date=expected_trade_date, symbol=row["SYMBOL"].strip(),
            series=row["SERIES"].strip(), isin=row["ISIN"].strip() or None, token=None,
            texts={
                "open": row["OPEN"], "high": row["HIGH"], "low": row["LOW"], "close": row["CLOSE"],
                "prev_close": row["PREVCLOSE"], "last": row["LAST"], "volume": row["TOTTRDQTY"],
                "traded_value": row["TOTTRDVAL"], "trades": row["TOTALTRADES"],
            },
            quarantines=quarantines,
        )
        if bar is not None:
            bars.append(bar)
    return CmLegacyParse(tuple(bars), tuple(quarantines))


# --------------------------------------------------------------------------- PR zip
class CorporateActionRaw(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    file_date: date
    series: str
    nse_symbol: str
    security: str | None
    record_date: date | None
    bc_start: date | None
    bc_end: date | None
    ex_date: date | None
    nd_start: date | None
    nd_end: date | None
    purpose_raw: str

    def row_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class EtfInfoRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    file_date: date
    nse_symbol: str
    series: str
    security: str | None
    underlying: str | None

    def row_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


@dataclass(frozen=True)
class PrBundle:
    pd_bars: tuple[BhavcopyBar, ...]
    ca_rows: tuple[CorporateActionRaw, ...]
    etf_rows: tuple[EtfInfoRow, ...]
    bc_missing: bool
    etf_missing: bool
    parse_quarantine_inputs: tuple[ParseQuarantineInput, ...]


@dataclass(frozen=True)
class PrIngestOutcome:
    source_sha256: str
    trade_date: date
    pd_inserted: int
    pd_identical: int
    conflicts: int
    quarantined: int
    ca_rows: int
    etf_rows: int
    bc_missing: bool
    etf_missing: bool


def pr_stamp(day: date) -> str:
    return f"{day.day:02d}{day.month:02d}{day.year % 100:02d}"


def _parse_ddmmyyyy(text: str) -> date | None:
    """PR Bc dates: dd/mm/yyyy until October 2025, yyyy-mm-dd from November 2025."""
    cleaned = text.strip()
    if not cleaned:
        return None
    fmt = "%Y-%m-%d" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned) else "%d/%m/%Y"
    try:
        return datetime.strptime(cleaned, fmt).date()
    except ValueError as exc:
        raise PilotDataError("date_invalid", "date is not dd/mm/yyyy or yyyy-mm-dd") from exc


def _blank_to_none(text: str) -> str | None:
    cleaned = text.strip()
    return cleaned or None


def parse_pr_zip(content: bytes, *, expected_trade_date: date) -> PrBundle:
    members = read_zip_members(content)
    # Member stamps are DDMMYY until October 2025 and DDMMYYYY (lower-case
    # names) from November 2025. A zip carrying both forms of one member is
    # ambiguous and refused.
    wanted: dict[str, str] = {}
    for stamp in (pr_stamp(expected_trade_date), f"{expected_trade_date:%d%m%Y}"):
        wanted.update({f"pd{stamp}.csv": "pd", f"bc{stamp}.csv": "bc", f"etf{stamp}.csv": "etf"})
    found: dict[str, bytes] = {}
    for name, data in members.items():
        kind = wanted.get(name.lower())
        if kind is not None:
            if kind in found:
                raise PilotDataError("bhavcopy_schema_mismatch", f"PR zip has two {kind} members for the date")
            found[kind] = data
    if "pd" not in found:
        raise PilotDataError("pr_member_missing", "PR zip has no Pd member for the expected date")
    quarantines: list[ParseQuarantineInput] = []
    pd_bars: list[BhavcopyBar] = []
    reader = _read_csv(
        decode_text(found["pd"], code="bhavcopy_schema_mismatch", cp1252_fallback=True), PD_HEADER, label="PR Pd", strip_header=True
    )
    for raw in reader:
        if not raw:
            continue
        if len(raw) != len(PD_HEADER):
            raise PilotDataError("bhavcopy_schema_mismatch", "PR Pd row has the wrong field count")
        row = {name: cell.strip() for name, cell in zip(PD_HEADER, raw)}
        if row["MKT"] == "Y":
            continue
        if not row["SYMBOL"] and row["MKT"] not in {"N", "G"}:
            continue  # section headers and separators in the real file carry no symbol and no MKT
        if row["MKT"] not in {"N", "G"}:
            quarantines.append(
                ParseQuarantineInput(
                    reason_code="invalid_row", trade_date=expected_trade_date, nse_symbol=row["SYMBOL"] or None,
                    series=row["SERIES"] or None, detail={"problem": "unknown MKT value"},
                )
            )
            continue
        bar = _build_bar(
            file_kind="pr_pd", trade_date=expected_trade_date, symbol=row["SYMBOL"], series=row["SERIES"],
            isin=None, token=None,
            texts={
                "open": row["OPEN_PRICE"], "high": row["HIGH_PRICE"], "low": row["LOW_PRICE"],
                "close": row["CLOSE_PRICE"], "prev_close": row["PREV_CL_PR"], "last": "",
                "volume": row["NET_TRDQTY"], "traded_value": row["NET_TRDVAL"], "trades": row["TRADES"],
            },
            quarantines=quarantines,
        )
        if bar is not None:
            pd_bars.append(bar)
    ca_rows: list[CorporateActionRaw] = []
    if "bc" in found:
        reader = _read_csv(
            decode_text(found["bc"], code="bhavcopy_schema_mismatch", cp1252_fallback=True), BC_HEADER, label="PR Bc", strip_header=True
        )
        for raw in reader:
            if not raw:
                continue
            if len(raw) != len(BC_HEADER):
                raise PilotDataError("bhavcopy_schema_mismatch", "PR Bc row has the wrong field count")
            row = dict(zip(BC_HEADER, raw))
            try:
                ca_rows.append(
                    CorporateActionRaw(
                        file_date=expected_trade_date, series=row["SERIES"].strip(),
                        nse_symbol=row["SYMBOL"].strip(), security=_blank_to_none(row["SECURITY"]),
                        record_date=_parse_ddmmyyyy(row["RECORD_DT"]), bc_start=_parse_ddmmyyyy(row["BC_STRT_DT"]),
                        bc_end=_parse_ddmmyyyy(row["BC_END_DT"]), ex_date=_parse_ddmmyyyy(row["EX_DT"]),
                        nd_start=_parse_ddmmyyyy(row["ND_STRT_DT"]), nd_end=_parse_ddmmyyyy(row["ND_END_DT"]),
                        purpose_raw=row["PURPOSE"].strip(),
                    )
                )
            except (PilotDataError, ValidationError) as exc:
                quarantines.append(
                    ParseQuarantineInput(
                        reason_code="invalid_ca_row", trade_date=expected_trade_date,
                        nse_symbol=row["SYMBOL"].strip() or None, series=row["SERIES"].strip() or None,
                        detail={"problem": getattr(exc, "code", "validation")},
                    )
                )
    etf_rows: list[EtfInfoRow] = []
    if "etf" in found:
        reader = _read_csv(
            decode_text(found["etf"], code="bhavcopy_schema_mismatch", cp1252_fallback=True), ETF_HEADER, label="PR etf",
            strip_header=True,
        )
        for raw in reader:
            if not raw:
                continue
            if len(raw) != len(ETF_HEADER):
                raise PilotDataError("bhavcopy_schema_mismatch", "PR etf row has the wrong field count")
            row = {name: cell.strip() for name, cell in zip(ETF_HEADER, raw)}
            etf_rows.append(
                EtfInfoRow(
                    file_date=expected_trade_date, nse_symbol=row["SYMBOL"], series=row["SERIES"],
                    security=row["SECURITY"] or None, underlying=row["UNDERLYING"] or None,
                )
            )
    return PrBundle(
        pd_bars=tuple(pd_bars), ca_rows=tuple(ca_rows), etf_rows=tuple(etf_rows), bc_missing="bc" not in found,
        etf_missing="etf" not in found, parse_quarantine_inputs=tuple(quarantines),
    )


# --------------------------------------------------------------------------- ingest
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


def _ingest_bars(
    store: PilotDataStore,
    descriptor: SourceDescriptor,
    content: bytes,
    *,
    trade_date: date,
    file_kind: str,
    parser,
) -> BhavcopyIngestOutcome:
    ensure_bhavcopy_tables(store)
    ref = store.register_source(descriptor, content)
    parsed = parser(content, expected_trade_date=trade_date)
    outcome = store.append_rows(
        "bhavcopy_bars", [bar_to_row(bar, ref.source_sha256) for bar in parsed.bars], check="bhavcopy_bars"
    )
    records = quarantines_from_inputs(
        parsed.parse_quarantine_inputs, check="bhavcopy_parse", source_sha256=ref.source_sha256,
        default_date=trade_date,
    )
    quarantined = store.record_quarantine(records)
    append_bhavcopy_file_row(
        store, source_sha256=ref.source_sha256, file_kind=file_kind, trade_date=trade_date,
        row_count=len(parsed.bars),
    )
    return BhavcopyIngestOutcome(
        source_sha256=ref.source_sha256, trade_date=trade_date, bars_inserted=outcome.inserted,
        bars_identical=outcome.identical, conflicts=outcome.conflicts, quarantined=quarantined,
        skipped_non_stk=getattr(parsed, "skipped_non_stk", 0),
    )


def ingest_udiff(
    store: PilotDataStore, descriptor: SourceDescriptor, content: bytes, *, trade_date: date
) -> BhavcopyIngestOutcome:
    return _ingest_bars(store, descriptor, content, trade_date=trade_date, file_kind="udiff", parser=parse_udiff)


def ingest_cm_legacy(
    store: PilotDataStore, descriptor: SourceDescriptor, content: bytes, *, trade_date: date
) -> BhavcopyIngestOutcome:
    return _ingest_bars(
        store, descriptor, content, trade_date=trade_date, file_kind="cm_legacy", parser=parse_cm_legacy
    )


def ingest_pr_zip(
    store: PilotDataStore, descriptor: SourceDescriptor, content: bytes, *, trade_date: date
) -> PrIngestOutcome:
    ensure_pr_tables(store)
    ref = store.register_source(descriptor, content)
    bundle = parse_pr_zip(content, expected_trade_date=trade_date)
    bars = store.append_rows(
        "bhavcopy_bars", [bar_to_row(bar, ref.source_sha256) for bar in bundle.pd_bars], check="bhavcopy_bars"
    )
    ca_columns = ("file_date", "series", "nse_symbol", "security", "record_date", "bc_start", "bc_end",
                  "ex_date", "nd_start", "nd_end", "purpose_raw")
    store.append_rows(
        "bhavcopy_ca_raw",
        [
            {**{name: getattr(row, name) for name in ca_columns}, "source_sha256": ref.source_sha256,
             "row_sha256": row.row_sha256()}
            for row in bundle.ca_rows
        ],
        check="bhavcopy_ca_raw",
    )
    store.append_rows(
        "bhavcopy_etf_info",
        [
            {"file_date": row.file_date, "nse_symbol": row.nse_symbol, "series": row.series,
             "security": row.security, "underlying": row.underlying, "source_sha256": ref.source_sha256,
             "row_sha256": row.row_sha256()}
            for row in bundle.etf_rows
        ],
        check="bhavcopy_etf_info",
    )
    records = quarantines_from_inputs(
        bundle.parse_quarantine_inputs, check="bhavcopy_parse", source_sha256=ref.source_sha256,
        default_date=trade_date,
    )
    quarantined = store.record_quarantine(records)
    append_bhavcopy_file_row(
        store, source_sha256=ref.source_sha256, file_kind="pr_zip", trade_date=trade_date,
        row_count=len(bundle.pd_bars),
    )
    return PrIngestOutcome(
        source_sha256=ref.source_sha256, trade_date=trade_date, pd_inserted=bars.inserted,
        pd_identical=bars.identical, conflicts=bars.conflicts, quarantined=quarantined,
        ca_rows=len(bundle.ca_rows), etf_rows=len(bundle.etf_rows), bc_missing=bundle.bc_missing,
        etf_missing=bundle.etf_missing,
    )


# --------------------------------------------------------------------------- reads and consistency
_SELECT_BARS = (
    "SELECT " + ", ".join(BAR_COLUMNS) + ", source_sha256 FROM bhavcopy_bars WHERE trade_date = ? AND file_kind = ? "
    "ORDER BY nse_symbol, series"
)


def _bars_with_sources(store: PilotDataStore, day: date, file_kind: str) -> list[tuple[BhavcopyBar, str]]:
    ensure_bhavcopy_tables(store)
    out: list[tuple[BhavcopyBar, str]] = []
    for row in store.query(_SELECT_BARS, [day, file_kind]):
        values = dict(zip(BAR_COLUMNS, row[: len(BAR_COLUMNS)]))
        out.append((BhavcopyBar(**values), row[-1]))
    return out


def _has_file(store: PilotDataStore, day: date, file_kind: str) -> bool:
    ensure_bhavcopy_tables(store)
    found = store.query(
        "SELECT count(*) FROM bhavcopy_files WHERE trade_date = ? AND file_kind = ?", [day, file_kind]
    )
    return bool(found[0][0])


def primary_bars_on(store: PilotDataStore, trade_date: date) -> list[BhavcopyBar]:
    """UDiFF bars when a UDiFF file exists for the date, else legacy CM bars."""
    kind = "udiff" if _has_file(store, trade_date, "udiff") else "cm_legacy"
    return [bar for bar, _ in _bars_with_sources(store, trade_date, kind)]


# Series that share the stock's ISIN but are not its normal-market price: BL is
# the block-deal window (one negotiated print) and T0 the T+0 settlement segment
# (from 2024-03-28). NSE's price band lists never carry either.
NON_REGULAR_SERIES = frozenset({"BL", "T0"})

_CENT = Decimal("0.01")


def _same_numbers(a: BhavcopyBar, b: BhavcopyBar) -> list[str]:
    differing = []
    for name in ("open", "high", "low", "close"):
        if getattr(a, name).quantize(_CENT) != getattr(b, name).quantize(_CENT):
            differing.append(name)
    if a.volume != b.volume:
        differing.append("volume")
    return differing


def check_same_date_consistency(
    store: PilotDataStore, trade_date: date, *, workspace: Literal["india"]
) -> tuple[QuarantineRecord, ...]:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    ensure_bhavcopy_tables(store)
    records: list[QuarantineRecord] = []

    def add(reason: str, bar: BhavcopyBar, isin: str | None, detail: dict[str, str], evidence: tuple[str, ...]):
        records.append(
            QuarantineRecord(
                workspace="india", check="bhavcopy_consistency", reason_code=reason, scope="raw", isin=isin,
                nse_symbol=bar.nse_symbol, series=bar.series, date_from=trade_date, date_to=trade_date,
                detail=detail, evidence_sha256s=evidence,
            )
        )

    has_udiff = _has_file(store, trade_date, "udiff")
    has_cm = _has_file(store, trade_date, "cm_legacy")
    primary_kind = "udiff" if has_udiff else "cm_legacy"
    primary = {(b.nse_symbol, b.series): (b, s) for b, s in _bars_with_sources(store, trade_date, primary_kind)}
    if has_udiff or has_cm:
        for (bar, source) in primary.values():
            if bar.series in TRADE_SERIES and bar.isin is not None and not is_valid_isin(bar.isin):
                add("isin_invalid", bar, bar.isin, {"isin": bar.isin}, (source,))
    if primary and _has_file(store, trade_date, "pr_zip"):
        pd = {(b.nse_symbol, b.series): (b, s) for b, s in _bars_with_sources(store, trade_date, "pr_pd")}
        for key, (pd_bar, pd_source) in pd.items():
            if key not in primary:
                add("pd_without_primary", pd_bar, None, {}, (pd_source,))
                continue
            primary_bar, primary_source = primary[key]
            differing = _same_numbers(pd_bar, primary_bar)
            if differing:
                detail = {"fields": ",".join(differing), "primary_kind": primary_kind}
                for name in differing:
                    detail[f"pd_{name}"] = str(getattr(pd_bar, name))
                    detail[f"primary_{name}"] = str(getattr(primary_bar, name))
                add("pd_primary_mismatch", pd_bar, primary_bar.isin, detail, (pd_source, primary_source))
        for key, (primary_bar, primary_source) in primary.items():
            if key not in pd and primary_bar.series in TRADE_SERIES:
                add("primary_without_pd", primary_bar, primary_bar.isin, {}, (primary_source,))
    if has_udiff and has_cm:
        udiff = {(b.nse_symbol, b.series): (b, s) for b, s in _bars_with_sources(store, trade_date, "udiff")}
        legacy = {(b.nse_symbol, b.series): (b, s) for b, s in _bars_with_sources(store, trade_date, "cm_legacy")}
        for key in sorted(set(udiff) | set(legacy)):
            if key in udiff and key in legacy:
                differing = _same_numbers(udiff[key][0], legacy[key][0])
                if differing:
                    detail = {"fields": ",".join(differing)}
                    for name in differing:
                        detail[f"udiff_{name}"] = str(getattr(udiff[key][0], name))
                        detail[f"cm_legacy_{name}"] = str(getattr(legacy[key][0], name))
                    add("udiff_cm_mismatch", udiff[key][0], udiff[key][0].isin, detail,
                        (udiff[key][1], legacy[key][1]))
            else:
                owner = udiff if key in udiff else legacy
                bar, source = owner[key]
                add("udiff_cm_mismatch", bar, bar.isin,
                    {"only_in": "udiff" if key in udiff else "cm_legacy"}, (source,))
    store.record_quarantine(records)
    return tuple(records)

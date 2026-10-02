"""Breeze v2 daily-bar parsing and append-only ingest.

The bytes parsed here arrive from the VM relay (or from fixtures in tests). This module
never opens a broker session and never addresses a broker host.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from .bhavcopy import quarantines_from_inputs, validate_ohlc
from .core import PilotDataError, SourceDescriptor, canonical_sha256, utc_naive
from .models import BreezeDailyBar, DailyBarConvention, ParseQuarantineInput
from .store import PilotDataStore

BREEZE_ROW_KEYS = frozenset(
    {"close", "datetime", "exchange_code", "high", "low", "open", "stock_code", "volume"}
)

RESPONSES_DDL = (
    "CREATE TABLE IF NOT EXISTS breeze_responses("
    "source_sha256 VARCHAR PRIMARY KEY, stock_code VARCHAR NOT NULL, requested_from DATE NOT NULL, "
    "requested_to DATE NOT NULL, convention_json VARCHAR NOT NULL, bars_parsed BIGINT NOT NULL, "
    "parsed_at_utc TIMESTAMP NOT NULL, row_sha256 VARCHAR NOT NULL)"
)
BARS_RAW_DDL = (
    "CREATE TABLE IF NOT EXISTS breeze_bars_raw("
    "stock_code VARCHAR NOT NULL, trade_date DATE NOT NULL, source_sha256 VARCHAR NOT NULL, "
    "raw_datetime VARCHAR NOT NULL, open DECIMAL(18,4) NOT NULL, high DECIMAL(18,4) NOT NULL, "
    "low DECIMAL(18,4) NOT NULL, close DECIMAL(18,4) NOT NULL, volume BIGINT NOT NULL, api VARCHAR NOT NULL, "
    "interval VARCHAR NOT NULL, adjustment_basis VARCHAR NOT NULL, row_sha256 VARCHAR NOT NULL, "
    "PRIMARY KEY(stock_code, trade_date, source_sha256))"
)


def ensure_breeze_tables(store: PilotDataStore) -> None:
    store.ensure_table("breeze_responses", RESPONSES_DDL, key_columns=("source_sha256",))
    store.ensure_table(
        "breeze_bars_raw", BARS_RAW_DDL, key_columns=("stock_code", "trade_date", "source_sha256")
    )


@dataclass(frozen=True)
class BreezeParse:
    bars: tuple[BreezeDailyBar, ...]
    parse_quarantine_inputs: tuple[ParseQuarantineInput, ...]


@dataclass(frozen=True)
class BreezeIngestOutcome:
    source_sha256: str
    bars_inserted: int
    bars_identical: int
    conflicts: int
    quarantined: int


def _price(value: Any, name: str) -> Decimal:
    if isinstance(value, str):
        raise PilotDataError("breeze_version_mismatch", f"{name} arrived as a string; v2 returns numbers")
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise PilotDataError("breeze_schema_mismatch", f"{name} is not a JSON number")
    return Decimal(value)


def parse_breeze_v2_daily(
    content: bytes, *, stock_code: str, convention: DailyBarConvention
) -> BreezeParse:
    try:
        body = json.loads(content, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PilotDataError("breeze_schema_mismatch", "response is not valid JSON") from exc
    if not isinstance(body, dict) or not {"Success", "Status", "Error"} <= set(body):
        raise PilotDataError("breeze_schema_mismatch", "envelope needs Success, Status and Error")
    status = body["Status"]
    if status != 200 or body["Error"] is not None:
        raise PilotDataError("breeze_upstream_error", f"upstream reported status {status!r}")
    success = body["Success"]
    if not isinstance(success, list):
        raise PilotDataError("breeze_schema_mismatch", "Success is not a list")
    if not success:
        raise PilotDataError("breeze_empty_response", "empty Success list is not zero volume")
    bars: list[BreezeDailyBar] = []
    inputs: list[ParseQuarantineInput] = []
    for row in success:
        if not isinstance(row, dict) or set(row) != BREEZE_ROW_KEYS:
            raise PilotDataError("breeze_schema_mismatch", "row keys differ from the verified v2 shape")
        if row["stock_code"] != stock_code:
            raise PilotDataError("breeze_stock_code_mismatch", "row stock_code differs from the request")
        if row["exchange_code"] != "NSE":
            raise PilotDataError("breeze_exchange_mismatch", "row exchange_code is not NSE")
        open_, high = _price(row["open"], "open"), _price(row["high"], "high")
        low, close = _price(row["low"], "low"), _price(row["close"], "close")
        volume = row["volume"]
        if isinstance(volume, str):
            raise PilotDataError("breeze_version_mismatch", "volume arrived as a string; v2 returns numbers")
        if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
            raise PilotDataError("breeze_schema_mismatch", "volume is not a non-negative integer")
        stamp = row["datetime"]
        try:
            parsed = datetime.strptime(stamp, convention.datetime_format)
        except (TypeError, ValueError) as exc:
            raise PilotDataError("breeze_datetime_unparseable", "datetime does not match the convention") from exc
        trade_date = parsed.date()
        base = {"trade_date": trade_date, "stock_code": stock_code}
        if parsed.strftime("%H:%M:%S") not in convention.allowed_times:
            inputs.append(
                ParseQuarantineInput(
                    reason_code="timestamp_unexpected", detail={"raw_datetime": stamp}, **base
                )
            )
            continue
        problem = validate_ohlc(open_, high, low, close)
        if problem is not None:
            inputs.append(ParseQuarantineInput(reason_code="invalid_ohlc", detail={"problem": problem}, **base))
            continue
        bars.append(
            BreezeDailyBar(
                stock_code=stock_code, trade_date=trade_date, raw_datetime=stamp, open=open_, high=high,
                low=low, close=close, volume=volume,
            )
        )
    grouped: dict[date, list[BreezeDailyBar]] = {}
    for bar in bars:
        grouped.setdefault(bar.trade_date, []).append(bar)
    kept: list[BreezeDailyBar] = []
    for day in sorted(grouped):
        distinct = {bar.row_sha256(): bar for bar in grouped[day]}
        if len(distinct) == 1:
            kept.append(next(iter(distinct.values())))
            continue
        for digest in sorted(distinct):
            inputs.append(
                ParseQuarantineInput(
                    reason_code="duplicate_conflict", trade_date=day, stock_code=stock_code,
                    detail={"row_sha256": digest},
                )
            )
    return BreezeParse(tuple(kept), tuple(inputs))


def ingest_breeze_response(
    store: PilotDataStore,
    descriptor: SourceDescriptor,
    content: bytes,
    *,
    stock_code: str,
    requested_from: date,
    requested_to: date,
    convention: DailyBarConvention,
) -> BreezeIngestOutcome:
    ensure_breeze_tables(store)
    ref = store.register_source(descriptor, content)
    parsed = parse_breeze_v2_daily(content, stock_code=stock_code, convention=convention)
    rows = [
        {
            "stock_code": bar.stock_code, "trade_date": bar.trade_date, "source_sha256": ref.source_sha256,
            "raw_datetime": bar.raw_datetime, "open": bar.open, "high": bar.high, "low": bar.low,
            "close": bar.close, "volume": bar.volume, "api": bar.api, "interval": bar.interval,
            "adjustment_basis": bar.adjustment_basis, "row_sha256": bar.row_sha256(),
        }
        for bar in parsed.bars
    ]
    outcome = store.append_rows("breeze_bars_raw", rows, check="breeze_bars")
    records = quarantines_from_inputs(
        parsed.parse_quarantine_inputs, check="breeze_parse", source_sha256=ref.source_sha256
    )
    quarantined = store.record_quarantine(records)
    convention_json = json.dumps(convention.model_dump(mode="json"), sort_keys=True)
    store.append_rows(
        "breeze_responses",
        [
            {
                "source_sha256": ref.source_sha256, "stock_code": stock_code, "requested_from": requested_from,
                "requested_to": requested_to, "convention_json": convention_json,
                "bars_parsed": len(parsed.bars), "parsed_at_utc": utc_naive(datetime.now(timezone.utc)),
                "row_sha256": canonical_sha256(
                    {
                        "stock_code": stock_code, "from": requested_from.isoformat(),
                        "to": requested_to.isoformat(), "convention": convention_json,
                        "bars": len(parsed.bars),
                    }
                ),
            }
        ],
        check="breeze_response",
    )
    return BreezeIngestOutcome(
        ref.source_sha256, outcome.inserted, outcome.identical, outcome.conflicts, quarantined
    )

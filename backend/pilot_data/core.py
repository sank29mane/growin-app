"""Shared primitives: errors, hashing, safe parsing, source descriptors, caveats."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from market_data.session import MarketDataError

IST = timezone(timedelta(hours=5, minutes=30), "IST")

ZIP_MAGIC = b"PK\x03\x04"


class PilotDataError(MarketDataError):
    """Fail-closed error with a stable code (reuses the market-data error type)."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_naive(value: datetime) -> datetime:
    """Convert an aware datetime to naive UTC for storage in TIMESTAMP columns."""
    if value.tzinfo is None:
        raise PilotDataError("timestamp_naive", "timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dec_str(value: Decimal) -> str:
    """Stable text form of a Decimal (independent of trailing zeros)."""
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _reject_inexact(payload: object) -> None:
    if isinstance(payload, (float, Decimal)):
        raise TypeError("canonical_sha256 rejects float and Decimal; pass model_dump(mode='json')")
    if isinstance(payload, dict):
        for key, value in payload.items():
            _reject_inexact(key)
            _reject_inexact(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            _reject_inexact(item)


def canonical_sha256(payload: object) -> str:
    """sha256 over the same canonical JSON form MarketSnapshot.snapshot_id uses."""
    _reject_inexact(payload)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_DECIMAL_RE = re.compile(r"^-?(\d+(\.\d*)?|\.\d+)$")


def parse_decimal(text: str, *, field: str) -> Decimal:
    cleaned = text.strip() if isinstance(text, str) else ""
    if not _DECIMAL_RE.match(cleaned):
        raise PilotDataError("decimal_invalid", f"field {field}: not a plain decimal number")
    return Decimal(cleaned)


def read_zip_members(
    content: bytes,
    *,
    max_member_bytes: int = 64 * 1024 * 1024,
    max_members: int = 64,
) -> dict[str, bytes]:
    """Read every member of a zip in memory, with name and size guards."""
    if not content.startswith(ZIP_MAGIC):
        raise PilotDataError("not_a_zip", "content does not start with the zip magic")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise PilotDataError("not_a_zip", "content is not a readable zip") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > max_members:
            raise PilotDataError("zip_too_large", f"zip has more than {max_members} members")
        members: dict[str, bytes] = {}
        for info in infos:
            name = info.filename
            if "/" in name or "\\" in name or ".." in name or not name:
                raise PilotDataError("zip_member_unsafe", "zip member name is not a plain file name")
            if info.file_size > max_member_bytes:
                raise PilotDataError("zip_too_large", "zip member declares an oversized body")
            with archive.open(info) as handle:
                data = handle.read(max_member_bytes + 1)
            if len(data) > max_member_bytes:
                raise PilotDataError("zip_too_large", "zip member expands beyond the size cap")
            members[name] = data
    return members


_SENSITIVE_LOCATOR_PARTS = ("session", "token", "apikey", "api_key", "secret", "password")


class SourceDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["nse_archive", "nse_api", "breeze_relay", "local_file"]
    kind: str = Field(..., pattern=r"^[a-z0-9_]+$")
    locator: str = Field(..., min_length=1)
    fetched_at: datetime
    for_date: date | None = None

    @field_validator("locator")
    @classmethod
    def _locator_not_sensitive(cls, value: str) -> str:
        lowered = value.lower()
        if any(part in lowered for part in _SENSITIVE_LOCATOR_PARTS):
            raise PilotDataError("locator_sensitive", "locator looks like it carries a credential")
        return value

    @field_validator("fetched_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")
        return value


class Caveat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(..., pattern=r"^[A-Z_]+$")
    text: str = Field(..., min_length=1)


SURVIVORSHIP_CAVEAT = Caveat(
    code="SURVIVORSHIP_BIAS",
    text=(
        "Survivorship bias: the universe is today's Nifty 500 plus ETFs that are liquid today. "
        "Securities that left the index, were delisted, or merged before today are missing, so "
        "historical results overstate what a point-in-time universe would have produced."
    ),
)
HINDSIGHT_CAVEAT = Caveat(
    code="HINDSIGHT_BIAS",
    text=(
        "Hindsight: index membership, liquid-ETF selection, small-cap classification (today's "
        "Nifty Smallcap 250) and the ISIN lineage used to follow each name back in time come from "
        "today's data, not from what was known on each historical date."
    ),
)
BREEZE_RAW_UNVERIFIED_CAVEAT = Caveat(
    code="BREEZE_RAW_UNVERIFIED",
    text=(
        "Breeze daily bars are assumed to be unadjusted (as traded) on the strength of one SDK "
        "README line; that assumption is not yet confirmed against NSE bhavcopy on known split "
        "and bonus dates."
    ),
)
SURVEILLANCE_HISTORY_CAVEAT = Caveat(
    code="SURVEILLANCE_HISTORY_UNAVAILABLE",
    text=(
        "ASM/GSM history is unavailable before the first stored NSE surveillance snapshot; "
        "surveillance exclusions were not applied for earlier dates."
    ),
)


def standard_caveats(*extra: Caveat) -> tuple[Caveat, ...]:
    ordered: list[Caveat] = [SURVIVORSHIP_CAVEAT, HINDSIGHT_CAVEAT, *extra]
    seen: set[str] = set()
    out: list[Caveat] = []
    for caveat in ordered:
        if caveat.code not in seen:
            seen.add(caveat.code)
            out.append(caveat)
    return tuple(out)


class CaveatedResult(BaseModel):
    """Base for every public result: survivorship and hindsight caveats are mandatory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: Literal["india"]
    caveats: tuple[Caveat, ...]

    @model_validator(mode="after")
    def _require_standard_caveats(self) -> Any:
        codes = {caveat.code for caveat in self.caveats}
        missing = {SURVIVORSHIP_CAVEAT.code, HINDSIGHT_CAVEAT.code} - codes
        if missing:
            raise ValueError(f"result is missing mandatory caveats: {sorted(missing)}")
        return self

    def content_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

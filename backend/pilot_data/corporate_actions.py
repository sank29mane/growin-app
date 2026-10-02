"""Corporate-action purpose grammar and typed, append-only events.

Evidence only: this module types what NSE announced. It never guesses a price factor,
and it records no quarantine. Plan 59-05 decides what an unresolved event quarantines.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .bhavcopy import ensure_pr_tables
from .core import PilotDataError, canonical_sha256, parse_decimal
from .store import PilotDataStore

ActionKind = Literal[
    "split", "consolidation", "bonus", "dividend", "rights", "demerger", "merger",
    "other_price_affecting", "non_price", "unknown",
]

_NUM = r"(\d+(?:\.\d+)?)"
_FV_SPLIT = re.compile(rf"^FVSPLT FRM (?:RS|RE) {_NUM} TO (?:RS|RE) {_NUM}$")
_FV_SPLIT_LONG = re.compile(
    rf"^FACE VALUE SPLIT(?: \(SUB-DIVISION\))?(?: -)? FROM (?:RS|RE)\.? ?{_NUM}(?: PER SHARE)? "
    rf"TO (?:RS|RE)\.? ?{_NUM}(?: PER SHARE)?$"
)
_BONUS = re.compile(r"^BONUS (\d+):(\d+)$")
_DIVIDEND = re.compile(
    rf"^(?:(?:INT|INTERIM|FIN|FNL|FINAL|SPL|SPECIAL) ?)?(?:DIV|DIVIDEND)(?: -)? (?:RS|RE)\.? ?{_NUM}"
    r"(?: PER SH(?:ARE)?)?$"
)
_MERGER = re.compile(r"^(?:MERGER|AMALGAMATION)$")
_OTHER_PRICE = re.compile(r"^(?:CAPITAL REDUCTION|SCHEME OF ARRANGEMENT)$")
NON_PRICE_ALLOWLIST = frozenset(
    {
        "AGM", "ANNUAL GENERAL MEETING", "EGM", "EXTRA ORDINARY GENERAL MEETING", "INTEREST PAYMENT",
        "REDEMPTION", "STP",
    }
)
PRICE_PART_KINDS = frozenset({"split", "consolidation", "bonus", "dividend"})
ADJUSTABLE_KINDS = frozenset({"split", "consolidation", "bonus", "dividend", "non_price"})

EVENTS_DDL = (
    "CREATE TABLE IF NOT EXISTS corporate_action_events("
    "event_id VARCHAR PRIMARY KEY, nse_symbol VARCHAR NOT NULL, ex_date DATE, record_date DATE, "
    "purpose_norm VARCHAR NOT NULL, parts_json VARCHAR NOT NULL, adjustable BOOLEAN NOT NULL, "
    "row_sha256 VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL)"
)
SIGHTINGS_DDL = (
    "CREATE TABLE IF NOT EXISTS corporate_action_sightings("
    "event_id VARCHAR NOT NULL, file_date DATE NOT NULL, series VARCHAR NOT NULL, "
    "source_sha256 VARCHAR NOT NULL, row_sha256 VARCHAR NOT NULL, PRIMARY KEY(event_id, file_date, series))"
)


class ActionPart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ActionKind
    old_fv: Decimal | None = None
    new_fv: Decimal | None = None
    bonus_new: Decimal | None = None
    bonus_held: Decimal | None = None
    dividend_per_share: Decimal | None = None


class CorporateActionEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    nse_symbol: str
    series_seen: tuple[str, ...]
    ex_date: date | None
    record_date: date | None
    purpose_norm: str
    parts: tuple[ActionPart, ...]
    adjustable: bool
    first_seen_file_date: date
    last_seen_file_date: date
    sightings: int
    evidence_sha256s: tuple[str, ...]


@dataclass(frozen=True)
class DeriveOutcome:
    events_inserted: int
    events_identical: int
    sightings_inserted: int
    conflicts: int


def ensure_ca_tables(store: PilotDataStore) -> None:
    store.ensure_table("corporate_action_events", EVENTS_DDL, key_columns=("event_id",))
    store.ensure_table(
        "corporate_action_sightings", SIGHTINGS_DDL, key_columns=("event_id", "file_date", "series")
    )


def normalise_purpose(purpose: str) -> str:
    return re.sub(r"\s+", " ", purpose.upper()).strip()


def _positive(text: str) -> Decimal | None:
    value = parse_decimal(text, field="purpose_amount")
    return value if value > 0 else None


def _classify(part: str) -> ActionPart:
    match = _FV_SPLIT.match(part) or _FV_SPLIT_LONG.match(part)
    if match:
        old, new = _positive(match.group(1)), _positive(match.group(2))
        if old is None or new is None or old == new:
            return ActionPart(kind="unknown")
        return ActionPart(kind="split" if new < old else "consolidation", old_fv=old, new_fv=new)
    match = _BONUS.match(part)
    if match:
        new, held = _positive(match.group(1)), _positive(match.group(2))
        if new is None or held is None:
            return ActionPart(kind="unknown")
        return ActionPart(kind="bonus", bonus_new=new, bonus_held=held)
    match = _DIVIDEND.match(part)
    if match:
        amount = _positive(match.group(1))
        return ActionPart(kind="dividend", dividend_per_share=amount) if amount else ActionPart(kind="unknown")
    if part.startswith("RIGHTS"):
        return ActionPart(kind="rights")
    if part == "DEMERGER":
        return ActionPart(kind="demerger")
    if _MERGER.match(part):
        return ActionPart(kind="merger")
    if _OTHER_PRICE.match(part):
        return ActionPart(kind="other_price_affecting")
    if part in NON_PRICE_ALLOWLIST:
        return ActionPart(kind="non_price")
    return ActionPart(kind="unknown")


def parse_purpose(purpose: str) -> tuple[ActionPart, ...]:
    norm = normalise_purpose(purpose).replace("/-", "")
    if not norm:
        return (ActionPart(kind="unknown"),)
    if norm.startswith("RIGHTS"):
        return (ActionPart(kind="rights"),)
    pieces = [piece.strip() for piece in re.split(r"/| \+ | AND ", norm) if piece.strip()]
    parts = [_classify(piece) for piece in pieces]
    dividends = [part for part in parts if part.kind == "dividend"]
    if len(dividends) > 1:
        total = sum((part.dividend_per_share or Decimal(0) for part in dividends), Decimal(0))
        merged = ActionPart(kind="dividend", dividend_per_share=total)
        out: list[ActionPart] = []
        placed = False
        for part in parts:
            if part.kind != "dividend":
                out.append(part)
            elif not placed:
                out.append(merged)
                placed = True
        parts = out
    return tuple(parts)


def is_adjustable(parts: tuple[ActionPart, ...]) -> bool:
    return bool(parts) and all(part.kind in ADJUSTABLE_KINDS for part in parts)


def event_id_for(nse_symbol: str, ex_date: date | None, purpose_norm: str) -> str:
    return canonical_sha256(
        {"nse_symbol": nse_symbol, "ex_date": ex_date.isoformat() if ex_date else None, "purpose_norm": purpose_norm}
    )


def _parts_json(parts: tuple[ActionPart, ...]) -> str:
    return json.dumps([part.model_dump(mode="json") for part in parts], sort_keys=True)


def _parts_from_json(text: str) -> tuple[ActionPart, ...]:
    return tuple(ActionPart(**item) for item in json.loads(text))


def derive_corporate_actions(store: PilotDataStore, *, workspace: Literal["india"]) -> DeriveOutcome:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    ensure_pr_tables(store)
    ensure_ca_tables(store)
    raw = store.query(
        "SELECT nse_symbol, series, ex_date, record_date, purpose_raw, file_date, source_sha256, row_sha256 "
        "FROM bhavcopy_ca_raw ORDER BY file_date, series, nse_symbol, row_sha256"
    )
    events: dict[str, dict] = {}
    sightings: list[dict] = []
    for symbol, series, ex_date, record_date, purpose_raw, file_date, source, row_hash in raw:
        norm = normalise_purpose(purpose_raw)
        parts = parse_purpose(purpose_raw)
        event_id = event_id_for(symbol, ex_date, norm)
        if event_id not in events:
            events[event_id] = {
                "event_id": event_id, "nse_symbol": symbol, "ex_date": ex_date, "record_date": record_date,
                "purpose_norm": norm, "parts_json": _parts_json(parts), "adjustable": is_adjustable(parts),
                "source_sha256": source,
                "row_sha256": canonical_sha256(
                    {
                        "event_id": event_id, "parts": json.loads(_parts_json(parts)),
                        "adjustable": is_adjustable(parts),
                    }
                ),
            }
        sightings.append(
            {"event_id": event_id, "file_date": file_date, "series": series, "source_sha256": source,
             "row_sha256": row_hash}
        )
    event_outcome = store.append_rows("corporate_action_events", list(events.values()), check="corporate_action_events")
    sighting_outcome = store.append_rows("corporate_action_sightings", sightings, check="corporate_action_sightings")
    return DeriveOutcome(
        events_inserted=event_outcome.inserted, events_identical=event_outcome.identical,
        sightings_inserted=sighting_outcome.inserted,
        conflicts=event_outcome.conflicts + sighting_outcome.conflicts,
    )


def _events_from_rows(store: PilotDataStore, rows: list[tuple]) -> tuple[CorporateActionEvent, ...]:
    out: list[CorporateActionEvent] = []
    for event_id, symbol, ex_date, record_date, purpose_norm, parts_json, adjustable in rows:
        seen = store.query(
            "SELECT series, file_date, source_sha256 FROM corporate_action_sightings WHERE event_id = ? "
            "ORDER BY file_date, series",
            [event_id],
        )
        if not seen:
            continue
        out.append(
            CorporateActionEvent(
                event_id=event_id, nse_symbol=symbol, series_seen=tuple(sorted({s[0] for s in seen})),
                ex_date=ex_date, record_date=record_date, purpose_norm=purpose_norm,
                parts=_parts_from_json(parts_json), adjustable=bool(adjustable),
                first_seen_file_date=min(s[1] for s in seen), last_seen_file_date=max(s[1] for s in seen),
                sightings=len(seen), evidence_sha256s=tuple(sorted({s[2] for s in seen})),
            )
        )
    return tuple(out)


def events_for_symbol(
    store: PilotDataStore, nse_symbol: str, *, ex_from: date, ex_to: date
) -> tuple[CorporateActionEvent, ...]:
    """Events with ex_date in range, plus events with no ex_date (flagged by ex_date None)."""
    ensure_ca_tables(store)
    rows = store.query(
        "SELECT event_id, nse_symbol, ex_date, record_date, purpose_norm, parts_json, adjustable "
        "FROM corporate_action_events WHERE nse_symbol = ? AND (ex_date IS NULL OR (ex_date >= ? AND ex_date <= ?)) "
        "ORDER BY ex_date NULLS FIRST, purpose_norm",
        [nse_symbol, ex_from, ex_to],
    )
    return _events_from_rows(store, rows)


__all__ = [
    "ActionPart", "CorporateActionEvent", "DeriveOutcome", "derive_corporate_actions", "events_for_symbol",
    "parse_purpose", "normalise_purpose", "is_adjustable",
]

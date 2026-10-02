"""As-of universe filter (PDAT-03), small-cap classification and the 30 percent exposure rule (D-08).

Every price, volume and surveillance input is selected with a single reference date, the
data cutoff. The public `evaluate_universe` fixes the cutoff to the evaluation date; only the
private `_evaluate_universe` accepts another cutoff, so a test can prove a leak would change
the result hash while production code cannot leak.

Liquidity follows review decision D7: a session is known only with a complete, validated
primary bhavcopy plus authoritative trading status. Everything else is unknown. The reported
median uses known sessions only; eligibility uses the conservative median where every
unknown session counts as zero turnover.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .bhavcopy import ensure_bhavcopy_tables, ensure_pr_tables
from .constituents import index_members, latest_index_list
from .core import (
    SURVEILLANCE_HISTORY_CAVEAT,
    CaveatedResult,
    PilotDataError,
    canonical_sha256,
    standard_caveats,
    utc_naive,
)
from .lineage import lineage_for_target
from .models import Lineage
from .sessions import previous_sessions
from .store import PilotDataStore
from .surveillance import snapshot_for, surveillance_entries
from .targets import TargetMember, TargetUniverseResult

SmallCapClass = Literal["small", "not_small", "unclassified"]
Mode = Literal["pilot", "research"]

EVALUATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS universe_evaluations("
    "result_sha256 VARCHAR PRIMARY KEY, workspace VARCHAR NOT NULL, as_of DATE NOT NULL, mode VARCHAR NOT NULL, "
    "policy_sha256 VARCHAR NOT NULL, payload_json VARCHAR NOT NULL, evaluated_at_utc TIMESTAMP NOT NULL, "
    "row_sha256 VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL)"
)


def ensure_universe_tables(store: PilotDataStore) -> None:
    store.ensure_table("universe_evaluations", EVALUATIONS_DDL, key_columns=("result_sha256",))


class UniversePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["pilot-universe/1"] = "pilot-universe/1"
    min_median_traded_value: Decimal = Decimal("50000000")
    lookback_sessions: int = 60
    min_known_sessions: int = 40
    min_price: Decimal = Decimal("50")
    allowed_series: tuple[str, ...] = ("EQ",)
    t2t_series: tuple[str, ...] = ("BE", "BZ")
    smallcap_capital_cap: Decimal = Decimal("0.30")

    def policy_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class TradingStatusSource(Protocol):
    def status(self, isin: str, session: date) -> Literal["trading", "suspended", "not_listed", "unknown"]: ...


class NoTradingStatusSource:
    """Part 1 default: there is no authoritative listing or suspension source yet."""

    def status(self, isin: str, session: date) -> Literal["trading", "suspended", "not_listed", "unknown"]:
        return "unknown"


class LiquidityObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session: date
    kind: Literal["known_traded", "known_zero", "unknown"]
    value: Decimal | None = None


@dataclass(frozen=True)
class LiquidityOutcome:
    known_sessions: int
    unknown_sessions: int
    reported_median: Decimal | None
    eligibility_median: Decimal | None
    reasons: tuple[str, ...]


class UniverseDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    anchor_isin: str
    isin_on_date: str | None
    stock_code: str
    eligible: bool
    reasons: tuple[str, ...]
    close: Decimal | None
    median_traded_value: Decimal | None
    eligibility_median_traded_value: Decimal | None
    known_sessions: int
    unknown_sessions: int
    excluded_prelisting_sessions: int
    smallcap_class: SmallCapClass


class UniverseResult(CaveatedResult):
    as_of: date
    mode: Mode
    policy_sha256: str
    decisions: tuple[UniverseDecision, ...]
    eligible_isins: tuple[str, ...]
    exclusions_by_reason: dict[str, int]
    input_hashes: dict[str, str]
    result_sha256: str


def median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def liquidity_check(observations: Sequence[LiquidityObservation], *, policy: UniversePolicy) -> LiquidityOutcome:
    """Pure D7 liquidity rule over the non-excluded sessions of a window."""
    known = [o for o in observations if o.kind != "unknown"]
    known_values = [(o.value if o.value is not None else Decimal(0)) for o in known]
    eligibility_values = [
        (o.value if o.value is not None else Decimal(0)) if o.kind != "unknown" else Decimal(0)
        for o in observations
    ]
    reported = median(known_values)
    eligibility = median(eligibility_values)
    reasons: list[str] = []
    if len(known) < policy.min_known_sessions:
        reasons.append("insufficient_history")
    if (eligibility if eligibility is not None else Decimal(0)) < policy.min_median_traded_value:
        reasons.append("adv_below_min")
    return LiquidityOutcome(
        known_sessions=len(known), unknown_sessions=len(observations) - len(known), reported_median=reported,
        eligibility_median=eligibility, reasons=tuple(reasons),
    )


def classify_liquidity_sessions(
    store: PilotDataStore,
    lineage: Lineage,
    *,
    data_cutoff: date,
    lookback: int,
    status_source: TradingStatusSource,
) -> tuple[LiquidityObservation, ...]:
    """One observation per non-excluded session in the lookback window ending at the cutoff."""
    ensure_bhavcopy_tables(store)
    sessions = previous_sessions(store, data_cutoff, lookback)
    live = [day for day in sessions if lineage.isin_on(day) is not None]
    if not live:
        return ()
    low, high = live[0], live[-1]
    primary_dates = {
        row[0]: row[1]
        for row in store.query(
            "SELECT trade_date, max(CASE WHEN file_kind = 'udiff' THEN 1 ELSE 0 END) FROM bhavcopy_files "
            "WHERE file_kind IN ('udiff', 'cm_legacy') AND trade_date >= ? AND trade_date <= ? GROUP BY trade_date",
            [low, high],
        )
    }
    isins = sorted({segment.isin for segment in lineage.segments})
    marks = ", ".join("?" for _ in isins)
    values: dict[tuple[date, str], Decimal] = {}
    for kind in ("udiff", "cm_legacy"):
        for day, isin, total in store.query(
            f"SELECT trade_date, isin, sum(traded_value) FROM bhavcopy_bars WHERE file_kind = ? "
            f"AND trade_date >= ? AND trade_date <= ? AND isin IN ({marks}) GROUP BY trade_date, isin",
            [kind, low, high, *isins],
        ):
            if primary_dates.get(day) is not None and (primary_dates[day] == 1) == (kind == "udiff"):
                values[(day, isin)] = Decimal(total)
    tainted: list[tuple[date, str | None, str | None]] = list(
        store.query(
            "SELECT date_from, isin, nse_symbol FROM quarantine_records WHERE check_name = 'bhavcopy_consistency' "
            "AND date_from >= ? AND date_from <= ?",
            [low, high],
        )
    )
    observations: list[LiquidityObservation] = []
    for day in live:
        isin = lineage.isin_on(day)
        assert isin is not None
        validated = day in primary_dates and not any(
            q_day == day and (q_isin == isin or (q_symbol is not None and q_symbol == lineage.symbol_on(day)))
            for q_day, q_isin, q_symbol in tainted
        )
        if not validated:
            observations.append(LiquidityObservation(session=day, kind="unknown"))
        elif (day, isin) in values:
            observations.append(LiquidityObservation(session=day, kind="known_traded", value=values[(day, isin)]))
        elif status_source.status(isin, day) == "trading":
            observations.append(LiquidityObservation(session=day, kind="known_zero", value=Decimal(0)))
        else:
            observations.append(LiquidityObservation(session=day, kind="unknown"))
    return tuple(observations)


# --------------------------------------------------------------------------- small caps
def classify_smallcap(store: PilotDataStore, targets: TargetUniverseResult) -> dict[str, SmallCapClass]:
    """D-08: today's Smallcap 250 is small; anything that cannot be classified counts as small for the cap."""
    ensure_pr_tables(store)
    nifty = latest_index_list(store, "nifty500")
    small = latest_index_list(store, "smallcap250")
    nifty_isins = {m.isin for m in index_members(store, nifty)} if nifty else set()
    small_isins = {m.isin for m in index_members(store, small)} if small else set()
    out: dict[str, SmallCapClass] = {}
    for member in targets.members:
        if member.kind == "nifty500":
            if small is None or nifty is None:
                out[member.anchor_isin] = "unclassified"
            elif member.anchor_isin in small_isins:
                out[member.anchor_isin] = "small"
            elif member.anchor_isin in nifty_isins:
                out[member.anchor_isin] = "not_small"
            else:
                out[member.anchor_isin] = "unclassified"
            continue
        found = store.query(
            "SELECT underlying FROM bhavcopy_etf_info WHERE nse_symbol = ? ORDER BY file_date DESC LIMIT 1",
            [member.nse_symbol],
        )
        underlying = found[0][0] if found else None
        if underlying is None:
            out[member.anchor_isin] = "unclassified"
        elif "SMALLCAP" in underlying.upper() or "MICROCAP" in underlying.upper():
            out[member.anchor_isin] = "small"
        else:
            out[member.anchor_isin] = "not_small"
    return out


class SmallCapExposureCheck(CaveatedResult):
    capital: Decimal
    small_value: Decimal
    small_share: Decimal
    limit: Decimal
    passed: bool
    counted_isins: tuple[str, ...]


def check_smallcap_exposure(
    position_values: Mapping[str, Decimal],
    *,
    capital: Decimal,
    classification: Mapping[str, SmallCapClass],
    policy: UniversePolicy,
    workspace: Literal["india"],
) -> SmallCapExposureCheck:
    """The PDAT-03 small-cap rule. It checks exposure and never sizes a position."""
    if capital <= 0:
        raise PilotDataError("exposure_capital_invalid", "capital must be positive")
    if any(value < 0 for value in position_values.values()):
        raise PilotDataError("exposure_value_invalid", "position values cannot be negative")
    counted = sorted(
        isin
        for isin, value in position_values.items()
        if value > 0 and classification.get(isin, "unclassified") in ("small", "unclassified")
    )
    small_value = sum((position_values[isin] for isin in counted), Decimal(0))
    return SmallCapExposureCheck(
        workspace=workspace, caveats=standard_caveats(), capital=capital, small_value=small_value,
        small_share=small_value / capital, limit=policy.smallcap_capital_cap,
        passed=small_value <= capital * policy.smallcap_capital_cap, counted_isins=tuple(counted),
    )


# --------------------------------------------------------------------------- evaluation
def _evaluate_universe(
    store: PilotDataStore,
    *,
    as_of: date,
    data_cutoff: date,
    targets: TargetUniverseResult,
    policy: UniversePolicy,
    mode: Mode,
    allow_missing_surveillance_before: date | None,
    workspace: Literal["india"],
    status_source: TradingStatusSource,
) -> UniverseResult:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    ensure_universe_tables(store)
    ensure_bhavcopy_tables(store)
    surveillance_applied = True
    if mode == "research" and allow_missing_surveillance_before is not None \
            and data_cutoff < allow_missing_surveillance_before:
        surveillance_applied = False
    asm_ref = gsm_ref = None
    asm_entries: tuple = ()
    gsm_entries: tuple = ()
    input_hashes: dict[str, str] = {"targets": targets.target_sha256}
    if surveillance_applied:
        asm_ref = snapshot_for(store, "asm", data_cutoff)
        gsm_ref = snapshot_for(store, "gsm", data_cutoff)
        if asm_ref is None or gsm_ref is None:
            raise PilotDataError(
                "surveillance_snapshot_missing",
                f"no ASM and GSM snapshot effective exactly {data_cutoff.isoformat()}",
            )
        asm_entries = surveillance_entries(store, asm_ref)
        gsm_entries = surveillance_entries(store, gsm_ref)
        input_hashes["asm"] = asm_ref.source_sha256
        input_hashes["gsm"] = gsm_ref.source_sha256
    classification = classify_smallcap(store, targets)
    lineage_as_of = max(targets.as_of, data_cutoff)
    decisions: list[UniverseDecision] = []
    for member in targets.members:
        decisions.append(
            _decide(
                store, member=member, data_cutoff=data_cutoff, lineage_as_of=lineage_as_of, policy=policy,
                workspace=workspace, status_source=status_source, asm=asm_entries, gsm=gsm_entries,
                smallcap_class=classification.get(member.anchor_isin, "unclassified"), input_hashes=input_hashes,
            )
        )
    decisions.sort(key=lambda d: d.anchor_isin)
    reasons: dict[str, int] = {}
    for decision in decisions:
        for reason in decision.reasons:
            reasons[reason] = reasons.get(reason, 0) + 1
    caveats = standard_caveats(*(() if surveillance_applied else (SURVEILLANCE_HISTORY_CAVEAT,)))
    policy_hash = policy.policy_sha256()
    content = {
        "as_of": as_of.isoformat(),
        "mode": mode,
        "policy_sha256": policy_hash,
        "decisions": [d.model_dump(mode="json") for d in decisions],
        "input_hashes": dict(sorted(input_hashes.items())),
        "surveillance_applied": surveillance_applied,
    }
    result = UniverseResult(
        workspace=workspace, caveats=caveats, as_of=as_of, mode=mode, policy_sha256=policy_hash,
        decisions=tuple(decisions), eligible_isins=tuple(d.anchor_isin for d in decisions if d.eligible),
        exclusions_by_reason=dict(sorted(reasons.items())), input_hashes=dict(sorted(input_hashes.items())),
        result_sha256=canonical_sha256(content),
    )
    store.append_rows(
        "universe_evaluations",
        [
            {
                "result_sha256": result.result_sha256, "workspace": store.workspace, "as_of": as_of, "mode": mode,
                "policy_sha256": policy_hash, "payload_json": json.dumps(result.model_dump(mode="json"), sort_keys=True),
                "evaluated_at_utc": utc_naive(datetime.now(timezone.utc)), "row_sha256": result.result_sha256,
                "source_sha256": targets.target_sha256,
            }
        ],
        check="universe_evaluation",
    )
    return result


def _decide(
    store: PilotDataStore,
    *,
    member: TargetMember,
    data_cutoff: date,
    lineage_as_of: date,
    policy: UniversePolicy,
    workspace: Literal["india"],
    status_source: TradingStatusSource,
    asm: Sequence,
    gsm: Sequence,
    smallcap_class: SmallCapClass,
    input_hashes: dict[str, str],
) -> UniverseDecision:
    def blank(reasons: tuple[str, ...], isin: str | None = None, **extra) -> UniverseDecision:
        base = dict(
            anchor_isin=member.anchor_isin, isin_on_date=isin, stock_code=member.stock_code, eligible=False,
            reasons=reasons, close=None, median_traded_value=None, eligibility_median_traded_value=None,
            known_sessions=0, unknown_sessions=0, excluded_prelisting_sessions=0, smallcap_class=smallcap_class,
        )
        base.update(extra)
        return UniverseDecision(**base)

    try:
        lineage = lineage_for_target(store, member, as_of=lineage_as_of, workspace=workspace)
    except PilotDataError as exc:
        if exc.code != "lineage_anchor_not_observed":
            raise
        return blank(("lineage_anchor_not_observed",))
    input_hashes[f"lineage:{member.anchor_isin}"] = lineage.content_sha256()
    reasons: list[str] = []
    isin = lineage.isin_on(data_cutoff)
    if isin is None:
        if lineage.unresolved_before is not None and data_cutoff < lineage.unresolved_before:
            return blank(("isin_unresolved_on_date",))
        return blank(("not_listed",))
    bars = store.query(
        "SELECT b.series, b.close FROM bhavcopy_bars b WHERE b.isin = ? AND b.trade_date = ? "
        "AND b.file_kind = CASE WHEN EXISTS (SELECT 1 FROM bhavcopy_files f WHERE f.trade_date = ? "
        "AND f.file_kind = 'udiff') THEN 'udiff' ELSE 'cm_legacy' END ORDER BY CASE b.series WHEN ? THEN 0 ELSE 1 END, "
        "b.series",
        [isin, data_cutoff, data_cutoff, member.anchor_series],
    )
    close: Decimal | None = None
    if not bars:
        reasons.append("no_bar_on_date")
    else:
        series, close = bars[0][0], Decimal(bars[0][1])
        if series in policy.t2t_series:
            reasons.append("trade_for_trade")
        elif series not in policy.allowed_series:
            reasons.append("series_not_eq")
        if close < policy.min_price:
            reasons.append("price_below_min")
    observations = classify_liquidity_sessions(
        store, lineage, data_cutoff=data_cutoff, lookback=policy.lookback_sessions, status_source=status_source
    )
    outcome = liquidity_check(observations, policy=policy)
    reasons.extend(outcome.reasons)
    sessions = previous_sessions(store, data_cutoff, policy.lookback_sessions)
    excluded = len(sessions) - len(observations)
    if any(e.isin == isin or (e.isin is None and e.nse_symbol == lineage.symbol_on(data_cutoff)) for e in asm):
        reasons.append("surveillance_asm")
    if any(e.isin == isin or (e.isin is None and e.nse_symbol == lineage.symbol_on(data_cutoff)) for e in gsm):
        reasons.append("surveillance_gsm")
    return blank(
        tuple(reasons), isin, eligible=not reasons, close=close, median_traded_value=outcome.reported_median,
        eligibility_median_traded_value=outcome.eligibility_median, known_sessions=outcome.known_sessions,
        unknown_sessions=outcome.unknown_sessions, excluded_prelisting_sessions=excluded,
    )


def evaluate_universe(
    store: PilotDataStore,
    *,
    as_of: date,
    targets: TargetUniverseResult,
    policy: UniversePolicy,
    mode: Mode,
    allow_missing_surveillance_before: date | None,
    workspace: Literal["india"],
    status_source: TradingStatusSource,
) -> UniverseResult:
    """Evaluate the universe as of a date. The data cutoff is the date itself, never anything later."""
    return _evaluate_universe(
        store, as_of=as_of, data_cutoff=as_of, targets=targets, policy=policy, mode=mode,
        allow_missing_surveillance_before=allow_missing_surveillance_before, workspace=workspace,
        status_source=status_source,
    )

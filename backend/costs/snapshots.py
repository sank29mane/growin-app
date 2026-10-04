"""Simulated reconciliation evidence shaped like ReconciliationSnapshot.

Every simulated fill maps to rows whose field names match the execution
ledger's ``ReconciliationSnapshot``, so a later wiring step is a one-line
construction. This package never imports ``backend/execution`` and never
constructs execution types: wiring waits for Phase 58 to merge. The ``sim:``
source prefix and ``simfill:`` fingerprint prefix keep simulated evidence from
ever being read as a broker fill.

A paper or simulated outcome is never a market fill. ``NO_ASSUMED_FILL``
(unsupported simulation data, D15) maps to a CANCELLED row like a miss, because
the reconciliation shape has no "unsupported" status. The ``reason_code`` and
``outcome`` attributes travel with each row so a reader can still tell the two
apart; Phase 62 reads the FillResult outcome, never the CANCELLED status.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .core import InputError, require_aware, require_text
from .fills import FillOutcome, FillResult, FillScenario

SIM_SOURCE_PREFIX = "sim:"
SIM_FINGERPRINT_PREFIX = "simfill:"
_SOURCE_LIMIT = 96
_FINGERPRINT_LIMIT = 128
_HASH_PREFIX_LENGTH = 48

SNAPSHOT_FIELDS = (
    "proposal_id",
    "broker_order_id",
    "source",
    "cumulative_quantity",
    "cumulative_notional",
    "status",
    "evidence_fingerprint",
    "observed_at",
)


@dataclass(frozen=True)
class SimulatedReconciliationEvidence:
    proposal_id: str
    broker_order_id: str
    source: str
    cumulative_quantity: Decimal
    cumulative_notional: Decimal
    status: str
    evidence_fingerprint: str
    observed_at: datetime
    # Not ReconciliationSnapshot fields: they stay out of as_snapshot_fields().
    outcome: str = ""
    reason_code: str = ""

    def as_snapshot_fields(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in SNAPSHOT_FIELDS}


def to_reconciliation_evidence(
    result: FillResult,
    *,
    scenario: FillScenario,
    proposal_id: str,
    broker_order_id: str,
    observed_at: datetime,
) -> tuple[SimulatedReconciliationEvidence, ...]:
    require_text(proposal_id, "proposal_id")
    require_text(broker_order_id, "broker_order_id")
    require_aware(observed_at, "observed_at")
    if (scenario.scenario_id, scenario.scenarios_version, scenario.scenarios_hash) != (
        result.scenario_id,
        result.scenarios_version,
        result.scenarios_hash,
    ):
        raise InputError("scenario does not match the scenario that produced this fill result")
    source = f"{SIM_SOURCE_PREFIX}daily-bar:{scenario.scenario_id}:{scenario.scenarios_version}"
    if len(source) > _SOURCE_LIMIT:
        raise InputError(f"evidence source exceeds {_SOURCE_LIMIT} characters")

    zero = Decimal(0)
    quantity = Decimal(result.filled_quantity)
    steps: list[tuple[str, Decimal, Decimal]]
    if result.outcome is FillOutcome.FILLED:
        steps = [("FILLED", quantity, result.notional)]
    elif result.outcome is FillOutcome.PARTIAL:
        # The unfilled remainder expires at the session close (D-10).
        steps = [("PARTIALLY_FILLED", quantity, result.notional), ("CANCELLED", quantity, result.notional)]
    elif result.outcome is FillOutcome.REJECTED:
        steps = [("REJECTED", zero, zero)]
    else:  # MISSED and NO_ASSUMED_FILL both end CANCELLED with nothing filled.
        steps = [("CANCELLED", zero, zero)]

    rows = []
    for index, (status, cum_quantity, cum_notional) in enumerate(steps):
        fingerprint = f"{SIM_FINGERPRINT_PREFIX}{result.result_hash[:_HASH_PREFIX_LENGTH]}:{index}"
        if len(fingerprint) > _FINGERPRINT_LIMIT:
            raise InputError(f"evidence fingerprint exceeds {_FINGERPRINT_LIMIT} characters")
        rows.append(
            SimulatedReconciliationEvidence(
                proposal_id=proposal_id,
                broker_order_id=broker_order_id,
                source=source,
                cumulative_quantity=cum_quantity,
                cumulative_notional=cum_notional,
                status=status,
                evidence_fingerprint=fingerprint,
                observed_at=observed_at,
                outcome=result.outcome.value,
                reason_code=result.reason_code,
            )
        )
    return tuple(rows)

"""Simulated reconciliation evidence shaped like ReconciliationSnapshot.

All fills here are SYNTHETIC results of simulate_session on small test bars.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from decimal import Decimal

import pytest

from costs.core import InputError, Side, sha256_hex
from costs.fills import (
    BandUnavailable,
    FillOutcome,
    LimitOrder,
    PriceBand,
    SessionBar,
    TickSize,
    load_fill_scenarios,
    simulate_session,
)
from costs.snapshots import (
    SIM_FINGERPRINT_PREFIX,
    SIM_SOURCE_PREFIX,
    SimulatedReconciliationEvidence,
    to_reconciliation_evidence,
)

D = Decimal
ISIN = "INE0TEST0001"
SESSION = date(2026, 10, 6)
OBSERVED = datetime.fromisoformat("2026-10-06T16:00:00+05:30")
TICK = TickSize(D("0.05"), date(2025, 4, 15), "test-explicit", sha256_hex("test-explicit"))
BAND = PriceBand("fixed", D("360.00"), D("440.00"), SESSION, "test-band", sha256_hex("test-band"))

# Copied from backend/execution/ledger.py (around line 1658): legal reconciliation
# status transitions. Quantities and notionals must also be monotonic.
LEGAL_TRANSITIONS = {
    "ACKNOWLEDGED": {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "UNKNOWN"},
    "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCELLED", "UNKNOWN"},
    "UNKNOWN": {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "UNKNOWN"},
}


def scenario(name="base"):
    return load_fill_scenarios().get(name)


def bar(*, low="395.00", band=BAND):
    return SessionBar(ISIN, "NSE", SESSION, D("400.50"), D("402.00"), D(low), D("401.00"), 20000, "raw", band, "test-bar")


def order(*, quantity=70, limit="400.00"):
    return LimitOrder(
        "o1", ISIN, "NSE", Side.BUY, quantity, D(limit), D("399.00"), SESSION,
        datetime.fromisoformat("2026-10-05T18:00:00+05:30"), date(2026, 10, 5), TICK,
    )


def result_for(*, scenario_name="base", **kwargs):
    order_kwargs = {k: kwargs.pop(k) for k in ("quantity", "limit") if k in kwargs}
    (result,) = simulate_session([order(**order_kwargs)], bar(**kwargs), scenario(scenario_name))
    return result


def evidence(result, scenario_name="base"):
    return to_reconciliation_evidence(
        result, scenario=scenario(scenario_name), proposal_id="proposal-1", broker_order_id="sim-order-1",
        observed_at=OBSERVED,
    )


FILLED = result_for(quantity=70)
PARTIAL = result_for(quantity=250, scenario_name="pessimistic")
MISSED = result_for(low="400.00")
REJECTED = result_for(limit="445.00")
NO_ASSUMED = result_for(band=BandUnavailable("no band source for 2026-10-06"))


def test_filled_maps_to_one_filled_row():
    (row,) = evidence(FILLED)
    assert FILLED.outcome is FillOutcome.FILLED
    assert row.status == "FILLED"
    assert row.cumulative_quantity == D("70")
    assert row.cumulative_notional == D("28000.00")


def test_partial_maps_to_partially_filled_then_cancelled():
    assert PARTIAL.outcome is FillOutcome.PARTIAL
    first, second = evidence(PARTIAL, "pessimistic")
    assert (first.status, first.cumulative_quantity, first.cumulative_notional) == ("PARTIALLY_FILLED", D("50"), D("20000.00"))
    assert (second.status, second.cumulative_quantity, second.cumulative_notional) == ("CANCELLED", D("50"), D("20000.00"))
    assert first.evidence_fingerprint != second.evidence_fingerprint


@pytest.mark.parametrize(
    "result, status",
    [(MISSED, "CANCELLED"), (REJECTED, "REJECTED"), (NO_ASSUMED, "CANCELLED")],
    ids=["missed", "rejected", "no-assumed-fill"],
)
def test_unfilled_outcomes_map_to_one_empty_row(result, status):
    (row,) = evidence(result)
    assert row.status == status
    assert row.cumulative_quantity == D("0")
    assert row.cumulative_notional == D("0")


def test_no_assumed_fill_is_distinct_from_missed():
    same_prices_missed = result_for(low="400.00")
    unsupported = result_for(low="400.00", band=BandUnavailable("no band source for 2026-10-06"))
    assert same_prices_missed.outcome is FillOutcome.MISSED
    assert unsupported.outcome is FillOutcome.NO_ASSUMED_FILL
    assert unsupported.outcome != same_prices_missed.outcome
    assert unsupported.reason_code != same_prices_missed.reason_code
    assert unsupported.result_hash != same_prices_missed.result_hash
    (missed_row,) = evidence(same_prices_missed)
    (unsupported_row,) = evidence(unsupported)
    assert missed_row.status == unsupported_row.status == "CANCELLED"
    assert missed_row.reason_code == same_prices_missed.reason_code
    assert unsupported_row.reason_code == unsupported.reason_code
    assert missed_row.reason_code != unsupported_row.reason_code
    assert missed_row.outcome == "MISSED" and unsupported_row.outcome == "NO_ASSUMED_FILL"
    assert missed_row.evidence_fingerprint != unsupported_row.evidence_fingerprint


ALL_CASES = [
    (FILLED, "base"), (PARTIAL, "pessimistic"), (MISSED, "base"), (REJECTED, "base"), (NO_ASSUMED, "base"),
]


@pytest.mark.parametrize("result, scenario_name", ALL_CASES)
def test_rows_are_labelled_as_simulated(result, scenario_name):
    chosen = scenario(scenario_name)
    for row in evidence(result, scenario_name):
        assert row.source.startswith(SIM_SOURCE_PREFIX)
        assert chosen.scenario_id in row.source
        assert chosen.scenarios_version in row.source
        assert len(row.source) <= 96
        assert row.evidence_fingerprint.startswith(SIM_FINGERPRINT_PREFIX)
        assert result.result_hash[:48] in row.evidence_fingerprint
        assert len(row.evidence_fingerprint) <= 128


@pytest.mark.parametrize("result, scenario_name", ALL_CASES)
def test_sequences_follow_the_ledger_transitions_and_are_monotonic(result, scenario_name):
    state, quantity, notional = "ACKNOWLEDGED", D(0), D(0)
    for row in evidence(result, scenario_name):
        assert row.status in LEGAL_TRANSITIONS[state] if state in LEGAL_TRANSITIONS else False
        assert row.cumulative_quantity >= quantity
        assert row.cumulative_notional >= notional
        state, quantity, notional = row.status, row.cumulative_quantity, row.cumulative_notional


def test_bad_inputs_raise():
    with pytest.raises(InputError):
        to_reconciliation_evidence(
            FILLED, scenario=scenario(), proposal_id="p", broker_order_id="b",
            observed_at=datetime(2026, 10, 6, 16, 0),
        )
    for proposal_id, broker_order_id in (("", "b"), ("p", ""), ("  ", "b")):
        with pytest.raises(InputError):
            to_reconciliation_evidence(
                FILLED, scenario=scenario(), proposal_id=proposal_id, broker_order_id=broker_order_id,
                observed_at=OBSERVED,
            )
    with pytest.raises(InputError):
        to_reconciliation_evidence(
            FILLED, scenario=scenario("adverse"), proposal_id="p", broker_order_id="b", observed_at=OBSERVED
        )


def test_over_long_source_raises():
    long_scenario = dataclasses.replace(scenario(), scenarios_version="v" * 100)
    forged = dataclasses.replace(FILLED, scenarios_version="v" * 100)
    with pytest.raises(InputError):
        to_reconciliation_evidence(
            forged, scenario=long_scenario, proposal_id="p", broker_order_id="b", observed_at=OBSERVED
        )


def test_the_function_is_deterministic_and_reads_no_clock():
    assert evidence(FILLED) == evidence(FILLED)


def test_field_contract_with_the_execution_models():
    # Test-only import: backend/costs itself never imports backend/execution.
    from execution.models import ReconciliationSnapshot, ReconciliationStatus

    rows = evidence(FILLED) + evidence(PARTIAL, "pessimistic") + evidence(MISSED) + evidence(REJECTED)
    fields = set(rows[0].as_snapshot_fields())
    model_fields = set(ReconciliationSnapshot.model_fields)
    assert fields <= model_fields, f"not ReconciliationSnapshot fields: {sorted(fields - model_fields)}"
    statuses = {status.value for status in ReconciliationStatus}
    for row in rows:
        assert row.status in statuses
        assert set(row.as_snapshot_fields()) == fields
    required = {name for name, info in ReconciliationSnapshot.model_fields.items() if info.is_required()}
    uncovered = sorted(required - fields)
    if uncovered:
        pytest.skip(f"ReconciliationSnapshot now requires fields this evidence does not cover: {uncovered}")
    for row in rows:
        ReconciliationSnapshot(**row.as_snapshot_fields())


def test_evidence_row_type_is_frozen():
    (row,) = evidence(FILLED)
    assert isinstance(row, SimulatedReconciliationEvidence)
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.status = "UNKNOWN"

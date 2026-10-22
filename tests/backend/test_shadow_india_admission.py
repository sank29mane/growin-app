"""Phase 64-07 Task 1: the SHADOW admission adapter, against the real gate and the real guard.

Every value is synthetic. No broker, relay, VM or private/ directory is touched: the private
config lives in a tmp dir, quotes are samples a test builds, and the simulation ledger is a file
under ``tmp_path``. Helpers at the top are reused by ``test_execution_shadow_sim_account.py``.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

import execution.service as service_module
from execution import ApprovalConflict, ExecutionLedger, ExecutionService, VenueBinding
from execution.india_guard import IndiaAdmissionGuard
from execution.models import AdmissionDecision, OrderIntent
from execution.venue import (
    ADMISSIBLE_PRICE_SOURCES,
    PRICE_SOURCE_OPERATOR_RECORDED,
    PRICE_SOURCE_RELAY_QUOTE,
    VENUE_T212_PRACTICE,
    admissible_price_sources,
)
from india_limits_support import NOW, SYMBOL, india_private_dir, make_evidence, make_guard
from regime_testkit import bound_regime_fields, shipped_map
from shadow_india.admission import (
    AuditEntry,
    AuditPage,
    MintUnresolved,
    QuoteSample,
    ShadowAdmissionAdapter,
    ShadowPreparation,
    VmDecision,
    open_simulation_ledger,
)

CAPITAL_CAP = "50000.00"
PER_POSITION_CAP = "20000.00"
SEED = Decimal("50000")
RUN_ID = "run1"


def run(coro):
    return asyncio.run(coro)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def samples(
    now: datetime,
    count: int = 3,
    *,
    bid: str = "99.90",
    ask: str = "100.00",
    age: int = 2,
    quote_observed_at: datetime = NOW,
) -> list[QuoteSample]:
    """``count`` distinct relay readings ending ``age`` seconds before ``now``, each pinned."""

    out = []
    for index in range(count):
        step = Decimal(index) * Decimal("0.01")
        out.append(
            QuoteSample(
                sha(f"raw-{index}"),
                now - timedelta(seconds=age * (count - index)),
                Decimal(bid) + step,
                Decimal(ask) + step,
                make_evidence(observed_at=quote_observed_at, ask=Decimal(ask), bid=Decimal(bid)),
            )
        )
    return out


class FixedFill:
    """A stand-in PreFlightSimulator that returns the fill price a test chooses."""

    def __init__(self, price: str) -> None:
        self.price = price
        self.calls = 0

    def simulate_execution(self, side, quantity, tick_window, portfolio_state):
        self.calls += 1
        return {"simulated_fill_price": self.price, "simulator_drawdown_pct": 0.0}


@dataclass
class Rig:
    root: Path
    private: Path
    ledger: ExecutionLedger
    guard: IndiaAdmissionGuard
    adapter: ShadowAdmissionAdapter

    def close(self) -> None:
        self.ledger.close()


def make_rig(
    tmp_path: Path,
    *,
    seed: Decimal = SEED,
    capital_cap: str = CAPITAL_CAP,
    per_position_cap: str = PER_POSITION_CAP,
    simulator: Any = None,
    risk_gate: Any = None,
    run_id: str = RUN_ID,
) -> Rig:
    private = india_private_dir(tmp_path, capital_cap=capital_cap, per_position_cap=per_position_cap)
    root = tmp_path / "sim"
    ledger = open_simulation_ledger(root, run_id, seed)
    guard = make_guard(ledger, private)
    adapter = ShadowAdmissionAdapter(ledger, guard, simulator=simulator, risk_gate=risk_gate)
    return Rig(root, private, ledger, guard, adapter)


def reopen(rig: Rig, *, seed: Decimal = SEED, run_id: str = RUN_ID) -> Rig:
    rig.close()
    ledger = open_simulation_ledger(rig.root, run_id, seed)
    guard = make_guard(ledger, rig.private)
    return Rig(rig.root, rig.private, ledger, guard, ShadowAdmissionAdapter(ledger, guard))


def buy(rig: Rig, proposal_id: str, *, quantity: int = 2, limit: str = "100.00", symbol: str = SYMBOL):
    return rig.adapter.build_intent(
        proposal_id, symbol=symbol, side="BUY", quantity=quantity, limit_price=limit
    )


def prepare_buy(rig: Rig, proposal_id: str, *, quantity: int = 2, limit: str = "100.00", **kw):
    now = datetime.now(timezone.utc)
    return run(
        rig.adapter.prepare(
            buy(rig, proposal_id, quantity=quantity, limit=limit), samples(now, **kw)
        )
    )


@pytest.fixture
def evidence_spy(monkeypatch):
    """The admission evidence dicts ``admit`` hashed, in order (the ledger stores only the hash)."""

    seen: list[dict] = []
    original = service_module.canonical_json

    def spy(value):
        if isinstance(value, Mapping) and "simulator" in value and "risk" in value:
            seen.append(json.loads(original(value)))
        return original(value)

    monkeypatch.setattr(service_module, "canonical_json", spy)
    return seen


# --- parity with the app ------------------------------------------------------------


def _service_keywords(path: Path, marker: str) -> set[str]:
    tree = ast.parse(path.read_text())
    found: list[set[str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "ExecutionService"
            and any(keyword.arg == marker for keyword in node.keywords)
        ):
            found.append({keyword.arg for keyword in node.keywords})
    assert len(found) == 1, "expected exactly one construction"
    return found[0]


def test_the_adapter_builds_the_service_with_every_control_the_app_builds(tmp_path):
    root = Path(__file__).resolve().parents[2] / "backend"
    app = _service_keywords(root / "app_context.py", "require_runtime_preflight")
    mine = _service_keywords(root / "shadow_india" / "admission.py", "require_runtime_preflight")
    # The only app arguments the simulation leaves out: the signing service (the simulation
    # account has no key) and the test-only price-source opt-in (default False in both).
    assert app - mine == {"approval_service", "allow_test_price_sources"}
    assert mine - app == set()
    rig = make_rig(tmp_path)
    try:
        service = rig.adapter.service
        from simulation import PreFlightSimulator, RiskSwarmGate

        assert type(service._simulator) is PreFlightSimulator
        assert type(service._risk_gate) is RiskSwarmGate
        assert service._require_runtime_preflight is True
        assert service._india_guard is rig.guard
        assert service._dispatcher is None
        assert service._regime_severity_map.policy_hash == shipped_map().policy_hash
        assert service.admissible_price_sources == frozenset({PRICE_SOURCE_RELAY_QUOTE})
    finally:
        rig.close()


def test_the_adapter_refuses_an_ordinary_ledger(tmp_path):
    private = india_private_dir(tmp_path)
    ledger = ExecutionLedger(tmp_path / "paper.sqlite3", workspace="india", require_approval=True)
    try:
        with pytest.raises(ValueError):
            ShadowAdmissionAdapter(ledger, make_guard(ledger, private))
    finally:
        ledger.close()


# --- the admission path with the real gate and guard ----------------------------------


def test_a_good_buy_is_admitted_and_reserved_at_its_limit(tmp_path):
    rig = make_rig(tmp_path)
    try:
        admission = prepare_buy(rig, "ok-1")
        assert admission.decision is AdmissionDecision.ADMITTED
        assert admission.price == Decimal("100.00") and admission.notional == Decimal("200.00")
        reservation = rig.ledger.get_reservation("ok-1")
        assert reservation is not None and reservation.reserved == Decimal("200.00")
        assert rig.ledger.simulated_cash_view().reserved == Decimal("200.00")
    finally:
        rig.close()


def test_both_slippage_figures_are_recorded_when_both_inside(tmp_path, evidence_spy):
    rig = make_rig(tmp_path)
    try:
        admission = prepare_buy(rig, "both-ok")
        assert admission.decision is AdmissionDecision.ADMITTED
        india = evidence_spy[-1]["india"]
        assert Decimal(india["preflight"]) == Decimal(india["slippage_bps"])
        assert Decimal(india["slippage_reference"]) == Decimal("0")  # limit == ask
        assert "slippage_denied_by" not in india
    finally:
        rig.close()


def test_a_simulator_beyond_the_cap_denies_on_the_preflight_input(tmp_path, evidence_spy):
    # Fill 100.30 against ask 100.00 is 30 bps; the limit (100.00) is inside.
    rig = make_rig(tmp_path, simulator=FixedFill("100.30"))
    try:
        admission = prepare_buy(rig, "pre-bad")
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "SLIPPAGE_LIMIT"
        assert evidence_spy[-1]["india"]["slippage_denied_by"] == "preflight"
        assert rig.ledger.get_reservation("pre-bad") is None
        assert rig.ledger.simulated_cash_view().reserved == 0
        assert rig.ledger.get_order("pre-bad").state == "REJECTED"
    finally:
        rig.close()


def test_a_limit_beyond_the_cap_denies_on_the_at_limit_input(tmp_path, evidence_spy):
    # The simulator fill is the ask (0 bps); the limit 100.30 is 30 bps above it.
    rig = make_rig(tmp_path, simulator=FixedFill("100.00"))
    try:
        admission = prepare_buy(rig, "lim-bad", limit="100.30")
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "SLIPPAGE_LIMIT"
        india = evidence_spy[-1]["india"]
        assert india["slippage_denied_by"] == "slippage_reference"
        assert Decimal(india["preflight"]) == 0 and "slippage_reference" not in india
        assert rig.ledger.get_reservation("lim-bad") is None
    finally:
        rig.close()


def test_the_cap_edge_is_inclusive_for_the_limit_input(tmp_path):
    rig = make_rig(tmp_path, simulator=FixedFill("100.00"))
    try:
        exactly = prepare_buy(rig, "edge-in", limit="100.25")  # 25 bps above the ask
        beyond = prepare_buy(rig, "edge-out", limit="100.26")
        assert exactly.decision is AdmissionDecision.ADMITTED
        assert beyond.decision is AdmissionDecision.DENIED and beyond.reason_code == "SLIPPAGE_LIMIT"
    finally:
        rig.close()


def _direct_kwargs(rig: Rig, intent: OrderIntent, **overrides: Any) -> dict[str, Any]:
    """Every runtime input ``admit`` needs, supplied by hand so a test can break one."""

    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {
        "currency": "INR",
        "price": intent.limit_price,
        "price_divisor": Decimal(1),
        "price_source": PRICE_SOURCE_RELAY_QUOTE,
        "tick_window": {"bid": [99.9, 99.91, 99.92], "ask": [100.0, 100.01, 100.02], "spread": [0.001] * 3},
        "portfolio_state": rig.adapter.portfolio_state(),
        "risk_db_connection": rig.adapter._policy_connection,
        "india_quote": make_evidence(),
        "evidence_at": now,
        **bound_regime_fields(),
        "current_spread_pct": 0.001,
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not ...}


def test_a_caller_simulator_figure_never_overrides_the_configured_simulator(tmp_path):
    rig = make_rig(tmp_path, simulator=FixedFill("100.30"))
    try:
        intent = buy(rig, "override")
        admission = rig.adapter.service.admit(
            intent,
            simulator_evidence={"simulated_fill_price": "100.00"},  # a good price the caller supplies
            **_direct_kwargs(rig, intent),
        )
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "SLIPPAGE_LIMIT"
        assert admission.simulator_fill_price == Decimal("100.30")
    finally:
        rig.close()


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"regime_id": None}, "GMM_REGIME_ID_IS_REQUIRED"),
        ({"regime_policy_hash": "0" * 64}, "REGIME_POLICY_MISMATCH"),
        ({"price": Decimal("100.01")}, "PRICE_NOT_PINNED_TO_LIMIT"),
        ({"price_source": PRICE_SOURCE_OPERATOR_RECORDED}, "PRICE_SOURCE_NOT_ADMISSIBLE"),
        ({"price_source": "local-replay"}, "PRICE_SOURCE_NOT_ADMISSIBLE"),
        ({"price_divisor": None}, "PRICE_NOT_PINNED_TO_LIMIT"),
        ({"evidence_at": datetime.now(timezone.utc) - timedelta(seconds=45)}, "ADMISSION_EVIDENCE_IS_STALE"),
    ],
)
def test_integration_rows_deny_with_a_typed_code_and_no_reservation(tmp_path, overrides, code):
    rig = make_rig(tmp_path)
    try:
        intent = buy(rig, "row")
        if overrides.get("regime_id", ...) is None:
            overrides = {**overrides, "regime_policy_hash": None, "regime_audit": None}
        admission = rig.adapter.service.admit(intent, **_direct_kwargs(rig, intent, **overrides))
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == code
        assert rig.ledger.get_reservation("row") is None
        assert rig.ledger.simulated_cash_view().reserved == 0
    finally:
        rig.close()


def test_a_wrong_audit_field_is_denied(tmp_path):
    rig = make_rig(tmp_path)
    try:
        intent = buy(rig, "audit")
        fields = bound_regime_fields()
        fields["regime_audit"] = {**fields["regime_audit"], "severity_rank": 3}
        admission = rig.adapter.service.admit(intent, **_direct_kwargs(rig, intent, **fields))
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "REGIME_POLICY_MISMATCH"
    finally:
        rig.close()


def test_a_mismatched_policy_table_is_denied(tmp_path):
    from regime_testkit import permuted_params
    from simulation.regime_severity import build_scaling_policy_connection, build_severity_map

    rig = make_rig(tmp_path)
    foreign = build_scaling_policy_connection(build_severity_map(permuted_params((1, 0, 2, 3))))
    try:
        intent = buy(rig, "table")
        admission = rig.adapter.service.admit(
            intent, **_direct_kwargs(rig, intent, risk_db_connection=foreign)
        )
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "REGIME_POLICY_MISMATCH"
    finally:
        foreign.close()
        rig.close()


def test_a_service_without_a_severity_map_denies_every_admission(tmp_path):
    from simulation import PreFlightSimulator, RiskSwarmGate

    rig = make_rig(tmp_path)
    try:
        bare = ExecutionService(
            None, rig.ledger, simulator=PreFlightSimulator(), risk_gate=RiskSwarmGate(),
            require_runtime_preflight=True, india_guard=rig.guard,
        )
        intent = buy(rig, "nomap")
        admission = bare.admit(intent, **_direct_kwargs(rig, intent))
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "REGIME_SEVERITY_MAP_UNAVAILABLE"
    finally:
        rig.close()


def test_two_quotes_only_deny_as_a_short_window(tmp_path):
    rig = make_rig(tmp_path)
    try:
        admission = prepare_buy(rig, "two", count=2)
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "REGIME_WINDOW_INSUFFICIENT"
        assert rig.ledger.get_reservation("two") is None
    finally:
        rig.close()


def test_stale_samples_and_repeated_readings_deny(tmp_path):
    rig = make_rig(tmp_path)
    try:
        stale = prepare_buy(rig, "stale", age=30)  # newest reading 30 s old, oldest 90 s
        assert stale.decision is AdmissionDecision.DENIED and stale.reason_code == "STALE_SNAPSHOT"
        now = datetime.now(timezone.utc)
        one = samples(now)[0]
        repeated = run(rig.adapter.prepare(buy(rig, "same"), [one, one, one]))
        assert repeated.decision is AdmissionDecision.DENIED
        assert repeated.reason_code == "QUOTE_READINGS_NOT_DISTINCT"
    finally:
        rig.close()


def test_the_adapter_always_pins_the_price_to_the_limit(tmp_path, evidence_spy):
    rig = make_rig(tmp_path, simulator=FixedFill("100.00"))
    try:
        admission = prepare_buy(rig, "pin", limit="100.20")
        assert admission.decision is AdmissionDecision.ADMITTED
        assert admission.price == Decimal("100.20")  # the limit, not the mid and not the fill
    finally:
        rig.close()


# --- the price source is scoped to the binding ------------------------------------------


def test_the_relay_source_is_admissible_only_on_the_shadow_binding(tmp_path):
    assert PRICE_SOURCE_RELAY_QUOTE not in ADMISSIBLE_PRICE_SOURCES
    assert PRICE_SOURCE_RELAY_QUOTE not in admissible_price_sources()
    practice = VenueBinding(venue=VENUE_T212_PRACTICE, account_id="demo-1", currency="GBP")
    assert admissible_price_sources(False, practice) == frozenset({PRICE_SOURCE_OPERATOR_RECORDED})
    ledger = ExecutionLedger(tmp_path / "uk.sqlite3", workspace="uk", require_approval=True, venue=practice)
    try:
        service = ExecutionService(None, ledger)
        proposal = {
            "proposal_id": "t212-relay", "workspace": "uk", "account": "demo-1", "broker": VENUE_T212_PRACTICE,
            "mode": "PRACTICE", "ticker": "VODl_EQ", "action": "BUY", "quantity": "1",
            "order_type": "LIMIT", "limit_price": "50",
        }
        denied = service.admit(proposal, currency="GBP", price="0.5", price_divisor="100",
                               price_source=PRICE_SOURCE_RELAY_QUOTE)
        assert denied.decision is AdmissionDecision.DENIED
        assert denied.reason_code == "PRICE_SOURCE_NOT_ADMISSIBLE"
        assert ledger.get_reservation("t212-relay") is None
    finally:
        ledger.close()


@pytest.mark.parametrize("mode", ["PAPER", "PRACTICE", "LIVE"])
def test_the_shadow_ledger_refuses_every_other_mode(tmp_path, mode):
    rig = make_rig(tmp_path)
    try:
        intent = OrderIntent(
            proposal_id=f"mode-{mode}", workspace="india", account=f"sim-{RUN_ID}", broker="breeze_relay",
            mode=mode, ticker=f"NSE:CASH:{SYMBOL}", side="BUY", quantity=Decimal(1),
            order_type="LIMIT", limit_price=Decimal("100.00"),
        )
        with pytest.raises(ApprovalConflict):
            rig.ledger.register_intent(intent)
        assert rig.ledger.get_order(f"mode-{mode}") is None
    finally:
        rig.close()


# --- unchanged behaviour: unbound India paper and UK paper ---------------------------------
#
# ``_GOLDEN`` was produced by running ``_golden_records`` on origin/main (9dd549cd) before any
# 64-07 change. The test reruns it on this branch and compares every recorded field and the whole
# admission evidence dict (minus the clock), so a new key or a changed figure on either path fails.

def _golden_records(service_module, tmp: Path):
    from execution import ExecutionLedger, ExecutionService, PaperDispatcher
    from india_limits_support import (admit, make_guard, make_service, india_private_dir, open_ledger,
                                      seed_position, make_intent, make_evidence, TICKER)
    from regime_testkit import bound_admit, gated
    out = {}
    seen = []
    original = service_module.canonical_json
    def spy(value):
        if isinstance(value, dict) and "simulator" in value and "risk" in value:
            copy = json.loads(original(value))
            copy.pop("evidence_at", None)
            seen.append(copy)
        return original(value)
    service_module.canonical_json = spy
    try:
        def record(name, admission):
            out[name] = {
                "decision": admission.decision.value, "reason": admission.reason_code,
                "final_quantity": str(admission.final_quantity), "price": str(admission.price),
                "notional": str(admission.notional), "fill": str(admission.simulator_fill_price),
                "spread": str(admission.current_spread_pct), "evidence": seen[-1],
            }
        private = india_private_dir(tmp)
        ledger = open_ledger(tmp, name="gold-india.sqlite3")
        try:
            guard = make_guard(ledger, private)
            service = make_service(ledger, guard)
            record("india_paper_buy", admit(service, make_intent("g-buy", quantity=2)))
            record("india_paper_buy_slippage", admit(service, make_intent("g-slip", quantity=1), fill="100.30"))
            seed_position(ledger, TICKER, 5, "500.00", guard=guard)
            record("india_paper_sell", admit(service, make_intent("g-sell", side="SELL", quantity=2)))
        finally:
            ledger.close()
        uk = ExecutionLedger(tmp / "gold-uk.sqlite3", workspace="uk", require_approval=True)
        try:
            uk.configure_paper_budget("paper", "GBP", "1000", workspace="uk")
            uk_service = ExecutionService(PaperDispatcher(), uk, **gated())
            admission = uk_service.admit(
                {"proposal_id": "g-uk", "workspace": "uk", "account": "paper", "broker": "paper",
                 "mode": "PAPER", "ticker": "VOD.L", "action": "BUY", "quantity": "3"},
                currency="GBP", price="10",
                simulator_evidence={"simulated_fill_price": "10"}, risk_evidence={"scaled_size": "3"},
                **bound_admit(),
            )
            record("uk_paper_buy", admission)
        finally:
            uk.close()
    finally:
        service_module.canonical_json = original
    return out



_GOLDEN = json.loads(r"""{
 "india_paper_buy": {
  "decision": "ADMITTED",
  "evidence": {
   "current_spread_pct": "0.002",
   "india": {
    "codes": [],
    "guard": true,
    "latches": [],
    "limits_sha256": "01f406d81fbab1427055ab282cc04b0901af0828313cdaf08a43c51a5935363d",
    "slippage_bps": "0"
   },
   "max_age_seconds": 30,
   "regime": {
    "component_count": 4,
    "mapping_version": "gmm-severity-score-v1",
    "model_version": "gmm-p256:154172baa4d634f6dd380bc18139317151f63b5331f81cf6f96bcd1c5903e823",
    "params_sha256": "9312e46ee61de33dc05f630a2ab4ff42468b07104e146d963e343d45f051b746",
    "policy_hash": "0c82bd333655c222a1edeccfa1bd1bd33a57abb20e4bc75f014d079c737b90d8",
    "policy_version": "gmm-severity-policy-v1",
    "rank_by_id": "[1, 3, 2, 0]",
    "regime_id": 3,
    "scores": "['0.34337449218959853', '17.14801433993116', '5.913311072548657', '-0.5672188849734687']",
    "severity_label": "calm",
    "severity_rank": 0,
    "size_multiplier": "1.0"
   },
   "risk": {
    "scaled_size": "2.0"
   },
   "simulator": {
    "simulated_fill_price": "100.00"
   }
  },
  "fill": "100.00",
  "final_quantity": "2.0",
  "notional": "200.000",
  "price": "100.00",
  "reason": "ADMITTED",
  "spread": "0.002"
 },
 "india_paper_buy_slippage": {
  "decision": "DENIED",
  "evidence": {
   "current_spread_pct": "0.002",
   "india": {
    "codes": [
     "SLIPPAGE_LIMIT"
    ],
    "guard": true,
    "latches": [],
    "limits_sha256": "01f406d81fbab1427055ab282cc04b0901af0828313cdaf08a43c51a5935363d"
   },
   "max_age_seconds": 30,
   "regime": {
    "component_count": 4,
    "mapping_version": "gmm-severity-score-v1",
    "model_version": "gmm-p256:154172baa4d634f6dd380bc18139317151f63b5331f81cf6f96bcd1c5903e823",
    "params_sha256": "9312e46ee61de33dc05f630a2ab4ff42468b07104e146d963e343d45f051b746",
    "policy_hash": "0c82bd333655c222a1edeccfa1bd1bd33a57abb20e4bc75f014d079c737b90d8",
    "policy_version": "gmm-severity-policy-v1",
    "rank_by_id": "[1, 3, 2, 0]",
    "regime_id": 3,
    "scores": "['0.34337449218959853', '17.14801433993116', '5.913311072548657', '-0.5672188849734687']",
    "severity_label": "calm",
    "severity_rank": 0,
    "size_multiplier": "1.0"
   },
   "risk": {
    "scaled_size": "1.0"
   },
   "simulator": {
    "simulated_fill_price": "100.30"
   }
  },
  "fill": "100.30",
  "final_quantity": "0",
  "notional": "0.00",
  "price": "100.00",
  "reason": "SLIPPAGE_LIMIT",
  "spread": "0.002"
 },
 "india_paper_sell": {
  "decision": "ADMITTED",
  "evidence": {
   "current_spread_pct": "0.002",
   "india": {
    "codes": [],
    "guard": true,
    "latches": [],
    "limits_sha256": "01f406d81fbab1427055ab282cc04b0901af0828313cdaf08a43c51a5935363d",
    "slippage_bps": "-10.01001001001001001001001001"
   },
   "max_age_seconds": 30,
   "regime": {
    "component_count": 4,
    "mapping_version": "gmm-severity-score-v1",
    "model_version": "gmm-p256:154172baa4d634f6dd380bc18139317151f63b5331f81cf6f96bcd1c5903e823",
    "params_sha256": "9312e46ee61de33dc05f630a2ab4ff42468b07104e146d963e343d45f051b746",
    "policy_hash": "0c82bd333655c222a1edeccfa1bd1bd33a57abb20e4bc75f014d079c737b90d8",
    "policy_version": "gmm-severity-policy-v1",
    "rank_by_id": "[1, 3, 2, 0]",
    "regime_id": 3,
    "scores": "['0.34337449218959853', '17.14801433993116', '5.913311072548657', '-0.5672188849734687']",
    "severity_label": "calm",
    "severity_rank": 0,
    "size_multiplier": "1.0"
   },
   "risk": {
    "scaled_size": "2.0"
   },
   "simulator": {
    "simulated_fill_price": "100.00"
   }
  },
  "fill": "100.00",
  "final_quantity": "2.0",
  "notional": "200.000",
  "price": "100.00",
  "reason": "ADMITTED",
  "spread": "0.002"
 },
 "uk_paper_buy": {
  "decision": "ADMITTED",
  "evidence": {
   "current_spread_pct": "0.002",
   "max_age_seconds": 30,
   "regime": {
    "component_count": 4,
    "mapping_version": "gmm-severity-score-v1",
    "model_version": "gmm-p256:154172baa4d634f6dd380bc18139317151f63b5331f81cf6f96bcd1c5903e823",
    "params_sha256": "9312e46ee61de33dc05f630a2ab4ff42468b07104e146d963e343d45f051b746",
    "policy_hash": "0c82bd333655c222a1edeccfa1bd1bd33a57abb20e4bc75f014d079c737b90d8",
    "policy_version": "gmm-severity-policy-v1",
    "rank_by_id": "[1, 3, 2, 0]",
    "regime_id": 3,
    "scores": "['0.34337449218959853', '17.14801433993116', '5.913311072548657', '-0.5672188849734687']",
    "severity_label": "calm",
    "severity_rank": 0,
    "size_multiplier": "1.0"
   },
   "risk": {
    "scaled_size": "3.0"
   },
   "simulator": {
    "simulated_fill_price": "10"
   }
  },
  "fill": "10",
  "final_quantity": "3.0",
  "notional": "30.0",
  "price": "10",
  "reason": "ADMITTED",
  "spread": "0.002"
 }
}
""")


def test_unbound_india_paper_and_uk_paper_admissions_are_unchanged(tmp_path):
    assert _golden_records(service_module, tmp_path) == _GOLDEN
    # No bound-venue slippage keys leak onto the unbound India path.
    for name in ("india_paper_buy", "india_paper_buy_slippage"):
        india = _GOLDEN[name]["evidence"]["india"]
        assert not {"preflight", "slippage_reference", "slippage_denied_by"} & set(india)

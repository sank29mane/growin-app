"""Mac and VM halt reset and halt anchor, driven through the same scripts (Phase 63-02 r1).

The Mac module (``risk_india.drawdown``) and the VM module (``gateway_vm.orders.risk``) are
separate implementations. Each script below is replayed through both, and after every step
the latches, the exact drawdown, the true peak, the halt anchor and the last equity must
agree, as must whether an admin reset was refused. The shared vectors carry no anchor rows,
so these scripts are the cross-check for the reset and anchor rules.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from gateway_vm.orders import limits as vm_limits  # noqa: E402
from gateway_vm.orders import risk as vm_risk  # noqa: E402
from gateway_vm.orders.limits import Trade  # noqa: E402

from risk_india import drawdown, exits, rules  # noqa: E402

LV = json.loads(
    (ROOT / "tests/backend/fixtures/relay_orders/limits_vectors.json").read_text(encoding="utf-8")
)
MAC_LIMITS = rules.Limits.from_fields(LV["limits"])
VM_LIMITS = vm_limits.Limits.from_fields(LV["limits"])
ISIN = "INE000A01012"
START = date(2026, 10, 9)

# ("close", price) evaluates the next session at that close of a 100-share position
# costing 500 each (peak 50,000). ("reset", rebase) is an admin halt reset.
SCRIPTS = {
    "refused_then_rebased_then_still_minus_8": [
        ("close", "460"), ("reset", False), ("reset", True), ("close", "460"), ("close", "450"),
    ],
    "minus_15_ignores_the_anchor": [
        ("close", "460"), ("reset", True), ("close", "425.01"), ("close", "425"),
    ],
    "anchor_follows_equity_up_then_halts_8_below_it": [
        ("close", "460"), ("reset", True), ("close", "480"), ("close", "441.61"), ("close", "441.60"),
    ],
    "anchor_dropped_at_a_new_peak": [
        ("close", "460"), ("reset", True), ("close", "505"), ("close", "464.6"),
    ],
    "ordinary_reset_after_recovery_needs_no_flag": [
        ("close", "460"), ("close", "520"), ("reset", False), ("close", "520"),
    ],
    "second_halt_after_a_rebase_needs_the_flag_again": [
        ("close", "460"), ("reset", True), ("close", "480"), ("close", "441.60"),
        ("reset", False), ("reset", True), ("close", "441.60"),
    ],
    "reset_while_ended_is_refused_without_the_flag": [("close", "400"), ("reset", False)],
}


class _Mac:
    def __init__(self) -> None:
        self.state = drawdown.initial_state(MAC_LIMITS)
        self.day = 0

    def close(self, price: str) -> None:
        self.day += 1
        self.state = drawdown.evaluate_session(
            self.state, MAC_LIMITS, START + timedelta(days=self.day), cash=Decimal(0),
            positions=[exits.Position(ISIN, "TESTCO", 100, Decimal("50000"))],
            closes={ISIN: Decimal(price)},
        ).state

    def reset(self, rebase: bool) -> bool:
        try:
            self.state = drawdown.reset(
                self.state, "halt", "op", limits=MAC_LIMITS, rebase_halt_anchor=rebase
            )
        except drawdown.ResetRefused:
            return False
        return True

    def view(self) -> dict:
        s = self.state
        return {"halt": s.halt, "ended": s.ended, "drawdown": s.drawdown, "peak": s.peak,
                "anchor": s.halt_anchor, "equity": s.last_equity}


class _Vm:
    def __init__(self) -> None:
        self.state = vm_risk.initial_state(VM_LIMITS)
        vm_risk.apply_trades(
            self.state, [Trade("b1", ISIN, "buy", 100, Decimal("500"), Decimal("0"))]
        )
        self.day = 0

    def close(self, price: str) -> None:
        self.day += 1
        vm_risk.evaluate_session(
            self.state, VM_LIMITS, START + timedelta(days=self.day), {ISIN: Decimal(price)}
        )

    def reset(self, rebase: bool) -> bool:
        try:
            vm_risk.reset_latch(self.state, "halt", limits=VM_LIMITS, rebase_halt_anchor=rebase)
        except vm_risk.ResetRefused:
            return False
        return True

    def view(self) -> dict:
        s = self.state
        return {"halt": s.halt, "ended": s.ended, "drawdown": s.drawdown, "peak": s.peak,
                "anchor": s.halt_anchor, "equity": s.last_equity}


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_mac_and_vm_agree_after_every_step_of_the_script(name):
    mac, vm = _Mac(), _Vm()
    for index, (kind, arg) in enumerate(SCRIPTS[name]):
        if kind == "close":
            mac.close(arg)
            vm.close(arg)
        else:
            assert mac.reset(arg) is vm.reset(arg), (name, index, "reset outcome")
        assert mac.view() == vm.view(), (name, index, kind, arg)


def test_the_scripts_reach_a_refusal_a_rebase_a_drop_and_an_anchored_halt():
    """Guard against a vacuous parity: each rule is actually exercised on both sides."""
    seen = {"refused": 0, "rebased": 0, "dropped": 0, "anchored_halt": 0}
    for script in SCRIPTS.values():
        mac = _Mac()
        for kind, arg in script:
            before = mac.state
            if kind == "close":
                mac.close(arg)
                if before.halt_anchor is not None and mac.state.halt_anchor is None:
                    seen["dropped"] += 1
                if before.halt_anchor is not None and mac.state.halt and not before.halt:
                    seen["anchored_halt"] += 1
            elif mac.reset(arg):
                seen["rebased"] += mac.state.halt_anchor is not None
            else:
                seen["refused"] += 1
    assert all(count >= 1 for count in seen.values()), seen

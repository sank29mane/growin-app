from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from execution import ExecutionLedger, ExecutionService, PaperDispatcher
from execution.models import OrderIntent


def proposal(pid, quantity="5"):
    return {
        "proposal_id": pid,
        "workspace": "uk",
        "account": "invest",
        "broker": "paper",
        "mode": "PAPER",
        "ticker": "VUSA",
        "action": "BUY",
        "quantity": quantity,
    }


def prepare(service, ledger, pid, quantity="5"):
    p = proposal(pid, quantity)
    service.admit(
        p,
        currency="GBP",
        price="10",
        simulator_evidence={"simulated_fill_price": "10"},
        risk_evidence={"scaled_size": quantity},
    )
    return p


def test_explicit_budget_is_required_and_reservation_is_decimal(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        service = ExecutionService(PaperDispatcher(), ledger)
        prepare(service, ledger, "one")
        with pytest.raises(Exception, match="budget"):
            service.reserve("one")
        ledger.configure_paper_budget("invest", "GBP", "100")
        reservation = service.reserve("one")
        assert reservation.reserved == Decimal("50")
        assert ledger.get_paper_budget("invest", "GBP").available == Decimal("50")


def test_concurrent_buy_reservations_cannot_overallocate_budget(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        service = ExecutionService(PaperDispatcher(), ledger)
        ledger.configure_paper_budget("invest", "GBP", "100")
        for pid in ("one", "two", "three"):
            prepare(service, ledger, pid)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda pid: _reserve(service, pid), ("one", "two", "three")))
        assert sum(result is not None for result in results) == 2
        assert ledger.get_paper_budget("invest", "GBP").available == Decimal("0")


def _reserve(service, pid):
    try:
        return service.reserve(pid)
    except Exception:
        return None


def test_cross_workspace_and_sell_reservations_fail_closed(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3", workspace="uk") as ledger:
        service = ExecutionService(PaperDispatcher(), ledger)
        ledger.configure_paper_budget("invest", "GBP", "100")
        with pytest.raises(Exception, match="workspace"):
            ledger.register_intent(
                OrderIntent(
                    proposal_id="india",
                    workspace="india",
                    account="invest",
                    broker="paper",
                    mode="PAPER",
                    ticker="VUSA",
                    side="BUY",
                    quantity=Decimal("1"),
                )
            )
        sell = proposal("sell") | {"action": "SELL"}
        service.admit(
            sell,
            currency="GBP",
            price="10",
            simulator_evidence={"simulated_fill_price": "10"},
            risk_evidence={"scaled_size": "5"},
        )
        with pytest.raises(Exception, match="SELL|admission"):
            service.reserve("sell")

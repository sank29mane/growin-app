"""67-FU2: the deterministic wash sale floor in ``RiskAgent``.

The system computes ``wash_sale_alert`` from the user's trade history. When it is
True and the proposal is a BUY or LONG entry the critic's status is raised to
BLOCKED, whatever the model said. The floor only tightens: it never lowers a
status, and an already BLOCKED verdict is returned untouched.
"""

import json

import pytest

from agents.risk_agent import (
    WASH_SALE_FLOOR_NOTE,
    RiskAgent,
    RiskAssessment,
    apply_wash_sale_floor,
    is_long_entry,
)
from market_context import MarketContext, PortfolioData, PriceData
from model_registry import ProviderError
from model_registry_testkit import (  # noqa: F401  (fixtures)
    key_env,
    network_guard,
    registry_factory,
    risk_workspace,
    stub,
)

RANK = {"APPROVED": 0, "FLAGGED": 1, "BLOCKED": 2}


def _assessment(status, notes="model notes"):
    return RiskAssessment(
        status=status,
        confidence_score=0.7,
        risk_assessment="summary",
        compliance_notes=notes,
        recommendation_adjustment="none",
        debate_refutation="argument",
        requires_hitl=False,
    )


def _verdict(status, notes="model notes"):
    return json.loads(_assessment(status, notes).model_dump_json())


def _context(loss_sale=True):
    context = MarketContext(
        query="q",
        ticker="AAPL",
        intent="analytical",
        price=PriceData(ticker="AAPL", current_price=150.0, validated=True),
        portfolio=PortfolioData(total_value=100000.0, cash_balance={"total": 5000.0}),
    )
    if loss_sale:
        context.user_context["recent_trades"] = [
            {"ticker": "AAPL", "side": "SELL", "pnl": -50.0, "timestamp": "2026-01-01"}
        ]
    return context


# --- the pure floor ----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["APPROVED", "FLAGGED"])
def test_the_floor_raises_a_weaker_status_to_blocked_and_says_why(status):
    result = apply_wash_sale_floor(_assessment(status), wash_sale_alert=True, long_entry=True)
    assert result.status == "BLOCKED"
    assert result.compliance_notes == "model notes\n" + WASH_SALE_FLOOR_NOTE
    # Nothing else about the model's answer changes.
    assert result.model_dump(exclude={"status", "compliance_notes"}) == _assessment(status).model_dump(
        exclude={"status", "compliance_notes"}
    )


def test_an_already_blocked_verdict_is_returned_untouched():
    original = _assessment("BLOCKED")
    result = apply_wash_sale_floor(original, wash_sale_alert=True, long_entry=True)
    assert result == original and result.compliance_notes == "model notes"


@pytest.mark.parametrize("status", sorted(RANK))
@pytest.mark.parametrize("alert", [True, False])
@pytest.mark.parametrize("long_entry", [True, False])
def test_the_floor_never_relaxes_any_status(status, alert, long_entry):
    original = _assessment(status)
    result = apply_wash_sale_floor(original, alert, long_entry)
    assert RANK[result.status] >= RANK[original.status]
    if not (alert and long_entry):
        assert result == original  # not triggered: not touched
    else:
        assert result.status == "BLOCKED"


def test_the_floor_does_not_mutate_its_input():
    original = _assessment("APPROVED")
    apply_wash_sale_floor(original, True, True)
    assert original.status == "APPROVED" and original.compliance_notes == "model notes"


@pytest.mark.parametrize(
    "text,expected",
    [("BUY 10 AAPL", True), ("buy 10 aapl", True), ("LONG AMD", True), ("go long", True),
     ("SELL 10 AAPL", False), ("Explain RSI", False), ("", False)],
)
def test_is_long_entry(text, expected):
    assert is_long_entry(text) is expected


# --- through RiskAgent -------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_network(network_guard):
    return network_guard


@pytest.mark.asyncio
@pytest.mark.parametrize("model_status", ["APPROVED", "FLAGGED"])
async def test_a_wash_sale_buy_is_blocked_even_when_the_model_does_not_block(
    stub, registry_factory, risk_workspace, model_status
):
    registry_factory()
    stub.structured["RiskAssessment"] = _verdict(model_status)
    review = await RiskAgent().review(_context(), "BUY 10 shares of AAPL")
    assert review["status"] == "BLOCKED"
    assert review["model_status"] == model_status and review["floored"] is True
    assert WASH_SALE_FLOOR_NOTE in review["compliance_notes"]
    assert "model notes" in review["compliance_notes"]
    assert review["requires_hitl"] is True  # Phase 63: BLOCKED still reaches HITL registration
    assert "Wash Sale Risk Alert: True" in json.dumps(stub.snapshot()[0].json["messages"])


@pytest.mark.asyncio
async def test_a_model_block_is_left_exactly_as_the_model_wrote_it(stub, registry_factory, risk_workspace):
    registry_factory()
    stub.structured["RiskAssessment"] = _verdict("BLOCKED", "the model's own wash sale note")
    review = await RiskAgent().review(_context(), "BUY 10 shares of AAPL")
    assert review["status"] == "BLOCKED"
    assert review["compliance_notes"] == "the model's own wash sale note"
    assert review["model_status"] == "BLOCKED" and review["floored"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("model_status", sorted(RANK))
async def test_without_the_system_alert_the_model_status_stands(stub, registry_factory, risk_workspace, model_status):
    registry_factory()
    stub.structured["RiskAssessment"] = _verdict(model_status)
    review = await RiskAgent().review(_context(loss_sale=False), "BUY 10 shares of AAPL")
    assert review["status"] == model_status
    assert review["model_status"] == model_status and review["floored"] is False
    assert WASH_SALE_FLOOR_NOTE not in review["compliance_notes"]


@pytest.mark.asyncio
async def test_a_loss_sale_on_a_sell_proposal_does_not_trigger_the_floor(stub, registry_factory, risk_workspace):
    registry_factory()
    stub.structured["RiskAssessment"] = _verdict("APPROVED")
    review = await RiskAgent().review(_context(), "SELL 10 shares of AAPL")
    assert review["status"] == "APPROVED"


@pytest.mark.asyncio
async def test_a_profitable_prior_sale_does_not_trigger_the_floor(stub, registry_factory, risk_workspace):
    registry_factory()
    stub.structured["RiskAssessment"] = _verdict("APPROVED")
    context = _context(loss_sale=False)
    context.user_context["recent_trades"] = [{"ticker": "AAPL", "side": "SELL", "pnl": 50.0}]
    assert (await RiskAgent().review(context, "BUY 10 shares of AAPL"))["status"] == "APPROVED"


@pytest.mark.asyncio
async def test_the_floor_never_rescues_a_failed_critic_call(stub, registry_factory, risk_workspace):
    """No verdict, no floor: a critic that fails closed stays CRITIC_OUTPUT_INVALID, never a manufactured BLOCKED."""
    registry_factory()
    stub.structured["RiskAssessment"] = _verdict("BLOCKED")
    stub.reasoning_tokens = 0
    with pytest.raises(ProviderError) as caught:
        await RiskAgent().review(_context(), "BUY 10 shares of AAPL")
    assert caught.value.code == "CRITIC_OUTPUT_INVALID"

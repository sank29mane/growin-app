"""Where slippage and liquidity figures go today. This is NOT a slippage gate.

No deterministic slippage gate exists in ``backend/`` yet: nothing blocks or
flags a trade because ``slippage_bps`` is high or ``liquidity_status`` is
ILLIQUID, and ``RiskAgent.review`` never reads ``risk_governance``. A deterministic
gate is tracked for Phase 63 and is not added here.

What the tests below pin is the real data path, so a later gate has an honest
baseline:

* the figures reach the DECISION prompt (``DecisionAgent._build_prompt``);
* the critic (the registry's ``risk_critic`` role) sees only the text it is
  given. The decision's recommendation reaches it verbatim, so a figure the
  decision writes down reaches the critic; the structured ``risk_governance``
  fields are not passed to it separately (a known gap, characterised below).

The filename is kept so the test is not moved; its old body asserted nothing.
"""

import json
import os
import sys
from decimal import Decimal

import pytest

pytestmark = pytest.mark.asyncio

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from market_context import MarketContext, RiskGovernanceData, PriceData
from agents.decision_agent import DecisionAgent
from agents.risk_agent import RiskAgent
import model_registry_testkit as kit
from model_registry_testkit import key_env, network_guard, offline_registry, registry_factory, stub  # noqa: F401


def _illiquid_context() -> MarketContext:
    return MarketContext(
        query="Buy 1,000,000 shares of ILLIQ",
        ticker="ILLIQ",
        price=PriceData(ticker="ILLIQ", current_price=Decimal("1.0")),
        risk_governance=RiskGovernanceData(
            slippage_bps=Decimal("150.0"),
            liquidity_status="ILLIQUID",
        ),
    )


async def test_slippage_and_liquidity_reach_the_decision_prompt(offline_registry):
    from unittest.mock import MagicMock

    prompt = DecisionAgent(mcp_client=MagicMock())._build_prompt(_illiquid_context(), "Buy ILLIQ")
    assert "Est. Slippage: 150.0 bps" in prompt
    assert "ILLIQUID" in prompt


async def test_figures_in_the_decision_text_reach_the_critic_prompt(stub, registry_factory):
    registry_factory()
    recommendation = "BUY 1,000,000 ILLIQ. LIQUIDITY: Status: ILLIQUID | Est. Slippage: 150.0 bps"
    review = await RiskAgent().review(_illiquid_context(), recommendation)

    [request] = stub.snapshot()
    assert request.model == kit.model_id_for("risk_critic")
    prompt = json.dumps(request.json["messages"])
    assert "Est. Slippage: 150.0 bps" in prompt
    assert "ILLIQUID" in prompt
    # The verdict is whatever the critic returned. Nothing here gates on slippage.
    assert review["status"] == "APPROVED"


async def test_known_gap_critic_does_not_receive_risk_governance_fields(stub, registry_factory):
    """Characterisation, not a requirement: Phase 63 may change this on purpose."""
    registry_factory()
    await RiskAgent().review(_illiquid_context(), "BUY 1,000,000 ILLIQ")

    [request] = stub.snapshot()
    prompt = json.dumps(request.json["messages"])
    assert "150" not in prompt
    assert "ILLIQUID" not in prompt


async def test_no_deterministic_slippage_gate_exists_in_the_risk_agent():
    import inspect

    import agents.risk_agent as risk_agent

    source = inspect.getsource(risk_agent)
    assert "slippage" not in source.lower()
    assert "risk_governance" not in source

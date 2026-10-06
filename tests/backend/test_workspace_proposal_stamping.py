"""Proposals are stamped from the open ledger or refused; chat survives a refusal (decision 2, ISO-01).

D3: the audit marker "unscoped" is telemetry only. It is not a workspace, and a
proposal or approval carrying it is refused.
"""

import logging
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import app_context
from agents.decision_agent import DecisionAgent
from app_context import AppState, state
from execution import (
    ExecutionDisabledError,
    ExecutionLedger,
    Workspace,
    WorkspaceMismatch,
    coerce_workspace,
)
from market_context import MarketContext
from server import app
from utils.audit_log import AUDIT_UNSCOPED, AUDIT_WORKSPACES


def _full_proposal(proposal_id="p-stamp-1", **overrides):
    proposal = {
        "proposal_id": proposal_id,
        "account": "invest",
        "broker": "paper",
        "mode": "PAPER",
        "ticker": "VUSA",
        "action": "BUY",
        "quantity": "1",
        "status": "PENDING",
    }
    proposal.update(overrides)
    return proposal


def _intent_rows(path):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return connection.execute("SELECT proposal_id FROM order_intents").fetchall()
    finally:
        connection.close()


@pytest.fixture
def uk_state(tmp_path, private_config_dir):
    app_state = AppState()
    assert app_state.start_execution(
        tmp_path / "execution.sqlite3", workspace="uk", private_dir=private_config_dir
    )
    try:
        yield app_state
    finally:
        app_state.close_execution()


def _agent():
    return DecisionAgent(mcp_client=MagicMock())


def _chat_proposal(agent, ticker="AAPL"):
    context = MarketContext(query="Buy it", intent="analytical", ticker=ticker)
    proposal = agent._extract_trade_proposal("BUY 1 share of AAPL.", context)
    assert proposal is not None
    return proposal, context


async def _review_proposal(context, proposal):
    from agents.base_agent import AgentResponse
    from agents.risk_agent import RiskAgent, RiskAssessment

    context.user_context["deferred_proposal"] = proposal
    critic = RiskAgent()
    critic.execute = AsyncMock(return_value=AgentResponse(
        agent_name="RiskAgent", success=True, latency_ms=0,
        data=RiskAssessment(
            status="APPROVED", confidence_score=1,
            risk_assessment="Fixture review", compliance_notes="Fixture review",
            recommendation_adjustment="None", debate_refutation="None",
        ).model_dump(),
    ))
    await critic.review(context, "BUY 1 share of AAPL")


# --- AppState.register_trade_proposal ---


def test_no_open_ledger_rejects_the_proposal_and_registers_nothing():
    app_state = AppState()

    with pytest.raises(ExecutionDisabledError):
        app_state.register_trade_proposal(_full_proposal(workspace="uk"))

    assert app_state.trade_proposals == {}


def test_a_proposal_without_workspace_is_stamped_from_the_open_ledger(uk_state):
    proposal = _full_proposal()
    assert "workspace" not in proposal

    uk_state.register_trade_proposal(proposal)

    assert proposal["workspace"] == "uk"
    assert uk_state.trade_proposals["p-stamp-1"] is proposal
    assert uk_state._execution_ledger.get_order("p-stamp-1") is not None


def test_a_proposal_naming_another_workspace_is_refused_and_not_persisted(uk_state):
    with pytest.raises(WorkspaceMismatch):
        uk_state.register_trade_proposal(_full_proposal(workspace="india"))

    assert uk_state.trade_proposals == {}
    assert _intent_rows(uk_state._execution_ledger.path) == []


def test_proposal_carrying_unscoped_is_refused(uk_state):
    with pytest.raises(WorkspaceMismatch):
        uk_state.register_trade_proposal(_full_proposal(workspace=AUDIT_UNSCOPED))

    assert uk_state.trade_proposals == {}
    assert uk_state._execution_ledger.get_order("p-stamp-1") is None
    assert _intent_rows(uk_state._execution_ledger.path) == []


# --- the decision agent ---


@pytest.mark.asyncio
async def test_agent_registration_without_a_ledger_returns_false_and_logs_the_code(
    monkeypatch, caplog
):
    monkeypatch.setattr(app_context, "state", AppState())
    agent = _agent()
    proposal, context = _chat_proposal(agent)

    await _review_proposal(context, proposal)
    with caplog.at_level(logging.WARNING):
        registered = agent._register_for_human_review(proposal, context)

    assert registered is False
    assert "TRADE_PROPOSAL_NOT_REGISTERED" in caplog.text
    assert "ExecutionDisabledError" in caplog.text
    assert "pending_proposal" not in context.user_context
    assert app_context.state.trade_proposals == {}


@pytest.mark.asyncio
async def test_agent_registration_on_a_uk_ledger_is_refused_for_missing_account_and_broker(
    monkeypatch, uk_state, caplog
):
    monkeypatch.setattr(app_context, "state", uk_state)
    agent = _agent()
    proposal, context = _chat_proposal(agent)

    await _review_proposal(context, proposal)
    with caplog.at_level(logging.WARNING):
        registered = agent._register_for_human_review(proposal, context)

    assert registered is False
    assert "TRADE_PROPOSAL_NOT_REGISTERED" in caplog.text
    assert proposal["ticker"] not in caplog.text
    assert _intent_rows(uk_state._execution_ledger.path) == []
    assert uk_state.trade_proposals == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{}, {"defer_proposal": False}])
async def test_chat_reply_survives_a_refused_registration(monkeypatch, caplog, kwargs):
    monkeypatch.setattr(app_context, "state", AppState())
    audits = []
    monkeypatch.setattr(
        "agents.decision_agent.log_audit",
        lambda action, actor, details, *, workspace: audits.append((action, workspace)),
    )
    agent = _agent()
    agent._initialized = True
    agent._run_agentic_loop = AsyncMock(
        return_value={"content": "BUY 2 shares of AAPL now.", "response_id": "r-1"}
    )
    agent._validate_prices = AsyncMock(side_effect=lambda text, ticker: text)
    context = MarketContext(query="Buy AAPL", intent="analytical", ticker="AAPL")

    with caplog.at_level(logging.WARNING):
        result = await agent.make_decision(context, "Buy AAPL", **kwargs)
        assert "pending_proposal" not in context.user_context
        assert "deferred_proposal" in context.user_context
        await _review_proposal(context, context.user_context["deferred_proposal"])
        result["content"] += agent.register_deferred_proposal(context)

    assert result["content"].startswith("BUY 2 shares of AAPL now.")
    assert "Trade proposal not registered for review (TRADE_PROPOSAL_NOT_REGISTERED)." in (
        result["content"]
    )
    assert "pending_proposal" not in context.user_context
    assert "TRADE_PROPOSAL_NOT_REGISTERED" in caplog.text
    # No ledger is open, so chat audit telemetry carries the explicit marker.
    assert audits == [("DECISION_MADE", AUDIT_UNSCOPED)]


def test_audit_workspace_follows_the_open_ledger(monkeypatch, uk_state):
    agent = _agent()

    monkeypatch.setattr(app_context, "state", AppState())
    assert agent._audit_workspace() == AUDIT_UNSCOPED

    monkeypatch.setattr(app_context, "state", uk_state)
    assert agent._audit_workspace() == "uk"


# --- D3: "unscoped" is audit telemetry only ---


def test_unscoped_is_not_a_workspace_member():
    assert AUDIT_UNSCOPED == "unscoped"
    assert AUDIT_UNSCOPED in AUDIT_WORKSPACES
    assert AUDIT_UNSCOPED not in {member.value for member in Workspace}
    with pytest.raises(ValueError):
        Workspace("unscoped")


def test_workspace_parser_rejects_unscoped():
    with pytest.raises(ValueError):
        coerce_workspace("unscoped")
    with pytest.raises(ValueError):
        coerce_workspace(AUDIT_UNSCOPED)


@pytest.mark.asyncio
async def test_approval_carrying_unscoped_is_refused(monkeypatch, tmp_path):
    ledger = ExecutionLedger(tmp_path / "uk.sqlite3", workspace="uk")
    execution_service = MagicMock()
    execution_service.approve = AsyncMock()
    monkeypatch.setattr(state, "_execution_service", execution_service)
    monkeypatch.setattr(state, "_execution_ledger", ledger)
    monkeypatch.setitem(
        state.trade_proposals, "p-approve", {"proposal_id": "p-approve", "ticker": "VUSA"}
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            approve = await client.post(
                "/api/ai/trade/approve",
                json={
                    "proposal_id": "p-approve",
                    "decision": "APPROVED",
                    "workspace": AUDIT_UNSCOPED,
                },
            )
            challenge = await client.post(
                "/api/ai/trade/approval/challenge",
                json={"proposal_id": "p-approve", "workspace": AUDIT_UNSCOPED},
            )

        assert approve.status_code == 422
        assert challenge.status_code == 422
        assert execution_service.approve.await_count == 0
        with pytest.raises(WorkspaceMismatch):
            ledger.require_workspace("unscoped")
    finally:
        ledger.close()

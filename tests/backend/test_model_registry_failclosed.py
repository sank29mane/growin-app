"""Review round 1: a missing role or unreachable provider fails closed.

No catch-all turns a registry or provider error into recommendation text, a
FLAGGED review, a keyword route or a basic query. Typed errors reach the caller,
the routes map them to 502/503, and no proposal is left behind.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import model_registry_testkit as kit
from app_context import ChatMessage, state
from agents.decision_agent import DecisionAgent
from agents.orchestrator_agent import OrchestratorAgent
from market_context import MarketContext, PortfolioData, PriceData
from model_registry import (
    ModelRegistryError,
    ModelRoleMissing,
    ProviderError,
    load_registry_file,
    set_active_registry,
)
from model_registry_testkit import (  # noqa: F401  (fixtures)
    key_env,
    network_guard,
    registry_factory,
    stub,
)

pytestmark = pytest.mark.asyncio

DEAD_DECISION = kit.dead_provider_urls("xai")  # decision and risk_critic live on "xai"
DEAD_COORDINATOR = kit.dead_provider_urls("lmstudio")  # coordinator and math_codegen
DEAD_RISK = kit.dead_provider_urls("ollama")  # used when risk_critic is moved to "ollama"


@pytest.fixture(autouse=True)
def _no_real_network(network_guard):
    return network_guard


@pytest.fixture(autouse=True)
def _reset_registry():
    set_active_registry(None)
    yield
    set_active_registry(None)


def _activate(tmp_path, stub_server, **kwargs):
    registry = kit.make_registry(tmp_path / "private", stub_server.url, **kwargs)
    kit.activate(registry)
    return registry


def _context(intent="analytical"):
    return MarketContext(
        query="Buy AAPL",
        ticker="AAPL",
        intent=intent,
        price=PriceData(ticker="AAPL", current_price=150.0, validated=True),
        portfolio=PortfolioData(total_value=10000.0, cash_balance={"total": 5000.0}),
    )


# --- temperature is required for decision and risk_critic -----------------------------------


@pytest.mark.parametrize("role", ["decision", "risk_critic"])
async def test_missing_temperature_fails_that_role_closed(tmp_path, stub, role):
    document = kit.registry_document(stub.url)
    del document["roles"][role]["temperature"]
    registry = load_registry_file(kit.write_registry(tmp_path, document))

    with pytest.raises(ModelRegistryError) as caught:
        registry.resolve(role)
    assert caught.value.code == "TEMPERATURE_REQUIRED"
    assert caught.value.field == role
    with pytest.raises(ModelRegistryError):
        registry.require("coordinator", role)
    # Only that role is affected.
    assert registry.resolve("coordinator").model == kit.model_id_for("coordinator")
    assert role in registry.describe_roles()["missing_roles"]
    assert role not in [item["role"] for item in registry.describe_roles()["roles"]]


async def test_zero_temperature_counts_as_set(tmp_path, stub):
    document = kit.registry_document(stub.url)
    document["roles"]["decision"]["temperature"] = 0
    registry = load_registry_file(kit.write_registry(tmp_path, document))
    assert registry.resolve("decision").temperature == 0


async def test_roles_without_the_requirement_may_omit_temperature(tmp_path, stub):
    document = kit.registry_document(stub.url)
    for role in ("coordinator", "research", "math_codegen"):
        document["roles"][role].pop("temperature", None)
    registry = load_registry_file(kit.write_registry(tmp_path, document))
    assert registry.resolve("coordinator").temperature is None


async def test_example_file_sets_temperature_for_decision_and_risk_critic():
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / "backend" / "model_registry" / "models.example.json"
    registry = load_registry_file(example)
    assert registry.resolve("decision").temperature is not None
    assert registry.resolve("risk_critic").temperature is not None


async def test_call_site_sends_nothing_when_temperature_is_missing(tmp_path, stub, key_env):
    document = kit.registry_document(stub.url)
    del document["roles"]["decision"]["temperature"]
    kit.activate(load_registry_file(kit.write_registry(tmp_path, document)))
    with pytest.raises(ModelRegistryError) as caught:
        await DecisionAgent(mcp_client=MagicMock()).generate_response("hi")
    assert caught.value.code == "TEMPERATURE_REQUIRED"
    assert stub.count == 0


async def test_chat_route_is_503_when_decision_has_no_temperature(tmp_path, stub, key_env):
    from routes.chat_routes import chat_message

    document = kit.registry_document(stub.url)
    del document["roles"]["decision"]["temperature"]
    kit.activate(load_registry_file(kit.write_registry(tmp_path, document)))
    with pytest.raises(HTTPException) as caught:
        await chat_message(ChatMessage(message="hi"), accept="application/json")
    assert caught.value.status_code == 503
    assert caught.value.detail == {"code": "TEMPERATURE_REQUIRED", "role": "decision"}
    assert stub.count == 0


# --- 1: decision failures are typed errors, not recommendation text ---------------------------


async def test_make_decision_raises_a_provider_error_instead_of_returning_text(tmp_path, stub, key_env):
    _activate(tmp_path, stub, base_urls=DEAD_DECISION)
    agent = DecisionAgent(mcp_client=MagicMock())
    before = dict(state.trade_proposals)
    with patch.object(agent, "_inject_context_layers", side_effect=lambda p, q: p):
        with pytest.raises(ProviderError) as caught:
            await agent.make_decision(_context(), "Buy AAPL")
    assert caught.value.code == "PROVIDER_UNREACHABLE"
    assert caught.value.role == "decision"
    assert state.trade_proposals == before


async def test_make_decision_raises_a_missing_role_instead_of_returning_text(tmp_path, stub, key_env):
    _activate(tmp_path, stub, roles=["coordinator", "risk_critic"])
    agent = DecisionAgent(mcp_client=MagicMock())
    with pytest.raises(ModelRoleMissing):
        await agent.make_decision(_context(), "Buy AAPL")
    assert stub.count == 0


async def test_make_decision_stream_raises_and_yields_no_text(tmp_path, stub, key_env):
    _activate(tmp_path, stub, base_urls=DEAD_DECISION)
    agent = DecisionAgent(mcp_client=MagicMock())
    chunks = []
    with patch.object(agent, "_inject_context_layers", side_effect=lambda p, q: p):
        with pytest.raises(ProviderError):
            async for chunk in agent.make_decision_stream(_context(), "Buy AAPL"):
                chunks.append(chunk)
    assert chunks == []


async def _decide_through_the_real_agent(*args, **kwargs):
    """Stand-in for OrchestratorAgent.run that runs the real DecisionAgent."""
    agent = DecisionAgent(mcp_client=MagicMock())
    with patch.object(agent, "_inject_context_layers", side_effect=lambda p, q: p):
        return await agent.make_decision(_context(), "Buy AAPL")


async def test_chat_returns_502_with_the_typed_code_when_the_decision_provider_is_down(
    tmp_path, stub, key_env
):
    from chat_manager import ChatManager
    from routes.chat_routes import chat_message

    _activate(tmp_path, stub, base_urls=DEAD_DECISION)
    manager = ChatManager(db_path=":memory:")
    previous = (state.chat_manager, state.mcp_client)
    state.chat_manager, state.mcp_client = manager, MagicMock()
    try:
        with patch("agents.orchestrator_agent.OrchestratorAgent") as orchestrator:
            orchestrator.return_value.run = AsyncMock(side_effect=_decide_through_the_real_agent)
            with pytest.raises(HTTPException) as caught:
                await chat_message(ChatMessage(message="Buy AAPL"), accept="application/json")
        history = manager.load_history(manager.list_conversations()[0]["id"])
    finally:
        state.chat_manager, state.mcp_client = previous
        manager.close()
    assert caught.value.status_code == 502
    assert caught.value.detail == {"code": "PROVIDER_UNREACHABLE", "role": "decision"}
    assert [m for m in history if m["role"] == "assistant"] == []


async def test_chat_returns_503_with_the_typed_code_for_a_missing_role_at_decision_time(
    tmp_path, stub, key_env
):
    from chat_manager import ChatManager
    from routes.chat_routes import chat_message

    _activate(tmp_path, stub)
    manager = ChatManager(db_path=":memory:")
    previous = (state.chat_manager, state.mcp_client)
    state.chat_manager, state.mcp_client = manager, MagicMock()
    try:
        with patch("agents.orchestrator_agent.OrchestratorAgent") as orchestrator:
            orchestrator.return_value.run = AsyncMock(side_effect=ModelRoleMissing("decision"))
            with pytest.raises(HTTPException) as caught:
                await chat_message(ChatMessage(message="hi"), accept="application/json")
    finally:
        state.chat_manager, state.mcp_client = previous
        manager.close()
    assert caught.value.status_code == 503
    assert caught.value.detail == {"code": "ROLE_MISSING", "field": "decision"}


async def test_stream_emits_a_typed_error_event_and_no_recommendation_text(tmp_path, stub, key_env):
    from chat_manager import ChatManager
    from routes.chat_routes import stream_chat_generator

    _activate(tmp_path, stub, base_urls=DEAD_DECISION)
    manager = ChatManager(db_path=":memory:")
    previous = (state.chat_manager, state.mcp_client)
    state.chat_manager, state.mcp_client = manager, MagicMock()

    async def run_stream(*args, **kwargs):
        agent = DecisionAgent(mcp_client=MagicMock())
        with patch.object(agent, "_inject_context_layers", side_effect=lambda p, q: p):
            async for chunk in agent.make_decision_stream(_context(), "Buy AAPL"):
                yield chunk

    events = []
    try:
        with patch("agents.orchestrator_agent.OrchestratorAgent") as orchestrator, patch(
            "routes.chat_routes.update_conversation_title_if_needed", new=AsyncMock()
        ):
            orchestrator.return_value.run_stream = run_stream
            async for event in stream_chat_generator(ChatMessage(message="Buy AAPL")):
                events.append(event)
        saved = [
            m
            for c in manager.list_conversations()
            for m in manager.load_history(c["id"])
            if m["role"] == "assistant"
        ]
    finally:
        state.chat_manager, state.mcp_client = previous
        manager.close()

    kinds = [e.get("event") for e in events]
    assert "token" not in kinds
    error = next(e for e in events if e.get("event") == "error")
    payload = json.loads(error["data"])
    assert payload["code"] == "PROVIDER_UNREACHABLE"
    assert "Error generating recommendation" not in json.dumps(events)
    assert saved == []


# --- 2: an unreachable coordinator produces no decision and no proposal -----------------------


async def test_unreachable_coordinator_stops_the_request_before_any_decision(tmp_path, stub, key_env):
    _activate(tmp_path, stub, base_urls=DEAD_COORDINATOR)
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    orchestrator.decision_engine.make_decision = AsyncMock()
    orchestrator.data_fabricator.fabricate_context = AsyncMock()
    before = dict(state.trade_proposals)

    with pytest.raises(ProviderError) as caught:
        await orchestrator.run(query="How is AAPL doing?")

    assert caught.value.role == "coordinator"
    orchestrator.decision_engine.make_decision.assert_not_awaited()
    orchestrator.data_fabricator.fabricate_context.assert_not_awaited()
    assert stub.count == 0  # nothing reached the decision provider either
    assert state.trade_proposals == before


async def test_unreachable_coordinator_does_not_use_keyword_heuristics(tmp_path, stub, key_env):
    _activate(tmp_path, stub, base_urls=DEAD_COORDINATOR)
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    with patch.object(orchestrator, "_heuristic_classify") as heuristic:
        with pytest.raises(ProviderError):
            await orchestrator._classify_intent("price of AAPL")
    heuristic.assert_not_called()


# --- 3: risk_critic failures never become FLAGGED and never leave a proposal ------------------


async def test_risk_review_raises_a_provider_error_instead_of_flagging(tmp_path, stub, key_env):
    from agents.risk_agent import RiskAgent

    _activate(tmp_path, stub, role_provider={"risk_critic": "ollama"}, base_urls=DEAD_RISK)
    agent = RiskAgent()
    with pytest.raises(ProviderError) as caught:
        await agent.review(_context(), "BUY 1 share of AAPL")
    assert caught.value.code == "PROVIDER_UNREACHABLE"
    assert caught.value.role == "risk_critic"
    assert stub.count == 0


async def test_risk_review_raises_a_missing_role_instead_of_flagging(tmp_path, stub, key_env):
    from agents.risk_agent import RiskAgent

    _activate(tmp_path, stub, roles=["coordinator", "decision"])
    with pytest.raises(ModelRoleMissing) as caught:
        await RiskAgent().review(_context(), "BUY 1 share of AAPL")
    assert caught.value.field == "risk_critic"
    assert stub.count == 0


async def test_risk_analyze_raises_typed_errors_directly(tmp_path, stub, key_env):
    from agents.risk_agent import RiskAgent

    _activate(tmp_path, stub, roles=["coordinator", "decision"])
    with pytest.raises(ModelRoleMissing):
        await RiskAgent().analyze({"context": _context(), "suggestion": "BUY 1 share of AAPL"})


async def test_risk_review_still_flags_ordinary_non_model_failures(tmp_path, stub, key_env):
    from agents.risk_agent import RiskAgent

    _activate(tmp_path, stub)
    with patch("agents.risk_agent.run_magentic", new=AsyncMock(side_effect=ValueError("bad output"))):
        review = await RiskAgent().review(_context(), "BUY 1 share of AAPL")
    assert review["status"] == "FLAGGED"
    assert review["requires_hitl"] is True


async def test_missing_risk_critic_stops_the_orchestrator_before_any_model_call(tmp_path, stub, key_env):
    _activate(tmp_path, stub, roles=["coordinator", "decision"])
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    orchestrator.decision_engine.make_decision = AsyncMock()
    before = dict(state.trade_proposals)

    with pytest.raises(ModelRoleMissing) as caught:
        await orchestrator.run(query="How is AAPL doing?")

    assert caught.value.field == "risk_critic"
    orchestrator.decision_engine.make_decision.assert_not_awaited()
    assert stub.count == 0
    assert state.trade_proposals == before


async def test_chat_route_is_503_when_risk_critic_is_missing(tmp_path, stub, key_env):
    from routes.chat_routes import chat_message

    _activate(tmp_path, stub, roles=["coordinator", "decision"])
    with pytest.raises(HTTPException) as caught:
        await chat_message(ChatMessage(message="hi"), accept="application/json")
    assert caught.value.status_code == 503
    assert caught.value.detail == {"code": "ROLE_MISSING", "role": "risk_critic"}
    assert stub.count == 0


async def test_risk_provider_failure_after_registration_voids_the_proposal(
    tmp_path, stub, key_env, private_config_dir
):
    """make_decision registers the proposal before the risk review runs. If the
    risk_critic provider then fails, the proposal is rejected and the error raised."""
    _activate(tmp_path, stub, role_provider={"risk_critic": "ollama"}, base_urls=DEAD_RISK)
    stub.reply_text = "INTENT: price_check\nTICKER: NONE\nREASON: stub"
    assert state.start_execution(
        tmp_path / "execution.sqlite3", workspace="uk", private_dir=private_config_dir
    )
    try:
        orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
        context = _context()
        orchestrator.data_fabricator.fabricate_context = AsyncMock(return_value=context)

        registered = {}

        async def decide_and_register(ctx, query, images=None):
            agent = DecisionAgent(mcp_client=MagicMock())
            proposal = agent._extract_trade_proposal("BUY 1 share of AAPL.", ctx)
            assert proposal is not None
            # Chat proposals carry no account or broker and are refused today; supply
            # them so this test exercises a proposal that really is registered.
            proposal.update({"account": "invest", "broker": "paper", "mode": "PAPER"})
            assert agent._register_for_human_review(proposal, ctx)
            ctx.user_context["pending_proposal"] = proposal
            registered["id"] = proposal["proposal_id"]
            return {"content": "BUY 1 share of AAPL.", "response_id": None, "quick_actions": []}

        orchestrator.decision_engine.make_decision = AsyncMock(side_effect=decide_and_register)

        with pytest.raises(ProviderError) as caught:
            await orchestrator.run(query="Buy AAPL", ticker="AAPL")

        assert caught.value.role == "risk_critic"
        assert "id" in registered, "the decision step must have run and registered a proposal"
        stored = state.get_trade_proposal(registered["id"])
        assert str(stored["status"]).upper() == "REJECTED"
        assert "pending_proposal" not in context.user_context
    finally:
        state.close_execution()


# --- 4: research provider failures are not a basic query ----------------------------------------


async def test_research_provider_failure_is_not_a_basic_query(tmp_path, stub, key_env):
    from agents.research_agent import ResearchAgent

    _activate(tmp_path, stub, base_urls=kit.dead_provider_urls("ollama"))
    with pytest.raises(ProviderError) as caught:
        await ResearchAgent()._generate_smart_query("AAPL")
    assert caught.value.role == "research"
    assert stub.count == 0


async def test_research_still_degrades_on_a_non_model_error(tmp_path, stub, key_env):
    from agents.research_agent import ResearchAgent

    _activate(tmp_path, stub)
    with patch("agents.research_agent.run_magentic", new=AsyncMock(side_effect=ValueError("bad output"))):
        params = await ResearchAgent()._generate_smart_query("AAPL")
    assert params["q"] == "AAPL stock market news"

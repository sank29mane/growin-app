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


async def test_risk_review_rejects_ordinary_non_model_failures(tmp_path, stub, key_env):
    from agents.risk_agent import RiskAgent

    _activate(tmp_path, stub)
    with patch("agents.risk_agent.run_magentic", new=AsyncMock(side_effect=ValueError("bad output"))):
        with pytest.raises(ProviderError) as caught:
            await RiskAgent().review(_context(), "BUY 1 share of AAPL")
    assert caught.value.code == "CRITIC_OUTPUT_INVALID"
    assert caught.value.role == "risk_critic"


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


async def _run_orchestrator_with_real_decision(
    orchestrator, stub, events, *, context_intent="analytical", authority_fields=True, streaming=False, output=None, context=None
):
    """Drive OrchestratorAgent.run with the real DecisionAgent against the stub.

    Routing, data fabrication and price validation are replaced (they would need
    the network); the decision and risk steps are real. ``events`` records, in
    order, "register" (human-review registration) and "broadcast" (the
    rebalance_proposal message), each with the models the stub had seen so far.
    """
    stub.reply_text = "BUY 1 share of AAPL."
    context = context or _context(intent=context_intent)
    orchestrator._classify_intent = AsyncMock(
        return_value={"type": "price_check", "needs": [], "primary_ticker": "AAPL", "reason": "test"}
    )
    orchestrator.data_fabricator.fabricate_context = AsyncMock(return_value=context)

    real_register = DecisionAgent._register_for_human_review
    real_extract = DecisionAgent._extract_trade_proposal

    def spy_register(self, proposal, ctx):
        events.append(("register", [r.model for r in stub.snapshot()]))
        return real_register(self, proposal, ctx)

    def extract_with_identity(self, text, ctx):
        proposal = real_extract(self, text, ctx)
        if proposal is not None and authority_fields:
            # Chat proposals carry no account or broker and are refused today; supply
            # them so registration can really succeed.
            proposal.update({"account": "invest", "broker": "paper", "mode": "PAPER"})
        return proposal

    real_send = orchestrator.messenger.send_message

    async def spy_send(message):
        if message.subject == "rebalance_proposal":
            events.append(("broadcast", [r.model for r in stub.snapshot()]))
        return await real_send(message)

    orchestrator.messenger.send_message = spy_send
    with patch.object(DecisionAgent, "_register_for_human_review", spy_register), patch.object(
        DecisionAgent, "_extract_trade_proposal", extract_with_identity
    ), patch.object(DecisionAgent, "_inject_context_layers", lambda self, p, q: p), patch(
        "agents.decision_agent.PriceValidator.validate_trade_price",
        new=AsyncMock(return_value={"action": "allow"}),
    ):
        if streaming:
            chunks = []
            async for event in orchestrator.run_stream(query="Buy AAPL", ticker="AAPL"):
                if isinstance(event, str):
                    chunks.append(event)
                    if output is not None:
                        output.append(event)
            return {"content": "".join(chunks)}, context
        return await orchestrator.run(query="Buy AAPL", ticker="AAPL"), context


async def test_risk_provider_failure_registers_and_broadcasts_nothing(tmp_path, stub, key_env):
    _activate(tmp_path, stub, role_provider={"risk_critic": "ollama"}, base_urls=DEAD_RISK)
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    events = []
    before = dict(state.trade_proposals)

    with pytest.raises(ProviderError) as caught:
        await _run_orchestrator_with_real_decision(orchestrator, stub, events)

    assert caught.value.role == "risk_critic"
    # The decision ran (it produced a BUY) but nothing was registered or announced.
    assert kit.model_id_for("decision") in [r.model for r in stub.snapshot()]
    assert events == []
    assert state.trade_proposals == before


async def test_rebuttal_failure_registers_and_broadcasts_nothing(tmp_path, stub, key_env):
    """A FLAGGED review triggers a rebuttal from the decision role. If that fails, nothing is registered."""
    _activate(tmp_path, stub)
    stub.tool_arguments["return_riskassessment"] = {
        **kit.STUB_TOOL_ARGUMENTS["return_riskassessment"],
        "status": "FLAGGED",
    }
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    events = []
    calls = {"n": 0}

    async def failing_rebuttal(prompt):
        calls["n"] += 1
        raise ProviderError("PROVIDER_UNREACHABLE", "decision")

    orchestrator.decision_engine.generate_response = failing_rebuttal
    with pytest.raises(ProviderError):
        await _run_orchestrator_with_real_decision(orchestrator, stub, events)
    assert calls["n"] == 1
    assert events == []


@pytest.mark.parametrize("streaming", [False, True])
async def test_after_a_successful_review_the_proposal_is_registered_then_broadcast(
    tmp_path, stub, key_env, private_config_dir, streaming
):
    _activate(tmp_path, stub)
    assert state.start_execution(
        tmp_path / "execution.sqlite3", workspace="uk", private_dir=private_config_dir
    )
    try:
        orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
        events = []
        result, context = await _run_orchestrator_with_real_decision(orchestrator, stub, events, streaming=streaming)

        assert [name for name, _ in events] == ["register", "broadcast"]
        risk_model = kit.model_id_for("risk_critic")
        # The critic had already answered when the proposal was registered and announced.
        assert all(risk_model in models for _name, models in events)
        proposal = context.user_context["pending_proposal"]
        assert f"[ACTION_REQUIRED:APPROVE_TRADE({proposal['proposal_id']})]" in result["content"]
        assert str(state.get_trade_proposal(proposal["proposal_id"])["status"]).upper() == "PENDING"
    finally:
        state.close_execution()


async def test_a_refused_registration_is_reported_in_the_reply_and_not_broadcast(tmp_path, stub, key_env):
    _activate(tmp_path, stub)
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    events = []
    result, context = await _run_orchestrator_with_real_decision(
        orchestrator, stub, events, authority_fields=False
    )
    assert [name for name, _ in events] == ["register"]  # attempted once, refused (no ledger)
    assert "TRADE_PROPOSAL_NOT_REGISTERED" in result["content"]
    assert "pending_proposal" not in context.user_context


async def test_conversational_proposal_requires_a_risk_review(tmp_path, stub, key_env):
    _activate(tmp_path, stub)
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    events = []
    result, _context_after = await _run_orchestrator_with_real_decision(
        orchestrator, stub, events, context_intent="conversational", authority_fields=False
    )
    assert [name for name, _ in events] == ["register"]
    assert all(kit.model_id_for("risk_critic") in models for _, models in events)
    assert result["content"]


async def test_no_reject_path_remains_to_swallow_a_failure():
    """With the critic first, a failed review leaves nothing registered, so nothing to reject."""
    import inspect

    import agents.orchestrator_agent as orchestrator_module

    source = inspect.getsource(orchestrator_module)
    assert "_void_pending_proposal" not in source
    assert "execution_service.reject" not in source


# --- 4: research provider failures are not a basic query ----------------------------------------


async def test_research_provider_failure_is_not_a_basic_query(tmp_path, stub, key_env):
    from agents.research_agent import ResearchAgent

    _activate(tmp_path, stub, base_urls=kit.dead_provider_urls("ollama"))
    with pytest.raises(ProviderError) as caught:
        await ResearchAgent()._generate_smart_query("AAPL")
    assert caught.value.role == "research"
    assert stub.count == 0


async def test_newsdata_fetch_does_not_turn_a_provider_failure_into_an_empty_list(tmp_path, stub, key_env, monkeypatch):
    from agents.research_agent import ResearchAgent

    monkeypatch.setenv("NEWSDATA_API_KEY", "valid_key_length_greater_than_10")
    _activate(tmp_path, stub, base_urls=kit.dead_provider_urls("ollama"))
    agent = ResearchAgent()
    with pytest.raises(ProviderError) as caught:
        await agent._fetch_newsdata("AAPL", "Apple")
    assert caught.value.role == "research"
    assert stub.count == 0


async def test_research_analysis_fails_instead_of_reporting_neutral_news(tmp_path, stub, key_env, monkeypatch):
    from agents.research_agent import ResearchAgent

    monkeypatch.setenv("NEWSDATA_API_KEY", "valid_key_length_greater_than_10")
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    _activate(tmp_path, stub, base_urls=kit.dead_provider_urls("ollama"))
    agent = ResearchAgent()
    agent._fetch_regulatory_news = AsyncMock(return_value=[])
    with pytest.raises(ProviderError) as caught:
        await agent.execute({"ticker": "AAPL"})
    assert caught.value.code == "PROVIDER_UNREACHABLE"
    assert caught.value.role == "research"


async def test_newsdata_still_returns_an_empty_list_for_a_plain_http_failure(tmp_path, stub, key_env, monkeypatch):
    """The external news API is not a model: its failures stay soft."""
    from agents.research_agent import ResearchAgent

    monkeypatch.setenv("NEWSDATA_API_KEY", "valid_key_length_greater_than_10")
    _activate(tmp_path, stub)
    agent = ResearchAgent()
    with patch(
        "agents.research_agent.agent_http_client.execute_with_breaker",
        new=AsyncMock(side_effect=RuntimeError("news api down")),
    ):
        assert await agent._fetch_newsdata("AAPL", "Apple") == []


async def test_research_still_degrades_on_a_non_model_error(tmp_path, stub, key_env):
    from agents.research_agent import ResearchAgent

    _activate(tmp_path, stub)
    with patch("agents.research_agent.run_magentic", new=AsyncMock(side_effect=ValueError("bad output"))):
        params = await ResearchAgent()._generate_smart_query("AAPL")
    assert params["q"] == "AAPL stock market news"


@pytest.mark.parametrize("intent", ["conversational", "educational"])
async def test_conversational_unreachable_critic_leaves_no_registration(tmp_path, stub, key_env, intent):
    _activate(tmp_path, stub, role_provider={"risk_critic": "ollama"}, base_urls=DEAD_RISK)
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    events = []
    with pytest.raises(ProviderError):
        await _run_orchestrator_with_real_decision(orchestrator, stub, events, context_intent=intent)
    assert events == []


async def test_conversational_approved_critic_registers_once_after_review(
    tmp_path, stub, key_env, private_config_dir
):
    _activate(tmp_path, stub)
    assert state.start_execution(tmp_path / "execution.sqlite3", workspace="uk", private_dir=private_config_dir)
    try:
        events = []
        orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
        await _run_orchestrator_with_real_decision(orchestrator, stub, events, context_intent="conversational")
        assert [name for name, _ in events] == ["register", "broadcast"]
        assert all(kit.model_id_for("risk_critic") in models for _, models in events)
    finally:
        state.close_execution()


async def test_ace_failure_leaves_no_registration(tmp_path, stub, key_env):
    _activate(tmp_path, stub)
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    events = []
    with patch("agents.ace_evaluator.ACEEvaluator.calculate_score", side_effect=RuntimeError("ACE failed")):
        with pytest.raises(RuntimeError, match="ACE failed"):
            await _run_orchestrator_with_real_decision(orchestrator, stub, events)
    assert events == []


@pytest.mark.parametrize("failure", ["missing", "unreachable"])
async def test_decision_requesting_math_fails_closed(tmp_path, stub, key_env, failure):
    from agents.math_generator_agent import MathGeneratorAgent

    kwargs = {"roles": ["decision", "coordinator", "risk_critic"]} if failure == "missing" else {
        "base_urls": DEAD_COORDINATOR
    }
    _activate(tmp_path, stub, **kwargs)
    expected = ModelRoleMissing if failure == "missing" else ProviderError
    agent = MathGeneratorAgent()
    with pytest.raises(expected) as caught:
        await agent.execute({"query": "calculate", "context_data": {}, "required_stats": []})
    assert caught.value.role == "math_codegen"
    decision = DecisionAgent(mcp_client=MagicMock())
    decision._run_agentic_loop = AsyncMock(return_value={"content": "must not run"})
    with patch.object(DecisionAgent, "_inject_context_layers", lambda self, p, q: p):
        with pytest.raises(expected):
            await decision.make_decision(_context(), "calculate a projection", defer_proposal=True)
    decision._run_agentic_loop.assert_not_awaited()
    assert stub.count == 0


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure", ["missing", "unreachable"])
async def test_research_typed_error_survives_both_orchestration_paths(
    tmp_path, stub, key_env, monkeypatch, streaming, failure
):
    from agents.research_agent import ResearchAgent

    monkeypatch.setenv("NEWSDATA_API_KEY", "valid_key_length_greater_than_10")
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    kwargs = {"roles": ["coordinator", "decision", "risk_critic"]} if failure == "missing" else {
        "base_urls": kit.dead_provider_urls("ollama")
    }
    _activate(tmp_path, stub, **kwargs)
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    orchestrator.research_agent = ResearchAgent()
    orchestrator.research_agent._fetch_regulatory_news = AsyncMock(return_value=[])
    orchestrator._classify_intent = AsyncMock(return_value={"type": "conversational", "needs": ["research"], "reason": "test"})
    orchestrator.data_fabricator.fabricate_context = AsyncMock(return_value=_context(intent="conversational"))
    expected = ModelRoleMissing if failure == "missing" else ProviderError
    with pytest.raises(expected) as caught:
        if streaming:
            async for _ in orchestrator.run_stream("news", account_type="invest"):
                pass
        else:
            await orchestrator.run("news", account_type="invest")
    assert caught.value.code == ("ROLE_MISSING" if failure == "missing" else "PROVIDER_UNREACHABLE")
    assert stub.count == 0


@pytest.mark.parametrize("intent", ["analytical", "conversational", "educational"])
@pytest.mark.parametrize("unreachable", [False, True])
async def test_stream_proposal_is_reviewed_before_registration_or_output(
    tmp_path, stub, key_env, private_config_dir, intent, unreachable
):
    _activate(tmp_path, stub, **({"role_provider": {"risk_critic": "ollama"}, "base_urls": DEAD_RISK} if unreachable else {}))
    assert state.start_execution(tmp_path / "execution.sqlite3", workspace="uk", private_dir=private_config_dir)
    try:
        orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
        events = []
        if unreachable:
            output = []
            with pytest.raises(ProviderError):
                await _run_orchestrator_with_real_decision(orchestrator, stub, events, context_intent=intent, streaming=True, output=output)
            assert events == []
            assert output == []
        else:
            result, context = await _run_orchestrator_with_real_decision(orchestrator, stub, events, context_intent=intent, streaming=True)
            assert [name for name, _ in events] == ["register", "broadcast"]
            assert all(kit.model_id_for("risk_critic") in models for _, models in events)
            assert context.user_context["pending_proposal"]["proposal_id"] in result["content"]
    finally:
        state.close_execution()


@pytest.mark.parametrize("failure", ["missing", "unreachable"])
async def test_chat_research_failure_returns_typed_http_error(tmp_path, stub, key_env, monkeypatch, failure):
    from chat_manager import ChatManager
    from routes.chat_routes import chat_message
    from agents.research_agent import ResearchAgent

    monkeypatch.setenv("NEWSDATA_API_KEY", "valid_key_length_greater_than_10")
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    _activate(tmp_path, stub, **({"roles": ["coordinator", "decision", "risk_critic"]} if failure == "missing" else {
        "base_urls": kit.dead_provider_urls("ollama")
    }))
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    orchestrator.research_agent = ResearchAgent()
    orchestrator.research_agent._fetch_regulatory_news = AsyncMock(return_value=[])
    orchestrator._classify_intent = AsyncMock(return_value={"type": "conversational", "needs": ["research"], "reason": "test"})
    orchestrator.data_fabricator.fabricate_context = AsyncMock(return_value=_context(intent="conversational"))
    manager = ChatManager(db_path=":memory:")
    previous = (state.chat_manager, state.mcp_client)
    state.chat_manager, state.mcp_client = manager, MagicMock()
    try:
        with patch("agents.orchestrator_agent.OrchestratorAgent", return_value=orchestrator):
            with pytest.raises(HTTPException) as caught:
                await chat_message(ChatMessage(message="news", account_type="invest"), accept="application/json")
        assert caught.value.status_code == (503 if failure == "missing" else 502)
        assert caught.value.detail == ({"code": "ROLE_MISSING", "field": "research"} if failure == "missing" else {
            "code": "PROVIDER_UNREACHABLE", "role": "research"
        })
        assert all(m["role"] != "assistant" for m in manager.load_history(manager.list_conversations()[0]["id"]))
    finally:
        state.chat_manager, state.mcp_client = previous
        manager.close()


@pytest.mark.parametrize("intent", ["analytical", "conversational", "educational"])
@pytest.mark.parametrize("review", [None, {}])
async def test_release_helper_refuses_unreviewed_proposal_before_any_side_effect(intent, review):
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    context = _context(intent=intent)
    held = {"ticker": "AAPL", "action": "BUY", "quantity": 1}
    context.user_context["deferred_proposal"] = held
    if review is not None:
        context.user_context["risk_review"] = review
    orchestrator.decision_engine.register_deferred_proposal = MagicMock(return_value=None)
    orchestrator.messenger.send_message = AsyncMock()
    before = dict(state.trade_proposals)

    with pytest.raises(ModelRegistryError) as caught:
        await orchestrator._release_proposal(context, "held response", "sweep")

    assert caught.value.code == "RISK_REVIEW_REQUIRED"
    assert caught.value.field == "risk_critic"
    orchestrator.decision_engine.register_deferred_proposal.assert_not_called()
    orchestrator.messenger.send_message.assert_not_awaited()
    assert state.trade_proposals == before
    assert context.user_context["deferred_proposal"] == held
    assert "pending_proposal" not in context.user_context


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("intent", ["analytical", "conversational", "educational"])
async def test_schema_invalid_critic_registers_and_broadcasts_nothing(
    tmp_path, stub, key_env, private_config_dir, streaming, intent
):
    _activate(tmp_path, stub)
    stub.tool_arguments["return_riskassessment"] = {"status": "NOT_A_VERDICT"}
    assert state.start_execution(tmp_path / "execution.sqlite3", workspace="uk", private_dir=private_config_dir)
    try:
        orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
        events = []
        before = dict(state.trade_proposals)
        with pytest.raises(ProviderError) as caught:
            await _run_orchestrator_with_real_decision(
                orchestrator, stub, events, context_intent=intent, streaming=streaming
            )
        assert caught.value.code == "CRITIC_OUTPUT_INVALID"
        assert caught.value.role == "risk_critic"
        assert kit.model_id_for("risk_critic") in [r.model for r in stub.snapshot()]
        assert events == []
        assert state.trade_proposals == before
    finally:
        state.close_execution()


@pytest.mark.parametrize("boundary", ["_register_for_human_review", "register_deferred_proposal"])
async def test_direct_registration_requires_critic_success_marker(boundary):
    agent = DecisionAgent(mcp_client=MagicMock())
    context = _context()
    proposal = {"proposal_id": "unreviewed", "ticker": "AAPL", "action": "BUY", "quantity": 1}
    context.user_context["deferred_proposal"] = proposal
    context.user_context["risk_review"] = {"status": "APPROVED"}
    with patch.object(state, "register_trade_proposal") as register:
        with pytest.raises(ModelRegistryError) as caught:
            if boundary == "_register_for_human_review":
                agent._register_for_human_review(proposal, context)
            else:
                agent.register_deferred_proposal(context)
    assert caught.value.code == "RISK_REVIEW_REQUIRED"
    register.assert_not_called()
    assert context.user_context["deferred_proposal"] is proposal


async def test_failed_review_clears_previous_success_binding(tmp_path, stub, key_env):
    from agents.risk_agent import RiskAgent

    _activate(tmp_path, stub)
    context = _context()
    from agents.critic_binding import require_review
    proposal = {"proposal_id": "reviewed", "ticker": "AAPL", "action": "BUY", "quantity": 1}
    context.user_context["deferred_proposal"] = proposal
    await RiskAgent().review(context, "BUY AAPL")
    require_review(context, proposal)
    stub.tool_arguments["return_riskassessment"] = {}
    with pytest.raises(ProviderError):
        await RiskAgent().review(context, "BUY MSFT")
    with pytest.raises(ModelRegistryError):
        require_review(context, proposal)


@pytest.mark.parametrize("stage", ["reflex", "synthesis"])
async def test_unreachable_swarm_stream_is_typed(tmp_path, stub, key_env, stage):
    from contextlib import asynccontextmanager
    import httpx
    import openai
    from agents.orchestrator import SwarmOrchestrator
    from agents.swarm_utils import AgentResult

    _activate(tmp_path, stub, **({"base_urls": DEAD_COORDINATOR} if stage == "reflex" else {}))
    swarm = SwarmOrchestrator(reflex_timeout=0.01, synthesis_timeout=0.01)
    await swarm.buffer.push(AgentResult(source="QuantEngine", data={}, conviction=8))
    if stage == "synthesis":
        await swarm.buffer.push(AgentResult(source="ResearchAgent", data={}, conviction=6))
        real_stream = swarm.agent.run_stream
        @asynccontextmanager
        async def stream(prompt, **kwargs):
            if kwargs.get("message_history") is not None:
                raise openai.APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1"))
            async with real_stream(prompt, **kwargs) as result:
                yield result
        swarm.agent.run_stream = stream
    with pytest.raises(ProviderError) as caught:
        async for _ in swarm.stream_swarm_run("What now?"):
            pass
    assert caught.value.code == "PROVIDER_UNREACHABLE"
    assert caught.value.role == "coordinator"


@pytest.mark.parametrize("boundary", ["registration", "broadcast"])
@pytest.mark.parametrize("marker", [True, "matching_hash"])
async def test_forged_critic_marker_is_refused(boundary, marker):
    context = _context()
    proposal = {"proposal_id": "forged", "ticker": "AAPL", "quantity": 1}
    from agents.critic_binding import proposal_identity
    if marker == "matching_hash":
        proposal_id, content_hash = proposal_identity(proposal)
        marker = {"proposal_id": proposal_id, "content_hash": content_hash}
    context.user_context.update(risk_review_succeeded=marker, risk_review_binding=marker)
    context.user_context["deferred_proposal" if boundary == "registration" else "pending_proposal"] = proposal
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    orchestrator.messenger.send_message = AsyncMock()
    with patch.object(state, "register_trade_proposal") as register:
        with pytest.raises(ModelRegistryError) as caught:
            if boundary == "registration":
                orchestrator.decision_engine.register_deferred_proposal(context)
            else:
                await orchestrator._release_proposal(context, "BUY AAPL", None)
    assert caught.value.code == "RISK_REVIEW_REQUIRED"
    register.assert_not_called()
    orchestrator.messenger.send_message.assert_not_awaited()


@pytest.mark.parametrize("change", [{"proposal_id": "different"}, {"quantity": 2}])
@pytest.mark.parametrize("boundary", ["registration", "broadcast"])
async def test_critic_binding_refuses_different_proposal(tmp_path, stub, key_env, change, boundary):
    from agents.risk_agent import RiskAgent

    _activate(tmp_path, stub)
    context = _context()
    proposal = {"proposal_id": "reviewed", "ticker": "AAPL", "quantity": 1}
    context.user_context["deferred_proposal"] = proposal
    await RiskAgent().review(context, "BUY 1 share of AAPL")
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    orchestrator.messenger.send_message = AsyncMock()
    with patch.object(state, "register_trade_proposal") as register:
        if boundary == "broadcast":
            orchestrator.decision_engine.register_deferred_proposal(context)
            register.assert_called_once()
            register.reset_mock()
        proposal.update(change)
        with pytest.raises(ModelRegistryError) as caught:
            if boundary == "registration":
                orchestrator.decision_engine.register_deferred_proposal(context)
            else:
                await orchestrator._release_proposal(context, "BUY AAPL", None)
    assert caught.value.code == "RISK_REVIEW_REQUIRED"
    register.assert_not_called()
    orchestrator.messenger.send_message.assert_not_awaited()


async def test_critic_binding_cannot_register_or_broadcast_twice(tmp_path, stub, key_env):
    from agents.risk_agent import RiskAgent

    _activate(tmp_path, stub)
    context = _context()
    proposal = {"proposal_id": "reviewed", "ticker": "AAPL", "quantity": 1}
    context.user_context["deferred_proposal"] = proposal
    await RiskAgent().review(context, "BUY AAPL")
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    orchestrator.messenger.send_message = AsyncMock()
    with patch.object(state, "register_trade_proposal") as register:
        orchestrator.decision_engine.register_deferred_proposal(context)
        with pytest.raises(ModelRegistryError) as caught:
            orchestrator.decision_engine._register_for_human_review(proposal, context)
        assert caught.value.code == "RISK_REVIEW_REQUIRED"
        await orchestrator._release_proposal(context, "BUY AAPL", None)
        with pytest.raises(ModelRegistryError) as caught:
            await orchestrator._release_proposal(context, "BUY AAPL", None)
        assert caught.value.code == "RISK_REVIEW_REQUIRED"
    register.assert_called_once()
    orchestrator.messenger.send_message.assert_awaited_once()


@pytest.mark.parametrize("streaming", [False, True])
async def test_new_extraction_clears_successful_critic_binding(tmp_path, stub, key_env, streaming):
    from agents.risk_agent import RiskAgent

    _activate(tmp_path, stub)
    context = _context()
    proposal = {"proposal_id": "same-content", "ticker": "AAPL", "quantity": 1}
    context.user_context["deferred_proposal"] = proposal
    await RiskAgent().review(context, "BUY AAPL")
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())

    async def refuse_before_new_review(ctx, suggestion):
        # Even identical content from a new extraction requires a fresh review.
        with pytest.raises(ModelRegistryError) as caught:
            orchestrator.decision_engine.register_deferred_proposal(ctx)
        assert caught.value.code == "RISK_REVIEW_REQUIRED"
        raise ProviderError("STOP_BEFORE_NEW_REVIEW", "risk_critic")

    orchestrator.risk_agent.review = AsyncMock(side_effect=refuse_before_new_review)
    with patch.object(DecisionAgent, "_extract_trade_proposal", return_value=proposal), patch.object(
        state, "register_trade_proposal"
    ) as register:
        with pytest.raises(ProviderError) as caught:
            await _run_orchestrator_with_real_decision(
                orchestrator, stub, [], streaming=streaming, context=context, authority_fields=False
            )
        assert caught.value.code == "STOP_BEFORE_NEW_REVIEW"
    register.assert_not_called()


async def test_critic_hash_distinguishes_decimal_from_forged_container(tmp_path, stub, key_env):
    from decimal import Decimal
    from agents.risk_agent import RiskAgent

    _activate(tmp_path, stub)
    context = _context()
    proposal = {"proposal_id": "reviewed", "ticker": "AAPL", "quantity": Decimal("1")}
    context.user_context["deferred_proposal"] = proposal
    await RiskAgent().review(context, "BUY AAPL")
    proposal["quantity"] = {"decimal": "1"}
    with patch.object(state, "register_trade_proposal") as register:
        with pytest.raises(ModelRegistryError) as caught:
            DecisionAgent(mcp_client=MagicMock()).register_deferred_proposal(context)
    assert caught.value.code == "RISK_REVIEW_REQUIRED"
    register.assert_not_called()


async def test_refused_registration_cannot_reuse_binding_for_broadcast(tmp_path, stub, key_env):
    from agents.risk_agent import RiskAgent
    from execution import ExecutionDisabledError

    _activate(tmp_path, stub)
    context = _context()
    proposal = {"proposal_id": "reviewed", "ticker": "AAPL", "quantity": 1}
    context.user_context["deferred_proposal"] = proposal
    await RiskAgent().review(context, "BUY AAPL")
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    orchestrator.messenger.send_message = AsyncMock()
    with patch.object(state, "register_trade_proposal", side_effect=ExecutionDisabledError("no ledger")) as register:
        assert orchestrator.decision_engine.register_deferred_proposal(context)
        context.user_context["pending_proposal"] = proposal
        with pytest.raises(ModelRegistryError) as caught:
            await orchestrator._release_proposal(context, "BUY AAPL", None)
    assert caught.value.code == "RISK_REVIEW_REQUIRED"
    register.assert_called_once()
    orchestrator.messenger.send_message.assert_not_awaited()

"""AC-3 (call-site half), AC-10, AC-11, AC-12 and the agent half of AC-14.

Each call site runs against the loopback stub server with a registry whose
model ids are unique per role, so a request that reached the wrong role is
visible in the recorded model. Nothing here touches a real network.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import model_registry_testkit as kit
from agents.decision_agent import DecisionAgent, extract_tool_calls
from market_context import MarketContext, PortfolioData, PriceData
from model_registry import (
    ModelRegistryUnavailable,
    ModelRoleMissing,
    set_active_registry,
)
from model_registry.provider import run_magentic
from model_registry_testkit import (  # noqa: F401  (fixtures)
    key_env,
    network_guard,
    registry_factory,
    stub,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_real_network(network_guard):
    return network_guard


def _models(stub_server):
    return [r.model for r in stub_server.snapshot()]


def _prefixes(stub_server):
    return [r.prefix for r in stub_server.snapshot()]


# --- AC-3: a missing role fails closed at the call site ---------------------------------


async def test_research_surfaces_a_missing_role_and_sends_nothing(stub, registry_factory):
    registry_factory(roles=["coordinator", "decision", "risk_critic", "math_codegen"])
    from agents.research_agent import ResearchAgent

    agent = ResearchAgent()
    with pytest.raises(ModelRoleMissing) as caught:
        await agent._generate_smart_query("AAPL")
    assert caught.value.code == "ROLE_MISSING"
    assert caught.value.field == "research"
    assert stub.count == 0


async def test_other_roles_keep_working_when_one_is_missing(stub, registry_factory):
    registry_factory(roles=["coordinator", "decision"])
    agent = DecisionAgent(mcp_client=MagicMock())
    assert await agent.generate_response("hello") == stub.reply_text
    assert _models(stub) == [kit.model_id_for("decision")]


async def test_decision_without_a_registry_sends_nothing(stub):
    set_active_registry(None)
    agent = DecisionAgent(mcp_client=MagicMock())
    with pytest.raises(ModelRegistryUnavailable):
        await agent.generate_response("hello")
    assert stub.count == 0


async def test_orchestrator_without_coordinator_role_fails_closed(stub, registry_factory):
    registry_factory(roles=["decision"])
    from agents.orchestrator_agent import OrchestratorAgent

    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    with pytest.raises(ModelRoleMissing) as caught:
        await orchestrator._classify_intent("how is AAPL doing")
    assert caught.value.field == "coordinator"
    assert stub.count == 0


async def test_math_without_its_role_reports_failure_and_sends_nothing(stub, registry_factory):
    registry_factory(roles=["decision"])
    from agents.math_generator_agent import MathGeneratorAgent

    agent = MathGeneratorAgent()
    response = await agent.analyze({"query": "simulate", "context_data": {}, "required_stats": []})
    assert response.success is False
    assert "ROLE_MISSING" in (response.error or "")
    assert stub.count == 0


# --- AC-10: each call site uses its role ---------------------------------------------------


async def test_orchestrator_routes_with_the_coordinator_role(stub, registry_factory):
    registry_factory()
    from agents.orchestrator_agent import OrchestratorAgent

    stub.reply_text = "INTENT: price_check\nTICKER: AAPL\nREASON: stub"
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    result = await orchestrator._classify_intent("price of AAPL")
    assert result["type"] == "price_check"
    assert _models(stub) == [kit.model_id_for("coordinator")]
    assert _prefixes(stub) == ["lmstudio"]


async def test_orchestrator_decision_engine_uses_the_decision_role(stub, registry_factory, key_env):
    registry_factory()
    from agents.orchestrator_agent import OrchestratorAgent

    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    await orchestrator.decision_engine._initialize_llm()
    text = await orchestrator.decision_engine.generate_response("hi")
    assert text == stub.reply_text
    assert _models(stub) == [kit.model_id_for("decision")]
    assert stub.snapshot()[0].headers["authorization"] == f"Bearer {key_env}"
    assert orchestrator.model_name == kit.model_id_for("decision")


async def test_title_generation_uses_the_decision_role(stub, registry_factory):
    registry_factory()
    from app_context import state
    from chat_manager import ChatManager
    from routes.chat_routes import generate_conversation_title

    manager = ChatManager(db_path=":memory:")
    previous = state.chat_manager
    state.chat_manager = manager
    try:
        conversation_id = manager.create_conversation("seed")
        manager.save_message(conversation_id, "user", "Tell me about AAPL earnings")
        stub.reply_text = "AAPL Earnings Review"
        result = await generate_conversation_title(conversation_id)
    finally:
        state.chat_manager = previous
        manager.close()
    assert result["title"]
    assert _models(stub) == [kit.model_id_for("decision")]


async def test_extract_tool_calls_uses_the_decision_role(stub, registry_factory):
    registry_factory()
    calls = await run_magentic("decision", extract_tool_calls, "content", "reasoning")
    assert calls == []
    assert _models(stub) == [kit.model_id_for("decision")]
    assert _prefixes(stub) == ["xai"]


async def test_agentic_loop_sends_only_decision_model_requests(stub, registry_factory):
    registry_factory()
    agent = DecisionAgent(mcp_client=MagicMock())
    await agent._initialize_llm()
    context = MarketContext(query="hi", intent="conversational")
    result = await agent._run_agentic_loop("system", "prompt", context)
    assert result["content"] == stub.reply_text
    # One chat completion, one structured tool-extraction call. Both are the decision role.
    assert set(_models(stub)) == {kit.model_id_for("decision")}
    assert len(stub.snapshot()) == 2
    assert set(_prefixes(stub)) == {"xai"}


async def test_risk_agent_uses_the_risk_critic_role(stub, registry_factory):
    registry_factory(role_provider={"risk_critic": "ollama"})
    from agents.risk_agent import RiskAgent

    context = MarketContext(
        query="Buy AAPL",
        ticker="AAPL",
        intent="analytical",
        price=PriceData(ticker="AAPL", current_price=150.0, validated=True),
        portfolio=PortfolioData(total_value=10000.0, cash_balance={"total": 5000.0}),
    )
    agent = RiskAgent()
    assert agent.model_name == kit.model_id_for("risk_critic")
    response = await agent.analyze({"context": context, "suggestion": "BUY 1 share of AAPL"})
    assert response.success, response.error
    assert response.data["status"] == "APPROVED"
    assert response.data["requires_hitl"] is True  # trade suggestions always need a human
    assert _models(stub) == [kit.model_id_for("risk_critic")]
    assert _prefixes(stub) == ["ollama"]


async def test_research_agent_uses_the_research_role(stub, registry_factory):
    registry_factory()
    from agents.research_agent import ResearchAgent

    params = await ResearchAgent()._generate_smart_query("AAPL")
    assert params["q"] == "stub query"
    assert _models(stub) == [kit.model_id_for("research")]
    assert _prefixes(stub) == ["ollama"]


async def test_math_agent_uses_the_math_codegen_role(stub, registry_factory):
    registry_factory()
    from agents.math_generator_agent import MathGeneratorAgent

    stub.reply_text = json.dumps({"script": "print(1)", "explanation": "stub"})
    agent = MathGeneratorAgent()
    response = await agent.analyze({"query": "simulate", "context_data": {"a": 1}, "required_stats": ["rsi"]})
    assert response.success, response.error
    assert response.data["script"] == "print(1)"
    assert _models(stub) == [kit.model_id_for("math_codegen")]
    assert _prefixes(stub) == ["lmstudio"]


async def test_math_agent_has_no_second_model_retry(stub, registry_factory):
    registry_factory()
    from agents.math_generator_agent import MathGeneratorAgent

    stub.reply_text = "this is not json"
    agent = MathGeneratorAgent()
    response = await agent.analyze({"query": "simulate", "context_data": {}, "required_stats": []})
    assert response.success  # the parse-failure placeholder, as before
    assert len(stub.snapshot()) == 1


async def test_swarm_uses_the_coordinator_role(stub, registry_factory):
    registry_factory()
    from agents.orchestrator import SwarmOrchestrator

    swarm = SwarmOrchestrator(reflex_timeout=0.05, synthesis_timeout=0.05)
    assert swarm.model_name == kit.model_id_for("coordinator")
    result = await swarm.execute_swarm_run("What now?")
    assert stub.reply_text in result.reflex_conclusion
    assert _models(stub) == [kit.model_id_for("coordinator")]
    assert _prefixes(stub) == ["lmstudio"]


async def test_swarm_without_coordinator_role_fails_closed(stub, registry_factory):
    registry_factory(roles=["decision"])
    from agents.orchestrator import SwarmOrchestrator

    with pytest.raises(ModelRoleMissing):
        SwarmOrchestrator()
    assert stub.count == 0


# --- AC-11: capabilities come from fields, not names -----------------------------------------


def _agent_with_decision(registry_factory, model, **fields):
    registry = registry_factory(role_extra={"decision": {"model": model, **fields}})
    agent = DecisionAgent(mcp_client=MagicMock())
    agent._resolved = registry.resolve("decision")
    return agent


async def _decide_and_capture_prompt(agent, images):
    captured = {}

    async def fake_loop(system, prompt, context, previous_response_id=None, images=None):
        captured["prompt"] = prompt
        return {"content": "ok", "response_id": None}

    agent._initialized = True
    agent.llm = MagicMock()
    context = MarketContext(query="hi", intent="conversational")
    with patch.object(agent, "_run_agentic_loop", side_effect=fake_loop), patch.object(
        agent, "_inject_context_layers", side_effect=lambda p, q: p
    ), patch("agents.decision_agent.log_audit"):
        await agent.make_decision(context, "hi", images=images)
    return captured["prompt"]


async def test_image_prefix_field_prefixes_any_model(registry_factory):
    agent = _agent_with_decision(registry_factory, "plain-model", image_prefix="<|image|>")
    prompt = await _decide_and_capture_prompt(agent, ["aGVsbG8="])
    assert prompt.startswith("<|image|>\n")


async def test_gemma_named_model_without_the_field_gets_no_prefix(registry_factory):
    agent = _agent_with_decision(registry_factory, "gemma-4-x")
    prompt = await _decide_and_capture_prompt(agent, ["aGVsbG8="])
    assert "<|image|>" not in prompt


async def test_no_prefix_without_images_even_when_the_field_is_set(registry_factory):
    agent = _agent_with_decision(registry_factory, "plain-model", image_prefix="<|image|>")
    prompt = await _decide_and_capture_prompt(agent, None)
    assert "<|image|>" not in prompt


LONG_SKILLS = "S" * 2000


def _skills_prompt(agent):
    with patch("utils.skill_loader.get_skill_loader") as loader, patch(
        "app_context.state._rag_manager", None
    ):
        loader.return_value.get_relevant_skills.return_value = LONG_SKILLS
        return agent._inject_context_layers("BASE", "query")


async def test_compact_prompt_true_trims_skills_for_any_model_string(registry_factory):
    agent = _agent_with_decision(registry_factory, "huge-model-70b", compact_prompt=True)
    out = _skills_prompt(agent)
    assert LONG_SKILLS not in out
    assert "S" * 500 + "..." in out
    assert "(Nano)" in agent._get_system_persona("analytical")


async def test_compact_prompt_false_never_trims_even_for_tiny_names(registry_factory):
    agent = _agent_with_decision(registry_factory, "nano-mobile-phi-tiny", compact_prompt=False)
    out = _skills_prompt(agent)
    assert LONG_SKILLS in out
    assert "(Nano)" not in agent._get_system_persona("analytical")


# --- AC-14 (agent half): the audit actor names the model ------------------------------------


async def test_decision_audit_actor_and_details_carry_model_and_fingerprint(registry_factory):
    registry = registry_factory()
    agent = DecisionAgent(mcp_client=MagicMock())
    agent._resolved = registry.resolve("decision")
    agent._initialized = True
    agent.llm = MagicMock()
    context = MarketContext(query="hi", intent="conversational")
    with patch.object(
        agent, "_run_agentic_loop", new=AsyncMock(return_value={"content": "ok", "response_id": None})
    ), patch.object(agent, "_inject_context_layers", side_effect=lambda p, q: p), patch(
        "agents.decision_agent.log_audit"
    ) as audit:
        await agent.make_decision(context, "hi")
    kwargs = audit.call_args.kwargs
    assert kwargs["actor"] == f"DecisionAgent::{kit.model_id_for('decision')}"
    assert kwargs["details"]["model"] == kit.model_id_for("decision")
    assert kwargs["details"]["registry_fingerprint"] == registry.fingerprint


# --- AC-12: the forecaster role ---------------------------------------------------------------


class _FakeStream:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeStdin:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(json.loads(data.decode()))

    async def drain(self):
        return None


class _FakeProcess:
    def __init__(self):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(
            [
                b'{"status": "pong"}\n',
                b'{"status": "success", "success": true, "forecast": []}\n',
            ]
        )
        self.stderr = _FakeStream([])
        self.returncode = None

    async def wait(self):
        return 0

    def kill(self):
        return None


async def _client_with_fake_process():
    from utils.worker_client import WorkerClient

    process = _FakeProcess()
    spawn = AsyncMock(return_value=process)
    client = WorkerClient()
    return client, process, spawn


async def test_forecast_request_carries_the_role_model_and_revision(registry_factory):
    registry_factory()
    client, process, spawn = await _client_with_fake_process()
    with patch("asyncio.create_subprocess_exec", spawn):
        await client.forecast_fused(ohlcv_data=[{"c": 1.0}], prediction_steps=4, timeframe="1Day")
    spawn.assert_awaited_once()
    ping, forecast = process.stdin.written
    assert ping == {"action": "ping"}
    assert forecast["action"] == "forecast_fused"
    assert forecast["model"] == "stub-org/stub-forecaster"
    assert forecast["revision"] == "stub-revision-1"


async def test_load_ttm_carries_the_role_model_and_revision(registry_factory):
    registry_factory()
    client, process, spawn = await _client_with_fake_process()
    with patch("asyncio.create_subprocess_exec", spawn):
        assert await client.load_ttm_model() is True
    _ping, load = process.stdin.written
    assert load == {
        "action": "load_ttm",
        "model": "stub-org/stub-forecaster",
        "revision": "stub-revision-1",
    }


async def test_missing_forecaster_role_launches_no_worker_and_loads_nothing(registry_factory):
    registry_factory(include_forecaster=False)
    client, process, spawn = await _client_with_fake_process()
    with patch("asyncio.create_subprocess_exec", spawn):
        with pytest.raises(ModelRoleMissing) as load_err:
            await client.load_ttm_model()
        with pytest.raises(ModelRoleMissing) as forecast_err:
            await client.forecast_fused(ohlcv_data=[], prediction_steps=1)
    assert load_err.value.field == "forecaster"
    assert forecast_err.value.code == "ROLE_MISSING"
    spawn.assert_not_awaited()
    assert process.stdin.written == []


async def test_forecaster_surfaces_a_missing_role_instead_of_a_fallback(registry_factory):
    registry_factory(include_forecaster=False)
    from forecaster import TTMForecaster

    forecaster = TTMForecaster.__new__(TTMForecaster)
    forecaster.ttm_available = True
    with patch("utils.worker_client.WorkerClient._ensure_worker", new=AsyncMock()) as ensure:
        with pytest.raises(ModelRoleMissing):
            await forecaster._ttm_forecast([{"c": 1.0, "t": 0, "v": 1}] * 600, 4, "1Day")
    ensure.assert_not_awaited()


async def test_bridge_validator_rejects_missing_or_bad_models():
    sys.path.insert(0, str(BACKEND))
    from forecast_bridge import validate_forecast_request

    for bad in ({}, {"model": ""}, {"model": "  "}, {"model": 7}, {"model": None}):
        with pytest.raises(ValueError, match="MODEL_REQUIRED"):
            validate_forecast_request(bad)
    with pytest.raises(ValueError, match="REVISION_INVALID"):
        validate_forecast_request({"model": "m", "revision": ""})
    assert validate_forecast_request({"model": "m", "revision": "r1"}) == {"model": "m", "revision": "r1"}
    assert validate_forecast_request({"model": "m"}) == {"model": "m", "revision": None}


async def test_bridge_validator_imports_no_model_library():
    code = (
        "import sys; sys.path.insert(0, 'backend'); import forecast_bridge\n"
        "bad = [m for m in ('transformers', 'tsfm_public', 'torch') if m in sys.modules]\n"
        "print('LOADED:' + ','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-1000:]
    assert result.stdout.strip().endswith("LOADED:")


async def test_bridge_stdin_without_a_model_is_rejected_before_any_load():
    request = json.dumps({"ohlcv_data": [], "prediction_steps": 4})
    result = subprocess.run(
        [sys.executable, str(BACKEND / "forecast_bridge.py")],
        input=request,
        capture_output=True,
        text=True,
        timeout=120,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["success"] is False
    assert "MODEL_REQUIRED" in payload["error"]


async def test_worker_rejects_requests_without_a_model():
    sys.path.insert(0, str(BACKEND))
    from utils.worker_service import ModelWorker

    worker = ModelWorker()
    assert worker.handle_request({"action": "load_ttm"})["status"] == "error"
    assert "MODEL_REQUIRED" in worker.handle_request({"action": "load_ttm"})["error"]
    forecast = worker.handle_request({"action": "forecast_ttm", "ohlcv_data": []})
    assert forecast["success"] is False
    assert "MODEL_REQUIRED" in forecast["error"]

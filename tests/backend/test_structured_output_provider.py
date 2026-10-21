"""67-FU1 F1 and F3: structured output through ``response_format``, and MODEL_MISMATCH.

``run_structured`` is the one call every structured answer (critic verdict, tool
call extraction, news query) goes through. These tests run it against the loopback
stub: what the request carries, what makes a reply unusable, and that no failure
is retried or answered by another model.
"""

import json

import httpx
import pytest
from langchain_core.messages import HumanMessage

import model_registry_testkit as kit
from agents.decision_agent import ToolCallList, extract_tool_calls
from agents.research_agent import NewsDataQueryParams, ResearchAgent
from agents.risk_agent import RiskAssessment
from model_registry import (
    ModelRegistryUnavailable,
    ModelRoleMissing,
    ProviderError,
    set_active_registry,
)
from model_registry.provider import build_chat_model, check_served_model, run_structured
from model_registry_testkit import (  # noqa: F401  (fixtures)
    key_env,
    network_guard,
    registry_factory,
    stub,
)

pytestmark = pytest.mark.asyncio

MESSAGES = [{"role": "system", "content": "rules"}, {"role": "user", "content": "audit this"}]

VALID_VERDICT = {
    "status": "BLOCKED",
    "confidence_score": 0.9,
    "risk_assessment": "over the cap",
    "compliance_notes": "n/a",
    "recommendation_adjustment": "halve it",
    "debate_refutation": "the thesis ignores size",
    "requires_hitl": True,
}


@pytest.fixture(autouse=True)
def _no_real_network(network_guard):
    return network_guard


# --- F1: the request ------------------------------------------------------------------------


async def test_request_carries_the_json_schema_response_format_and_no_tool_choice(stub, registry_factory):
    registry_factory()
    stub.structured["RiskAssessment"] = VALID_VERDICT
    result = await run_structured("risk_critic", MESSAGES, RiskAssessment)

    assert result == RiskAssessment.model_validate(VALID_VERDICT)
    [request] = stub.snapshot()
    body = request.json
    assert body["response_format"]["type"] == "json_schema"
    spec = body["response_format"]["json_schema"]
    assert spec["name"] == "RiskAssessment"
    assert spec["schema"] == RiskAssessment.model_json_schema()
    # A forced tool call is what LM Studio rejects; none may be sent.
    assert "tool_choice" not in body
    assert "tools" not in body
    assert body["messages"] == MESSAGES
    assert body["model"] == kit.model_id_for("risk_critic")


async def test_registry_sampling_fields_reach_the_structured_request(stub, registry_factory):
    registry_factory(role_extra={"risk_critic": {"temperature": 0.25, "max_tokens": 777}})
    await run_structured("risk_critic", MESSAGES, RiskAssessment)
    [request] = stub.snapshot()
    assert request.json["temperature"] == 0.25
    assert request.json["max_tokens"] == 777


@pytest.mark.parametrize("provider_id", ["xai", "lmstudio", "ollama"])
async def test_structured_request_goes_to_the_role_endpoint_for_any_provider(
    stub, registry_factory, key_env, provider_id
):
    registry_factory(role_provider={"risk_critic": provider_id})
    stub.structured["RiskAssessment"] = VALID_VERDICT
    await run_structured("risk_critic", MESSAGES, RiskAssessment)
    [request] = stub.snapshot()
    assert request.prefix == kit.STUB_PREFIXES[provider_id]
    auth = request.headers.get("authorization")
    assert (auth == f"Bearer {key_env}") if provider_id == "xai" else (auth is None)


async def test_a_missing_role_raises_before_any_request(stub, registry_factory):
    registry_factory(roles=["coordinator", "decision", "research", "math_codegen"])
    with pytest.raises(ModelRoleMissing) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.field == "risk_critic"
    assert stub.count == 0


async def test_no_registry_raises_before_any_request(stub):
    set_active_registry(None)
    with pytest.raises(ModelRegistryUnavailable):
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert stub.count == 0


async def test_a_named_but_unset_key_fails_before_any_request(stub, registry_factory, monkeypatch):
    registry_factory()
    monkeypatch.delenv(kit.XAI_KEY_ENV)
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "KEY_MISSING"
    assert stub.count == 0


# --- F1: a reply that is not a strict RiskAssessment fails closed ---------------------------------

_FENCED = "```json\n" + json.dumps(VALID_VERDICT) + "\n```"
_PROSE = "Here is my verdict: " + json.dumps(VALID_VERDICT)
_BAD_REPLIES = {
    "fenced_json": _FENCED,
    "prose_then_json": _PROSE,
    "not_json": "I would block this trade.",
    "empty": "",
    "unknown_status": {**VALID_VERDICT, "status": "NOT_A_VERDICT"},
    "missing_field": {k: v for k, v in VALID_VERDICT.items() if k != "debate_refutation"},
    "string_confidence": {**VALID_VERDICT, "confidence_score": "0.9"},
    "out_of_range_confidence": {**VALID_VERDICT, "confidence_score": 1.5},
    "json_array": [VALID_VERDICT],
    "lowercase_status": {**VALID_VERDICT, "status": "blocked"},
}


@pytest.mark.parametrize("name", sorted(_BAD_REPLIES))
async def test_a_non_conforming_reply_is_structured_output_invalid_and_not_retried(
    stub, registry_factory, name
):
    registry_factory()
    stub.structured["RiskAssessment"] = _BAD_REPLIES[name]
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "STRUCTURED_OUTPUT_INVALID"
    assert caught.value.role == "risk_critic"
    # The model's text never reaches the error.
    assert "BLOCKED" not in str(caught.value) and "verdict" not in str(caught.value)
    # One request: no retry, no second model, no other provider.
    assert stub.count == 1
    assert {r.model for r in stub.snapshot()} == {kit.model_id_for("risk_critic")}


async def test_nothing_lenient_rescues_fenced_json(stub, registry_factory):
    """The fenced text above is a valid verdict once the fence is stripped. It must still fail."""
    registry_factory()
    stub.structured["RiskAssessment"] = _FENCED
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "STRUCTURED_OUTPUT_INVALID"
    # The unfenced text is a valid verdict, so a lenient parser would have accepted it.
    assert RiskAssessment.model_validate_json(json.dumps(VALID_VERDICT), strict=True).status == "BLOCKED"


# --- error mapping parity with run_magentic -------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "PROVIDER_AUTH_FAILED"),
        (404, "PROVIDER_MODEL_NOT_FOUND"),
        (429, "PROVIDER_RATE_LIMITED"),
        (500, "PROVIDER_SERVER_ERROR"),
        (400, "PROVIDER_REQUEST_REJECTED"),
    ],
)
async def test_http_errors_map_like_every_other_call_path(stub, registry_factory, monkeypatch, status, expected):
    registry_factory(base_urls=kit.dead_provider_urls("xai"))
    import openai

    calls = []

    class _Failing:
        def __init__(self, *args, **kwargs):
            self.chat = self
            self.completions = self

        async def create(self, **kwargs):
            calls.append(kwargs)
            request = httpx.Request("POST", "http://127.0.0.1:9/xai/v1/chat/completions")
            raise openai.APIStatusError(
                "boom-body", response=httpx.Response(status, request=request), body=None
            )

        async def close(self):
            return None

    monkeypatch.setattr(openai, "AsyncOpenAI", _Failing)
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == expected
    assert "boom-body" not in str(caught.value)
    assert len(calls) == 1
    assert stub.count == 0


async def test_the_client_is_built_from_the_role_with_retries_off(stub, registry_factory, monkeypatch):
    registry = registry_factory(role_provider={"risk_critic": "lmstudio"})
    resolved = registry.resolve("risk_critic")
    import openai

    built = []

    class _Recording(openai.AsyncOpenAI):
        def __init__(self, *args, **kwargs):
            built.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(openai, "AsyncOpenAI", _Recording)
    stub.structured["RiskAssessment"] = VALID_VERDICT
    await run_structured("risk_critic", MESSAGES, RiskAssessment)
    [kwargs] = built
    assert kwargs["max_retries"] == 0
    assert kwargs["base_url"] == resolved.base_url
    assert kwargs["timeout"] == resolved.timeout_s
    assert kwargs["api_key"] == ""  # keyless provider: never falls back to OPENAI_API_KEY


async def test_connection_refused_maps_to_unreachable(stub, registry_factory):
    registry_factory(base_urls=kit.dead_provider_urls("xai"))
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "PROVIDER_UNREACHABLE"
    assert stub.count == 0


# --- F3: MODEL_MISMATCH ---------------------------------------------------------------------------


async def test_a_reply_from_another_model_is_a_mismatch_and_is_never_used(stub, registry_factory):
    registry_factory()
    # The reply is a perfectly valid APPROVED verdict, from the wrong model.
    stub.structured["RiskAssessment"] = {**VALID_VERDICT, "status": "APPROVED"}
    stub.served_model = "some-other-loaded-model"
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "MODEL_MISMATCH"
    assert caught.value.role == "risk_critic"
    assert "some-other-loaded-model" not in str(caught.value)
    assert stub.count == 1


@pytest.mark.parametrize(
    "served",
    [
        "STUB-RISK-CRITIC-MODEL",  # case differs
        kit.model_id_for("risk_critic") + " ",  # trailing space
        "org/" + kit.model_id_for("risk_critic"),  # extra prefix
        kit.model_id_for("risk_critic")[:-1],  # truncated
        "",
    ],
)
async def test_the_comparison_is_exact(stub, registry_factory, served):
    registry_factory()
    stub.served_model = served
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "MODEL_MISMATCH"


async def test_the_same_model_id_is_accepted(stub, registry_factory):
    registry_factory()
    stub.structured["RiskAssessment"] = VALID_VERDICT
    stub.served_model = kit.model_id_for("risk_critic")
    assert (await run_structured("risk_critic", MESSAGES, RiskAssessment)).status == "BLOCKED"


@pytest.mark.parametrize("served", [None, 5, ["x"], b"x"])
async def test_a_reply_that_names_no_usable_model_is_a_mismatch(served):
    with pytest.raises(ProviderError) as caught:
        check_served_model("risk_critic", "wanted", served)
    assert caught.value.code == "MODEL_MISMATCH"


async def test_the_langchain_invoke_path_checks_the_served_model(stub, registry_factory):
    registry = registry_factory()
    chat = build_chat_model(registry.resolve("decision"))
    stub.served_model = "some-other-loaded-model"
    with pytest.raises(ProviderError) as caught:
        await chat.ainvoke([HumanMessage(content="hi")])
    assert caught.value.code == "MODEL_MISMATCH"
    assert caught.value.role == "decision"


async def test_the_langchain_stream_path_checks_the_served_model(stub, registry_factory):
    registry = registry_factory()
    chat = build_chat_model(registry.resolve("decision"))
    stub.served_model = "some-other-loaded-model"
    with pytest.raises(ProviderError) as caught:
        async for _ in chat.astream([HumanMessage(content="hi")]):
            pass
    assert caught.value.code == "MODEL_MISMATCH"


async def test_the_langchain_paths_still_work_for_the_requested_model(stub, registry_factory):
    registry = registry_factory()
    chat = build_chat_model(registry.resolve("decision"))
    assert (await chat.ainvoke([HumanMessage(content="hi")])).content == stub.reply_text
    pieces = [c.content async for c in chat.astream([HumanMessage(content="hi")])]
    assert "".join(pieces) == stub.reply_text


# --- decision and research use the same pattern ---------------------------------------------------


async def test_extract_tool_calls_sends_the_tool_call_list_schema(stub, registry_factory):
    registry_factory()
    stub.structured["ToolCallList"] = {
        "tool_calls": [{"tool_name": "get_quote", "arguments": {"ticker": "INFY"}, "conviction_level": 7}]
    }
    calls = await extract_tool_calls("[TOOL:get_quote({\"ticker\": \"INFY\"})]", "")
    assert [c.tool_name for c in calls] == ["get_quote"]
    assert calls[0].arguments == {"ticker": "INFY"}
    [request] = stub.snapshot()
    assert request.model == kit.model_id_for("decision")
    assert request.json["response_format"]["json_schema"]["schema"] == ToolCallList.model_json_schema()
    assert "tool_choice" not in request.json and "tools" not in request.json


async def test_extract_tool_calls_fails_closed_on_non_conforming_output(stub, registry_factory):
    registry_factory()
    stub.structured["ToolCallList"] = "[TOOL:get_quote(INFY)]"
    with pytest.raises(ProviderError) as caught:
        await extract_tool_calls("text", "")
    assert caught.value.code == "STRUCTURED_OUTPUT_INVALID"


async def test_extract_tool_calls_refuses_a_mismatched_model(stub, registry_factory):
    registry_factory()
    stub.served_model = "some-other-loaded-model"
    with pytest.raises(ProviderError) as caught:
        await extract_tool_calls("text", "")
    assert caught.value.code == "MODEL_MISMATCH"


async def test_research_query_sends_the_news_query_schema(stub, registry_factory):
    registry_factory()
    params = await ResearchAgent()._generate_smart_query("AAPL")
    assert params["q"] == "stub query"
    [request] = stub.snapshot()
    assert request.model == kit.model_id_for("research")
    assert request.json["response_format"]["json_schema"]["schema"] == NewsDataQueryParams.model_json_schema()
    assert "tool_choice" not in request.json and "tools" not in request.json


async def test_research_query_with_non_conforming_output_still_degrades_to_the_basic_query(
    stub, registry_factory
):
    registry_factory()
    stub.structured["NewsDataQueryParams"] = "q=AAPL"
    params = await ResearchAgent()._generate_smart_query("AAPL")
    assert params["q"] == "AAPL stock market news"
    assert stub.count == 1


async def test_research_query_never_degrades_on_a_mismatched_model(stub, registry_factory):
    registry_factory()
    stub.served_model = "some-other-loaded-model"
    with pytest.raises(ProviderError) as caught:
        await ResearchAgent()._generate_smart_query("AAPL")
    assert caught.value.code == "MODEL_MISMATCH"


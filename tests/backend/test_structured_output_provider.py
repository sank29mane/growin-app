"""67-FU1 F1 and F3, 67-FU2: structured output with the schema in the prompt, and MODEL_MISMATCH.

``run_structured`` is the one call every structured answer (critic verdict, tool
call extraction, news query) goes through. These tests run it against the loopback
stub: what the request carries (no ``response_format``, no tools, the wire schema
appended to the last user message), what makes a reply unusable (wrong model,
``finish_reason`` other than ``stop``, no reasoning evidence for the critic, text
that is not a strict verdict), and that no failure is retried or answered by
another model.
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
from model_registry.provider import _SCHEMA_INSTRUCTION, build_chat_model, check_served_model, run_structured
from model_registry.wire_schema import wire_spec
from model_stub_server import embedded_schema, last_user_text, strict_subset_violations
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


async def test_request_has_no_response_format_no_tools_and_the_schema_in_the_last_user_message(stub, registry_factory):
    registry_factory()
    stub.structured["RiskAssessment"] = VALID_VERDICT
    original = json.dumps(MESSAGES)
    result = await run_structured("risk_critic", MESSAGES, RiskAssessment)

    assert result == RiskAssessment.model_validate(VALID_VERDICT)
    [request] = stub.snapshot()
    body = request.json
    # json_schema constrained decoding is what kills reasoning on LM Studio; none may be sent.
    assert "response_format" not in body
    # A forced tool call is what LM Studio rejects; none may be sent.
    assert "tool_choice" not in body
    assert "tools" not in body
    schema_text = json.dumps(wire_spec(RiskAssessment).schema)
    assert body["messages"][0] == MESSAGES[0]
    assert body["messages"][1] == {
        "role": "user",
        "content": "audit this\n\n" + _SCHEMA_INSTRUCTION + schema_text,
    }
    assert last_user_text(body).endswith(schema_text)
    assert embedded_schema(body) == wire_spec(RiskAssessment).schema
    assert strict_subset_violations(embedded_schema(body)) == []
    assert "single JSON object matching this JSON schema and nothing else" in _SCHEMA_INSTRUCTION
    assert body["model"] == kit.model_id_for("risk_critic")
    # The caller's list is not edited in place.
    assert json.dumps(MESSAGES) == original


async def test_the_instruction_goes_on_the_last_user_message_only(stub, registry_factory):
    registry_factory()
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "tail"},
    ]
    await run_structured("risk_critic", messages, RiskAssessment)
    [request] = stub.snapshot()
    sent = request.json["messages"]
    assert [m["role"] for m in sent] == [m["role"] for m in messages]
    assert sent[1]["content"] == "first" and sent[4]["content"] == "tail"
    assert _SCHEMA_INSTRUCTION in sent[3]["content"] and sent[3]["content"].startswith("second")
    assert sum(_SCHEMA_INSTRUCTION in m["content"] for m in sent) == 1


async def test_a_list_content_user_message_gets_a_text_part(stub, registry_factory):
    registry_factory()
    parts = [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
    await run_structured("decision", [{"role": "user", "content": parts}], ToolCallList)
    [request] = stub.snapshot()
    sent = request.json["messages"][0]["content"]
    assert sent[:2] == parts
    assert sent[2]["type"] == "text" and _SCHEMA_INSTRUCTION in sent[2]["text"]
    assert embedded_schema(request.json) == wire_spec(ToolCallList).schema


async def test_messages_with_no_user_message_are_refused_before_any_request(stub, registry_factory):
    registry_factory()
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", [{"role": "system", "content": "rules"}], RiskAssessment)
    assert caught.value.code == "PROVIDER_REQUEST_REJECTED"
    assert stub.count == 0


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

_VERDICT_JSON = json.dumps(VALID_VERDICT)
_FENCED = "```json\n" + _VERDICT_JSON + "\n```"
_PROSE = "Here is my verdict: " + json.dumps(VALID_VERDICT)
_BAD_REPLIES = {
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


# --- 67-FU2: the one fence rule ---------------------------------------------------------------------

_FENCE_REJECTED = {
    "prose_then_fence": "Here you go:\n" + _FENCED,
    "fence_then_prose": _FENCED + "\nHope that helps.",
    "two_fences": _FENCED + "\n" + _FENCED,
    "two_blocks_one_wrapper": "```json\n" + _VERDICT_JSON + "\n```\n```json\n" + _VERDICT_JSON + "\n```",
    "uppercase_tag": "```JSON\n" + _VERDICT_JSON + "\n```",
    "mixed_case_tag": "```Json\n" + _VERDICT_JSON + "\n```",
    "no_tag": "```\n" + _VERDICT_JSON + "\n```",
    "other_tag": "```javascript\n" + _VERDICT_JSON + "\n```",
    "missing_newline_after_tag": "```json" + _VERDICT_JSON + "\n```",
    "space_instead_of_newline_after_tag": "```json " + _VERDICT_JSON + "\n```",
    "missing_newline_before_close": "```json\n" + _VERDICT_JSON + "```",
    "trailing_newline": _FENCED + "\n",
    "trailing_space": _FENCED + " ",
    "leading_newline": "\n" + _FENCED,
    "leading_space": " " + _FENCED,
    "crlf": "```json\r\n" + _VERDICT_JSON + "\r\n```",
    "tilde_fence": "~~~json\n" + _VERDICT_JSON + "\n~~~",
    "four_backticks": "````json\n" + _VERDICT_JSON + "\n````",
    "unclosed": "```json\n" + _VERDICT_JSON,
    "open_only_close_only": _VERDICT_JSON + "\n```",
    "empty_block": "```json\n```",
    "empty_inner": "```json\n\n```",
    "inner_is_not_json": "```json\nBLOCKED\n```",
    "inner_is_a_nested_fence": "```json\n```json\n" + _VERDICT_JSON + "\n```\n```",
    # One wrapper, valid JSON, but a second fence marker inside a string: not "a single
    # block", refused conservatively (this is the only input the single-block check decides).
    "fence_marker_inside_a_string": "```json\n"
    + json.dumps({**VALID_VERDICT, "risk_assessment": "see ```json\n{}\n``` here"})
    + "\n```",
    "inner_is_prose_plus_json": "```json\nverdict: " + _VERDICT_JSON + "\n```",
}


@pytest.mark.parametrize("name", sorted(_FENCE_REJECTED))
async def test_the_fence_rule_accepts_only_the_exact_anchored_form(stub, registry_factory, name):
    """Each near miss is a valid verdict wearing the wrong wrapper. All of them still fail closed."""
    registry_factory()
    stub.structured["RiskAssessment"] = _FENCE_REJECTED[name]
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "STRUCTURED_OUTPUT_INVALID"
    assert stub.count == 1


async def test_one_whole_content_json_fence_is_accepted(stub, registry_factory):
    registry_factory()
    stub.structured["RiskAssessment"] = _FENCED
    result = await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert result == RiskAssessment.model_validate(VALID_VERDICT)
    assert stub.count == 1


async def test_the_fenced_inner_text_is_still_strictly_validated(stub, registry_factory):
    """The fence only removes the wrapper: an invalid verdict inside it fails like an unfenced one."""
    registry_factory()
    for inner in (
        {**VALID_VERDICT, "status": "NOT_A_VERDICT"},
        {**VALID_VERDICT, "confidence_score": "0.9"},
        {k: v for k, v in VALID_VERDICT.items() if k != "debate_refutation"},
        {**VALID_VERDICT, "extra": 1},
    ):
        stub.structured["RiskAssessment"] = "```json\n" + json.dumps(inner) + "\n```"
        with pytest.raises(ProviderError) as caught:
            await run_structured("risk_critic", MESSAGES, RiskAssessment)
        assert caught.value.code == "STRUCTURED_OUTPUT_INVALID"


async def test_a_fenced_reply_gets_every_other_check(stub, registry_factory):
    """Fence acceptance does not bypass finish_reason, the served model or the reasoning guard."""
    registry_factory()
    stub.structured["RiskAssessment"] = _FENCED
    stub.finish_reason = "length"
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "STRUCTURED_OUTPUT_INVALID"
    stub.finish_reason = "stop"
    stub.reasoning_tokens = 0
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "REASONING_ABSENT"
    stub.reasoning_tokens = 5
    stub.served_model = "some-other-loaded-model"
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "MODEL_MISMATCH"


async def test_decision_and_research_accept_the_fenced_form(stub, registry_factory):
    registry_factory()
    stub.structured["ToolCallList"] = "```json\n" + json.dumps({"tool_calls": []}) + "\n```"
    assert await extract_tool_calls("text", "") == []
    query = {"q": "AAPL earnings", "qInTitle": None, "country": None, "category": None, "domain": None}
    stub.structured["NewsDataQueryParams"] = "```json\n" + json.dumps(query) + "\n```"
    assert (await ResearchAgent()._generate_smart_query("AAPL"))["q"] == "AAPL earnings"


async def test_research_still_degrades_to_the_basic_query_on_a_near_miss_fence(stub, registry_factory):
    registry_factory()
    stub.structured["NewsDataQueryParams"] = "```JSON\n" + json.dumps({"q": "x"}) + "\n```"
    assert (await ResearchAgent()._generate_smart_query("AAPL"))["q"] == "AAPL stock market news"


# --- 67-FU2: finish_reason must be "stop" ----------------------------------------------------------


@pytest.mark.parametrize("finish", ["length", "content_filter", "tool_calls", "function_call", ""])
async def test_a_reply_that_did_not_stop_cleanly_fails_closed_even_if_it_parses(stub, registry_factory, finish):
    """A complete, valid verdict with finish_reason other than stop is never used."""
    registry_factory()
    stub.structured["RiskAssessment"] = VALID_VERDICT
    stub.finish_reason = finish
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "STRUCTURED_OUTPUT_INVALID"
    assert caught.value.role == "risk_critic"
    assert stub.count == 1


async def test_finish_length_fails_closed_for_every_role_that_uses_it(stub, registry_factory):
    registry_factory()
    stub.finish_reason = "length"
    for role, schema in (("decision", ToolCallList), ("research", NewsDataQueryParams)):
        with pytest.raises(ProviderError) as caught:
            await run_structured(role, MESSAGES, schema)
        assert caught.value.code == "STRUCTURED_OUTPUT_INVALID"


_GARBAGE = [
    "",
    "   ",
    "00.0, 00.0, 00.0, 00.0",
    "...??...\xa0json...",
    "{",
    "null",
    "[]",
    '{"status": "BLOCKED"',
    "\x00\x01\x02",
]


@pytest.mark.parametrize("text", _GARBAGE, ids=[f"garbage{i}" for i in range(len(_GARBAGE))])
async def test_empty_or_garbage_content_fails_closed(stub, registry_factory, text):
    registry_factory()
    stub.structured["RiskAssessment"] = text
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "STRUCTURED_OUTPUT_INVALID"
    assert stub.count == 1


async def test_the_error_never_carries_the_model_text(stub, registry_factory):
    registry_factory()
    stub.structured["RiskAssessment"] = "CANARY-model-text " + json.dumps(VALID_VERDICT)
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert "CANARY" not in str(caught.value) and "CANARY" not in repr(caught.value.args)


# --- 67-FU2 D3: the risk critic must show reasoning evidence ----------------------------------------


@pytest.mark.parametrize(
    "tokens,text",
    [(None, None), (0, None), (0, ""), (0, "   \n"), (None, "")],
    ids=["no_field", "zero_tokens", "zero_tokens_empty_text", "zero_tokens_blank_text", "no_field_empty_text"],
)
async def test_a_critic_reply_with_no_reasoning_evidence_fails_closed(stub, registry_factory, tokens, text):
    registry_factory()
    # A valid APPROVED verdict: the no-think false approve the guard exists for.
    stub.structured["RiskAssessment"] = {**VALID_VERDICT, "status": "APPROVED"}
    stub.reasoning_tokens = tokens
    stub.reasoning_content = text
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "REASONING_ABSENT"
    assert caught.value.role == "risk_critic"
    assert stub.count == 1


@pytest.mark.parametrize(
    "wire",
    ["7", True, 1.0, -1, None, "", [7], {"n": 7}],
    ids=["string_7", "bool_true", "float_1_0", "negative", "null", "empty_string", "list", "object"],
)
async def test_only_a_raw_positive_json_integer_counts_as_reasoning_tokens(stub, registry_factory, wire):
    """The SDK coerces "7", true and 1.0 into ints; the guard reads the wire value instead."""
    registry_factory()
    stub.structured["RiskAssessment"] = {**VALID_VERDICT, "status": "APPROVED"}
    stub.reasoning_tokens_raw = wire
    stub.reasoning_content = ""
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "REASONING_ABSENT"
    assert stub.count == 1


async def test_a_raw_integer_passes_and_a_missing_field_is_no_evidence(stub, registry_factory):
    registry_factory()
    stub.structured["RiskAssessment"] = VALID_VERDICT
    stub.reasoning_tokens_raw = 7
    assert (await run_structured("risk_critic", MESSAGES, RiskAssessment)).status == "BLOCKED"
    stub.reasoning_tokens_raw = 0
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "REASONING_ABSENT"


@pytest.mark.parametrize("wire", ["7", True, 1.0, -1, None])
async def test_a_coerced_token_count_is_still_rescued_by_real_reasoning_text(stub, registry_factory, wire):
    registry_factory()
    stub.structured["RiskAssessment"] = VALID_VERDICT
    stub.reasoning_tokens_raw = wire
    stub.reasoning_content = "weighed the cap"
    assert (await run_structured("risk_critic", MESSAGES, RiskAssessment)).status == "BLOCKED"


@pytest.mark.parametrize("wire", ["7", True, 1.0, -1, None])
async def test_other_roles_ignore_the_token_count_entirely(stub, registry_factory, wire):
    registry_factory()
    stub.reasoning_tokens_raw = wire
    assert isinstance(await run_structured("decision", MESSAGES, ToolCallList), ToolCallList)


@pytest.mark.parametrize(
    "tokens,field,text",
    [
        (5, "reasoning_content", None),
        (1, "reasoning_content", None),
        (0, "reasoning_content", "weighed the cap"),
        (None, "reasoning_content", "weighed the cap"),
        (0, "reasoning", "weighed the cap"),
        (None, "reasoning", "weighed the cap"),
    ],
)
async def test_a_critic_reply_with_reasoning_evidence_passes(stub, registry_factory, tokens, field, text):
    registry_factory()
    stub.structured["RiskAssessment"] = VALID_VERDICT
    stub.reasoning_tokens = tokens
    stub.reasoning_content = text
    stub.reasoning_field = field
    assert (await run_structured("risk_critic", MESSAGES, RiskAssessment)).status == "BLOCKED"


@pytest.mark.parametrize("role,schema", [("decision", ToolCallList), ("research", NewsDataQueryParams), ("coordinator", ToolCallList), ("math_codegen", ToolCallList)])
async def test_other_roles_do_not_need_reasoning_evidence(stub, registry_factory, role, schema):
    registry_factory()
    stub.reasoning_tokens = None
    assert isinstance(await run_structured(role, MESSAGES, schema), schema)
    stub.reasoning_tokens = 0
    assert isinstance(await run_structured(role, MESSAGES, schema), schema)


async def test_decision_and_research_callers_pass_without_reasoning_evidence(stub, registry_factory):
    registry_factory()
    stub.reasoning_tokens = None
    assert await extract_tool_calls("text", "") == []
    assert (await ResearchAgent()._generate_smart_query("AAPL"))["q"] == "stub query"


async def test_the_model_mismatch_check_still_runs_before_the_finish_and_reasoning_checks(stub, registry_factory):
    registry_factory()
    stub.structured["RiskAssessment"] = VALID_VERDICT
    stub.served_model = "some-other-loaded-model"
    stub.finish_reason = "length"
    stub.reasoning_tokens = None
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "MODEL_MISMATCH"


# --- error mapping -------------------------------------------------------


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
            self.with_raw_response = self

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


async def test_a_stream_that_never_names_its_model_is_a_mismatch(stub, registry_factory):
    """No chunk carries a model name: the stream must end in MODEL_MISMATCH, not as a verified reply."""
    registry = registry_factory()
    chat = build_chat_model(registry.resolve("decision"))
    stub.omit_model = True
    finished = False
    with pytest.raises(ProviderError) as caught:
        async for _ in chat.astream([HumanMessage(content="hi")]):
            pass
        finished = True
    assert not finished
    assert caught.value.code == "MODEL_MISMATCH"
    assert caught.value.role == "decision"
    assert stub.count == 1


async def test_run_structured_refuses_a_completion_with_no_model_name(stub, registry_factory):
    registry_factory()
    stub.structured["RiskAssessment"] = VALID_VERDICT
    stub.omit_model = True
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
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
        "tool_calls": [
            {
                "tool_name": "get_quote",
                "arguments_json": json.dumps({"ticker": "INFY"}),
                "conviction_level": 7,
                "is_high_conviction": None,
            }
        ]
    }
    calls = await extract_tool_calls("[TOOL:get_quote({\"ticker\": \"INFY\"})]", "")
    assert [c.tool_name for c in calls] == ["get_quote"]
    assert calls[0].arguments == {"ticker": "INFY"}
    assert calls[0].conviction_level == 7
    assert calls[0].is_high_conviction is False  # null on the wire: the model default
    [request] = stub.snapshot()
    assert request.model == kit.model_id_for("decision")
    assert embedded_schema(request.json) == wire_spec(ToolCallList).schema
    assert "response_format" not in request.json
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
    assert embedded_schema(request.json) == wire_spec(NewsDataQueryParams).schema
    assert "response_format" not in request.json
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


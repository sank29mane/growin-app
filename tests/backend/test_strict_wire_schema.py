"""67-FU1 round 2: the schema on the wire is inside OpenAI's strict subset.

``run_structured`` sends ``strict: true``. A raw Pydantic schema is outside the
strict subset (no ``additionalProperties: false``, defaulted fields not required,
open objects), and a server that enforces it answers 400 on every critic, decision
and research call. These tests hold the builder (``wire_spec``) to an independent
checker (``strict_subset_violations`` in the stub server), prove the stub rejects
the raw schemas, and pin how a reply is decoded: strictly, with ``null`` meaning
"the default" and nothing else defaulted.
"""

import json
from typing import Any, Dict, List, Optional

import httpx
import pytest
from pydantic import BaseModel, Field

import model_registry_testkit as kit
from agents.decision_agent import ToolCall, ToolCallList, extract_tool_calls
from agents.research_agent import NewsDataQueryParams
from agents.risk_agent import RiskAssessment
from model_registry import ProviderError
from model_registry import provider as provider_module
from model_registry.provider import run_structured
from model_registry.wire_schema import WireReplyError, WireSchemaError, WireSpec, wire_spec
from model_registry_testkit import (  # noqa: F401  (fixtures)
    key_env,
    network_guard,
    registry_factory,
    stub,
)
from model_stub_server import strict_subset_violations

SCHEMAS = [RiskAssessment, ToolCallList, NewsDataQueryParams]
ROLE_FOR = {RiskAssessment: "risk_critic", ToolCallList: "decision", NewsDataQueryParams: "research"}
MESSAGES = [{"role": "user", "content": "go"}]

VERDICT = {
    "status": "BLOCKED",
    "confidence_score": 0.9,
    "risk_assessment": "over the cap",
    "compliance_notes": "n/a",
    "recommendation_adjustment": "halve it",
    "debate_refutation": "ignores size",
    "requires_hitl": True,
}


@pytest.fixture(autouse=True)
def _no_real_network(network_guard):
    return network_guard


# --- the checker itself: it fires on each strict-mode violation ------------------------------


def _ok() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
        "additionalProperties": False,
    }


def _without(key: str) -> Dict[str, Any]:
    schema = _ok()
    del schema[key]
    return schema


@pytest.mark.parametrize(
    "bad",
    [
        _without("additionalProperties"),
        {**_ok(), "additionalProperties": True},
        {**_ok(), "required": []},
        {**_ok(), "properties": {"a": {"type": "string"}, "b": {"type": "string"}}},
        {"type": "object", "additionalProperties": False},  # open object
        {**_ok(), "properties": {"a": {"type": "object", "additionalProperties": True}}},
        {**_ok(), "properties": {"a": {"type": "string", "default": "x"}}},
        {**_ok(), "properties": {"a": {"type": "integer", "minimum": 1}}},
        {**_ok(), "properties": {"a": {"$ref": "#/$defs/Missing"}}},
        {"type": "array", "items": {"type": "string"}},  # root is not an object
        {**_ok(), "properties": {"a": {"type": "array"}}},
        {
            **_ok(),
            "properties": {"a": {"type": "object", "properties": {"b": {"type": "string"}}, "required": ["b"]}},
        },
    ],
)
def test_the_checker_flags_each_strict_mode_violation(bad):
    assert strict_subset_violations(bad)


def test_the_checker_accepts_a_strict_schema():
    assert strict_subset_violations(_ok()) == []


@pytest.mark.parametrize("schema", SCHEMAS, ids=lambda s: s.__name__)
def test_the_raw_pydantic_schema_is_outside_the_strict_subset(schema):
    """The regression: this is what the first version of run_structured sent."""
    assert strict_subset_violations(schema.model_json_schema())


# --- the builder: every schema in use is inside the subset -----------------------------------


@pytest.mark.parametrize("schema", SCHEMAS, ids=lambda s: s.__name__)
def test_the_wire_schema_passes_the_strict_checker(schema):
    assert strict_subset_violations(wire_spec(schema).schema) == []


def _objects(node: Any) -> List[Dict[str, Any]]:
    found = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found += _objects(value)
    elif isinstance(node, list):
        for value in node:
            found += _objects(value)
    return found


@pytest.mark.parametrize("schema", SCHEMAS, ids=lambda s: s.__name__)
def test_every_wire_object_is_closed_and_fully_required(schema):
    objects = _objects(wire_spec(schema).schema)
    assert objects
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])


def test_the_risk_assessment_wire_schema():
    wire = wire_spec(RiskAssessment).schema
    assert list(wire["properties"]) == list(RiskAssessment.model_fields)
    assert wire["required"] == list(RiskAssessment.model_fields)
    assert wire["properties"]["status"]["enum"] == ["APPROVED", "FLAGGED", "BLOCKED"]
    # The one defaulted field is nullable; the six required fields are not.
    assert wire["properties"]["requires_hitl"]["anyOf"] == [{"type": "boolean"}, {"type": "null"}]
    for name in set(RiskAssessment.model_fields) - {"requires_hitl"}:
        assert "anyOf" not in wire["properties"][name]
    # The bound moved out of the wire schema; the real model still enforces it.
    assert "minimum" not in json.dumps(wire) and "maximum" not in json.dumps(wire)
    with pytest.raises(Exception):
        RiskAssessment.model_validate({**VERDICT, "confidence_score": 1.5}, strict=True)


def test_tool_call_arguments_go_on_the_wire_as_a_json_string():
    item = wire_spec(ToolCallList).schema["properties"]["tool_calls"]["anyOf"][0]["items"]
    assert "arguments" not in item["properties"]
    assert item["properties"]["arguments_json"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert "arguments_json" in item["required"]


# --- the builder: shapes it cannot express fail loudly ---------------------------------------


def test_an_open_object_without_the_opt_in_is_refused():
    class Loose(BaseModel):
        payload: Dict[str, Any]

    with pytest.raises(WireSchemaError):
        wire_spec(Loose)


def test_a_keyword_outside_the_strict_subset_is_refused_not_dropped():
    class Patterned(BaseModel):
        code: str = Field(..., pattern="^[A-Z]+$")

    with pytest.raises(WireSchemaError):
        wire_spec(Patterned)


def test_nested_models_are_inlined_and_closed():
    class Leg(BaseModel):
        symbol: str
        qty: int = Field(default=1, ge=1)

    class Order(BaseModel):
        legs: List[Leg]
        note: Optional[str] = None
        first: Optional[Leg] = None

    spec = wire_spec(Order)
    assert "$defs" not in json.dumps(spec.schema) and "$ref" not in json.dumps(spec.schema)
    assert strict_subset_violations(spec.schema) == []
    native = spec.to_native_json(
        json.dumps({"legs": [{"symbol": "X", "qty": None}], "note": None, "first": None})
    )
    assert Order.model_validate_json(native, strict=True) == Order(legs=[Leg(symbol="X")])


# --- the decoder: null means the default, nothing else is defaulted --------------------------


def test_null_means_the_model_default_for_each_defaulted_field():
    reply = {**VERDICT, "requires_hitl": None}
    result = RiskAssessment.model_validate_json(wire_spec(RiskAssessment).to_native_json(json.dumps(reply)), strict=True)
    assert result.requires_hitl is False

    wire = {
        "tool_calls": [
            {"tool_name": "t", "arguments_json": None, "conviction_level": None, "is_high_conviction": None}
        ]
    }
    [call] = ToolCallList.model_validate_json(
        wire_spec(ToolCallList).to_native_json(json.dumps(wire)), strict=True
    ).tool_calls
    assert call == ToolCall(tool_name="t")
    assert (call.arguments, call.conviction_level, call.is_high_conviction) == ({}, 5, False)

    empty = ToolCallList.model_validate_json(
        wire_spec(ToolCallList).to_native_json(json.dumps({"tool_calls": None})), strict=True
    )
    assert empty.tool_calls == []


def test_a_null_for_a_required_field_is_not_defaulted():
    spec = wire_spec(RiskAssessment)
    native = spec.to_native_json(json.dumps({**VERDICT, "risk_assessment": None}))
    with pytest.raises(Exception):
        RiskAssessment.model_validate_json(native, strict=True)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: {k: v for k, v in r.items() if k != "requires_hitl"},  # a defaulted key may not be left out
        lambda r: {k: v for k, v in r.items() if k != "status"},
        lambda r: {**r, "extra": 1},
        lambda r: [r],
    ],
    ids=["missing_defaulted_key", "missing_required_key", "extra_key", "array_root"],
)
def test_the_reply_must_carry_exactly_the_wire_properties(mutate):
    with pytest.raises(WireReplyError):
        wire_spec(RiskAssessment).to_native_json(json.dumps(mutate(VERDICT)))


@pytest.mark.parametrize(
    "text",
    ["", "not json", "```json\n{}\n```", '{"status": "BLOCKED", "status": "APPROVED"}', '{"a": NaN}'],
)
def test_the_reply_text_must_be_strict_json(text):
    with pytest.raises(WireReplyError):
        wire_spec(RiskAssessment).to_native_json(text)


def _call(arguments_json: Any) -> str:
    return json.dumps(
        {
            "tool_calls": [
                {"tool_name": "t", "arguments_json": arguments_json, "conviction_level": 5, "is_high_conviction": False}
            ]
        }
    )


def test_arguments_json_must_hold_one_json_object():
    spec = wire_spec(ToolCallList)
    ok = ToolCallList.model_validate_json(spec.to_native_json(_call('{"ticker": "INFY", "n": [1, 2]}')), strict=True)
    assert ok.tool_calls[0].arguments == {"ticker": "INFY", "n": [1, 2]}
    for bad in ["[1]", '"x"', "5", "null", "true", "not json", "", '{"a": 1} trailing', '{"a": NaN}', '{"a": 1, "a": 2}', 5, {"a": 1}, ["a"]]:
        with pytest.raises(WireReplyError):
            spec.to_native_json(_call(bad))


def test_the_old_open_arguments_key_is_not_accepted():
    wire = {"tool_calls": [{"tool_name": "t", "arguments": {"a": 1}, "conviction_level": 5, "is_high_conviction": False}]}
    with pytest.raises(WireReplyError):
        wire_spec(ToolCallList).to_native_json(json.dumps(wire))


# --- end to end through the strict-enforcing stub --------------------------------------------


@pytest.mark.parametrize("schema", SCHEMAS, ids=lambda s: s.__name__)
@pytest.mark.asyncio
async def test_the_strict_stub_accepts_what_run_structured_sends(stub, registry_factory, schema):
    registry_factory()
    result = await run_structured(ROLE_FOR[schema], MESSAGES, schema)
    assert isinstance(result, schema)
    [request] = stub.snapshot()
    spec = request.json["response_format"]["json_schema"]
    assert spec["strict"] is True
    assert strict_subset_violations(spec["schema"]) == []


@pytest.mark.parametrize("schema", SCHEMAS, ids=lambda s: s.__name__)
@pytest.mark.asyncio
async def test_the_strict_stub_rejects_the_raw_schema_with_400(stub, registry_factory, schema):
    registry = registry_factory()
    resolved = registry.resolve(ROLE_FOR[schema])
    body = {
        "model": resolved.model,
        "messages": MESSAGES,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "strict": True, "schema": schema.model_json_schema()},
        },
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{resolved.base_url}/chat/completions", json=body)
    assert response.status_code == 400
    assert "additionalProperties" in response.text


@pytest.mark.asyncio
async def test_run_structured_fails_against_a_strict_server_if_it_sends_the_raw_schema(
    stub, registry_factory, monkeypatch
):
    """Break-proof for the call site: swap the builder for the raw schema and the strict server refuses."""
    registry_factory()
    real = wire_spec(RiskAssessment)
    monkeypatch.setattr(
        provider_module, "wire_spec", lambda model: WireSpec(schema=model.model_json_schema(), _decode=real._decode)
    )
    with pytest.raises(ProviderError) as caught:
        await run_structured("risk_critic", MESSAGES, RiskAssessment)
    assert caught.value.code == "PROVIDER_REQUEST_REJECTED"


@pytest.mark.asyncio
async def test_a_non_strict_request_is_not_checked_by_the_stub(stub, registry_factory):
    """The stub only enforces what the request asks for: strict is the flag that triggers it."""
    registry = registry_factory()
    resolved = registry.resolve("risk_critic")
    body = {
        "model": resolved.model,
        "messages": MESSAGES,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "RiskAssessment", "schema": RiskAssessment.model_json_schema()},
        },
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{resolved.base_url}/chat/completions", json=body)
    assert response.status_code == 200


# --- decision semantics stay the same --------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_tool_calls_returns_the_same_objects_as_before(stub, registry_factory):
    registry_factory()
    stub.structured["ToolCallList"] = {
        "tool_calls": [
            {
                "tool_name": "place_order",
                "arguments_json": json.dumps({"symbol": "INFY", "qty": 3}),
                "conviction_level": 9,
                "is_high_conviction": True,
            },
            {"tool_name": "get_quote", "arguments_json": None, "conviction_level": None, "is_high_conviction": None},
        ]
    }
    calls = await extract_tool_calls("c", "r")
    assert calls == [
        ToolCall(tool_name="place_order", arguments={"symbol": "INFY", "qty": 3}, conviction_level=9, is_high_conviction=True),
        ToolCall(tool_name="get_quote"),
    ]
    assert all(isinstance(c.arguments, dict) for c in calls)


@pytest.mark.asyncio
async def test_extract_tool_calls_with_no_calls_is_an_empty_list(stub, registry_factory):
    registry_factory()
    stub.structured["ToolCallList"] = {"tool_calls": []}
    assert await extract_tool_calls("c", "r") == []
    stub.structured["ToolCallList"] = {"tool_calls": None}
    assert await extract_tool_calls("c", "r") == []


@pytest.mark.parametrize("bad", ["[1]", '"x"', "null", "oops"])
@pytest.mark.asyncio
async def test_extract_tool_calls_with_non_object_arguments_is_structured_output_invalid(stub, registry_factory, bad):
    registry_factory()
    stub.structured["ToolCallList"] = {
        "tool_calls": [{"tool_name": "t", "arguments_json": bad, "conviction_level": 5, "is_high_conviction": False}]
    }
    with pytest.raises(ProviderError) as caught:
        await extract_tool_calls("c", "r")
    assert caught.value.code == "STRUCTURED_OUTPUT_INVALID"
    assert stub.count == 1


@pytest.mark.parametrize("level", [0, 11, True, "5"])
@pytest.mark.asyncio
async def test_a_conviction_outside_the_model_bounds_still_fails_after_the_wire_check(stub, registry_factory, level):
    registry_factory()
    stub.structured["ToolCallList"] = {
        "tool_calls": [{"tool_name": "t", "arguments_json": "{}", "conviction_level": level, "is_high_conviction": False}]
    }
    with pytest.raises(ProviderError) as caught:
        await extract_tool_calls("c", "r")
    assert caught.value.code == "STRUCTURED_OUTPUT_INVALID"

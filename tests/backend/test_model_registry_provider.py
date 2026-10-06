"""AC-4 to AC-7: one OpenAI-compatible provider, no real network, typed errors,
and no ambient environment redirect.

All traffic goes to a loopback stub server or an injected httpx MockTransport.
Key values are generated per test.
"""

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import HumanMessage
from magentic import prompt as mag_prompt

import model_registry_testkit as kit
from model_registry import ModelRegistryError, ProviderError, load_registry_file
from model_registry.provider import (
    build_chat_model,
    chat_model_for_role,
    role_magentic_model,
    run_magentic,
)
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
    """AC-5: refuse DNS and connections to anything but loopback."""

    return network_guard


@mag_prompt("Say hello to {name}.")
def greet(name: str) -> str: ...


# --- AC-5 ---------------------------------------------------------------------------


async def test_guard_is_live_probe_to_a_real_host_is_refused(tmp_path, key_env, network_guard):
    document = kit.registry_document("http://127.0.0.1:9")
    document["providers"]["xai"]["base_url"] = "https://api.x.ai/v1"
    registry = load_registry_file(kit.write_registry(tmp_path, document))
    model = build_chat_model(registry.resolve("decision"))
    with pytest.raises(ProviderError) as caught:
        await model.ainvoke([HumanMessage(content="hi")])
    assert caught.value.code == "PROVIDER_UNREACHABLE"
    # The guard, not a real network, ended the call.
    assert any("api.x.ai" in host for host in network_guard.blocked)


async def test_guard_allows_loopback(stub, registry_factory):
    registry = registry_factory()
    model = build_chat_model(registry.resolve("coordinator"))
    reply = await model.ainvoke([HumanMessage(content="hi")])
    assert reply.content == stub.reply_text
    assert stub.count == 1


# --- AC-4 ---------------------------------------------------------------------------

PROVIDER_PREFIXES = ["xai", "lmstudio", "ollama"]


@pytest.mark.parametrize("provider_id", PROVIDER_PREFIXES)
async def test_one_provider_kind_serves_each_endpoint(stub, registry_factory, key_env, provider_id):
    registry = registry_factory(role_provider={"decision": provider_id})
    resolved = registry.resolve("decision")
    assert resolved.kind == "openai_compatible"
    assert resolved.provider_id == provider_id
    expected_model = kit.model_id_for("decision")
    prefix = kit.STUB_PREFIXES[provider_id]

    chat = build_chat_model(resolved)
    reply = await chat.ainvoke([HumanMessage(content="invoke")])
    assert reply.content == stub.reply_text

    pieces = []
    async for piece in chat.astream([HumanMessage(content="stream")]):
        pieces.append(piece.content)
    assert "".join(pieces) == stub.reply_text

    with role_magentic_model(resolved):
        assert isinstance(greet("world"), str)

    sent = stub.snapshot()
    assert len(sent) == 3
    for request in sent:
        assert request.prefix == prefix
        assert request.path == f"/{prefix}/v1/chat/completions"
        assert request.model == expected_model
    assert [bool(r.json.get("stream")) for r in sent] == [False, True, True]

    for request in sent:
        auth = request.headers.get("authorization")
        if provider_id == "xai":
            assert auth == f"Bearer {key_env}"
        else:
            assert auth is None


async def test_client_class_is_identical_across_providers(registry_factory):
    registry = registry_factory(role_provider={"decision": "xai", "coordinator": "lmstudio", "research": "ollama"})
    chats = [build_chat_model(registry.resolve(r)) for r in ("decision", "coordinator", "research")]
    assert len({type(c) for c in chats}) == 1
    assert len({type(c.inner) for c in chats}) == 1
    magentics = [role_magentic_model(registry.resolve(r)) for r in ("decision", "coordinator", "research")]
    assert len({type(m) for m in magentics}) == 1


async def test_run_magentic_reaches_the_role_endpoint(stub, registry_factory, key_env):
    registry_factory()
    result = await run_magentic("research", greet, "world")
    assert isinstance(result, str)
    [request] = stub.snapshot()
    assert request.prefix == "ollama"
    assert request.model == kit.model_id_for("research")
    assert request.headers.get("authorization") is None


async def test_registry_sampling_fields_reach_the_request(stub, registry_factory):
    registry = registry_factory(
        role_extra={"decision": {"temperature": 0.25, "max_tokens": 321, "top_p": 0.9}}
    )
    chat = chat_model_for_role("decision")
    await chat.ainvoke([HumanMessage(content="hi")])
    [request] = stub.snapshot()
    assert request.json["temperature"] == 0.25
    assert request.json["max_tokens"] == 321
    assert "max_completion_tokens" not in request.json
    assert request.json["top_p"] == 0.9
    assert registry.resolve("decision").max_tokens == 321


async def test_named_but_unset_key_fails_before_any_request(stub, registry_factory, monkeypatch):
    registry = registry_factory()
    monkeypatch.delenv(kit.XAI_KEY_ENV)
    with pytest.raises(ProviderError) as caught:
        build_chat_model(registry.resolve("decision"))
    assert caught.value.code == "KEY_MISSING"
    assert stub.count == 0


# --- AC-6 ---------------------------------------------------------------------------


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json_error(status: int, code: str):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.calls.append(request)
        return httpx.Response(status, json={"error": {"message": "boom-body", "code": code}})

    handler.calls = []
    return handler


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "PROVIDER_AUTH_FAILED"),
        (404, "PROVIDER_MODEL_NOT_FOUND"),
        (500, "PROVIDER_SERVER_ERROR"),
        (429, "PROVIDER_RATE_LIMITED"),
        (400, "PROVIDER_REQUEST_REJECTED"),
    ],
)
async def test_http_errors_map_to_typed_provider_errors(stub, registry_factory, status, expected):
    registry = registry_factory()
    handler = _json_error(status, "x")
    chat = build_chat_model(registry.resolve("decision"), http_client=_mock_client(handler))
    with pytest.raises(ProviderError) as caught:
        await chat.ainvoke([HumanMessage(content="hi")])
    assert caught.value.code == expected
    assert caught.value.role == "decision"
    assert "boom-body" not in str(caught.value)
    # No retry, and nothing reached any other provider.
    assert len(handler.calls) == 1
    assert stub.count == 0


async def test_connection_refused_maps_to_unreachable(stub, registry_factory):
    registry = registry_factory()
    calls = []

    def refuse(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    chat = build_chat_model(registry.resolve("coordinator"), http_client=_mock_client(refuse))
    with pytest.raises(ProviderError) as caught:
        await chat.ainvoke([HumanMessage(content="hi")])
    assert caught.value.code == "PROVIDER_UNREACHABLE"
    assert len(calls) == 1
    assert stub.count == 0


async def test_timeout_maps_to_timeout_code(stub, registry_factory):
    registry = registry_factory()

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    chat = build_chat_model(registry.resolve("coordinator"), http_client=_mock_client(slow))
    with pytest.raises(ProviderError) as caught:
        await chat.ainvoke([HumanMessage(content="hi")])
    assert caught.value.code == "PROVIDER_TIMEOUT"


async def test_stream_errors_are_typed_too(stub, registry_factory):
    registry = registry_factory()
    handler = _json_error(500, "x")
    chat = build_chat_model(registry.resolve("decision"), http_client=_mock_client(handler))
    with pytest.raises(ProviderError) as caught:
        async for _ in chat.astream([HumanMessage(content="hi")]):
            pass
    assert caught.value.code == "PROVIDER_SERVER_ERROR"
    assert len(handler.calls) == 1
    assert stub.count == 0


async def test_magentic_failure_is_typed_and_not_retried_elsewhere(stub, key_env, tmp_path):
    # Point the research provider at a closed loopback port. The call must fail
    # with a typed error and must not reach the live stub through any fallback.
    import model_registry

    document = kit.registry_document("http://127.0.0.1:9")
    dead = load_registry_file(kit.write_registry(tmp_path / "dead", document))
    model_registry.set_active_registry(dead)
    try:
        with pytest.raises(ProviderError) as caught:
            await run_magentic("research", greet, "world")
    finally:
        model_registry.set_active_registry(None)
    assert caught.value.code == "PROVIDER_UNREACHABLE"
    assert stub.count == 0


# --- AC-7 ---------------------------------------------------------------------------


@pytest.fixture
def decoy_env(monkeypatch):
    decoy_key = "decoy-" + secrets.token_hex(8)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_API_BASE", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_API_KEY", decoy_key)
    monkeypatch.setenv("MAGENTIC_OPENAI_MODEL", "decoy-model")
    monkeypatch.setenv("MAGENTIC_OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("MAGENTIC_OPENAI_API_KEY", decoy_key)
    return decoy_key


@pytest.mark.parametrize("role,provider_id", [("decision", "xai"), ("coordinator", "lmstudio")])
async def test_ambient_env_cannot_redirect_langchain_path(
    stub, registry_factory, key_env, decoy_env, role, provider_id
):
    registry_factory()
    chat = chat_model_for_role(role)
    await chat.ainvoke([HumanMessage(content="hi")])
    [request] = stub.snapshot()
    assert request.prefix == provider_id
    assert request.model == kit.model_id_for(role)
    auth = request.headers.get("authorization")
    if provider_id == "xai":
        assert auth == f"Bearer {key_env}"
    else:
        # Keyless provider: the decoy OPENAI_API_KEY is never sent.
        assert auth is None
    assert decoy_env not in json.dumps(request.headers)


@pytest.mark.parametrize("role,provider_id", [("decision", "xai"), ("research", "ollama")])
async def test_ambient_env_cannot_redirect_magentic_path(
    stub, registry_factory, key_env, decoy_env, role, provider_id
):
    registry_factory()
    await run_magentic(role, greet, "world")
    [request] = stub.snapshot()
    assert request.prefix == provider_id
    assert request.model == kit.model_id_for(role)
    auth = request.headers.get("authorization")
    if provider_id == "xai":
        assert auth == f"Bearer {key_env}"
    else:
        assert auth is None


async def test_app_context_import_leaves_openai_env_unset():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "MAGENTIC_"))}
    env["PYTHONPATH"] = os.pathsep.join([str(BACKEND), str(REPO_ROOT)])
    code = (
        "import os, app_context\n"
        "bad = [k for k in ('OPENAI_BASE_URL', 'OPENAI_API_KEY') if k in os.environ]\n"
        "assert not bad, bad\n"
        "print('ENV_CLEAN')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "ENV_CLEAN" in result.stdout


async def test_error_types_are_registry_errors_for_unavailable_registry():
    from model_registry import ModelRegistryUnavailable, get_active_registry, set_active_registry

    set_active_registry(None)
    with pytest.raises(ModelRegistryUnavailable) as caught:
        get_active_registry()
    assert caught.value.code == "MODEL_REGISTRY_UNAVAILABLE"
    assert isinstance(caught.value, ModelRegistryError)

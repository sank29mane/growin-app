"""AC-3 (route half), AC-13, AC-14 (route half) and AC-15.

Routes run through FastAPI. The stub server is the only endpoint any registry
points at, so a request that escaped to a default or fallback model would show
up in its request log.
"""

import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import model_registry_testkit as kit
from app_context import AppState, ChatMessage, state
from model_registry import set_active_registry
from model_registry_testkit import (  # noqa: F401  (fixtures)
    key_env,
    network_guard,
    registry_factory,
    stub,
)
from server import app


@pytest.fixture(autouse=True)
def _no_real_network(network_guard):
    return network_guard


@pytest.fixture(autouse=True)
def _reset_registry():
    set_active_registry(None)
    yield
    set_active_registry(None)


@pytest.fixture
def client():
    # No ``with``: the lifespan stays off, so the registry the test sets is the one used.
    return TestClient(app)


# --- AC-3: no registry -> 503 MODEL_REGISTRY_UNAVAILABLE, nothing sent ---------------------


def test_chat_message_without_a_registry_is_503(client, stub):
    response = client.post("/api/chat/message", json={"message": "hello"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MODEL_REGISTRY_UNAVAILABLE"
    assert stub.count == 0


def test_chat_message_sse_without_a_registry_is_503_before_streaming(client, stub):
    response = client.post(
        "/api/chat/message",
        json={"message": "hello"},
        headers={"accept": "text/event-stream"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MODEL_REGISTRY_UNAVAILABLE"
    assert stub.count == 0


def test_agent_analyze_without_a_registry_is_503(client, stub):
    response = client.post("/agent/analyze", json={"query": "Analyze AAPL"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MODEL_REGISTRY_UNAVAILABLE"
    assert stub.count == 0


@pytest.mark.parametrize("missing", ["coordinator", "decision"])
def test_chat_message_with_a_missing_chat_role_is_503_role_missing(client, stub, registry_factory, missing):
    roles = [r for r in ("coordinator", "decision", "research") if r != missing]
    registry_factory(roles=roles)
    response = client.post("/api/chat/message", json={"message": "hello"})
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "ROLE_MISSING", "role": missing}
    assert stub.count == 0


def test_agent_analyze_with_a_missing_role_is_503_role_missing(client, stub, registry_factory):
    registry_factory(roles=["decision"])
    response = client.post("/agent/analyze", json={"query": "Analyze AAPL"})
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "ROLE_MISSING", "role": "coordinator"}
    assert stub.count == 0


def test_invalid_registry_file_surfaces_its_code_on_the_lifespan_path(
    stub, tmp_path, monkeypatch
):
    private = tmp_path / "private_root"
    private.mkdir()
    (private / "models.json").write_text('{"schema_version": 2}', encoding="utf-8")
    monkeypatch.setenv("GROWIN_PRIVATE_DIR", str(private))
    with TestClient(app) as lifespan_client:
        response = lifespan_client.post("/api/chat/message", json={"message": "hello"})
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert detail == {"code": "MODEL_REGISTRY_UNAVAILABLE", "reason": "SCHEMA_VERSION"}
    assert stub.count == 0


# --- AC-13: request bodies and the roles endpoint ---------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"model_name": "anything"},
        {"coordinator_model": "anything"},
        {"api_keys": {"openai": "not-a-real-key"}},
    ],
)
def test_chat_message_rejects_model_selection_fields(client, registry_factory, extra):
    registry_factory()
    response = client.post("/api/chat/message", json={"message": "hello", **extra})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "extra",
    [
        {"model_name": "anything"},
        {"coordinator_model": "anything"},
        {"api_keys": {"openai": "not-a-real-key"}},
    ],
)
def test_agent_analyze_rejects_model_selection_fields(client, registry_factory, extra):
    registry_factory()
    response = client.post("/agent/analyze", json={"query": "Analyze AAPL", **extra})
    assert response.status_code == 422


def test_roles_endpoint_returns_only_non_secret_fields(client, registry_factory, key_env):
    registry = registry_factory()
    response = client.get("/api/models/roles")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"roles", "missing_roles"}
    assert body["missing_roles"] == []
    by_role = {item["role"]: item for item in body["roles"]}
    assert set(by_role) == set(registry.roles)
    for item in body["roles"]:
        assert set(item) == {"role", "provider", "kind", "model", "key_configured"}
    assert by_role["decision"]["provider"] == "xai"
    assert by_role["decision"]["kind"] == "openai_compatible"
    assert by_role["decision"]["model"] == kit.model_id_for("decision")
    assert by_role["decision"]["key_configured"] is True
    assert by_role["coordinator"]["key_configured"] is None  # keyless provider
    assert by_role["forecaster"]["kind"] == "hf_local"
    text = response.text
    assert key_env not in text
    assert kit.XAI_KEY_ENV not in text
    assert "127.0.0.1" not in text and "http" not in text


def test_roles_endpoint_reports_a_missing_key_and_missing_roles(client, registry_factory, monkeypatch):
    registry_factory(roles=["decision"])
    monkeypatch.delenv(kit.XAI_KEY_ENV)
    body = client.get("/api/models/roles").json()
    assert body["roles"][0]["role"] == "decision"
    assert body["roles"][0]["key_configured"] is False
    assert "coordinator" in body["missing_roles"] and "research" in body["missing_roles"]


def test_roles_endpoint_without_a_registry_is_503(client):
    response = client.get("/api/models/roles")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MODEL_REGISTRY_UNAVAILABLE"


def test_available_models_endpoint_is_gone(client, registry_factory):
    registry_factory()
    assert client.get("/api/models/available").status_code == 404


# --- AC-14 (route half): lineage on stored messages -------------------------------------------


@pytest.mark.asyncio
async def test_assistant_message_stores_decision_model_and_registry_fingerprint(registry_factory):
    from chat_manager import ChatManager
    from routes.chat_routes import chat_message

    registry = registry_factory()
    manager = ChatManager(db_path=":memory:")
    previous_manager, previous_mcp = state.chat_manager, state.mcp_client
    state.chat_manager = manager
    state.mcp_client = MagicMock()
    context = MagicMock()
    context.model_dump.return_value = {}
    try:
        with patch("agents.orchestrator_agent.OrchestratorAgent") as orchestrator, patch(
            "routes.chat_routes.update_conversation_title_if_needed", new=AsyncMock()
        ):
            orchestrator.return_value.run = AsyncMock(
                return_value={"content": "reply", "response_id": None, "context": context}
            )
            body = await chat_message(
                ChatMessage(message="hello"), accept="application/json"
            )
        history = manager.load_history(body["conversation_id"])
    finally:
        state.chat_manager, state.mcp_client = previous_manager, previous_mcp
        manager.close()

    assistant = [m for m in history if m["role"] == "assistant"][0]
    assert assistant["model_name"] == kit.model_id_for("decision")
    assert assistant["registry_fingerprint"] == registry.fingerprint
    assert body["model_name"] == kit.model_id_for("decision")
    assert "coordinator_model" not in body
    user = [m for m in history if m["role"] == "user"][0]
    assert user["registry_fingerprint"] is None


def test_chat_manager_adds_the_fingerprint_column_to_an_old_database(tmp_path):
    from chat_manager import ChatManager

    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL,"
        " content TEXT NOT NULL, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, tool_calls TEXT,"
        " agent_name TEXT, model_name TEXT)"
    )
    connection.commit()
    connection.close()
    manager = ChatManager(db_path=str(path))
    try:
        conversation = manager.create_conversation("t")
        manager.save_message(conversation, "assistant", "x", model_name="m", registry_fingerprint="f" * 64)
        assert manager.load_history(conversation)[0]["registry_fingerprint"] == "f" * 64
    finally:
        manager.close()


# --- AC-15: execution authority is independent of the registry ---------------------------------


@pytest.mark.parametrize("registry_state", ["absent", "invalid", "valid"])
def test_start_execution_does_not_read_or_wait_on_the_registry(
    tmp_path, private_config_dir, stub, key_env, registry_state
):
    if registry_state == "invalid":
        (private_config_dir / "models.json").write_text("{not json", encoding="utf-8")
        set_active_registry(None, "INVALID_JSON")
    elif registry_state == "valid":
        kit.activate(kit.make_registry(private_config_dir, stub.url))
    app_state = AppState()
    try:
        started = app_state.start_execution(
            tmp_path / "execution.sqlite3", workspace="uk", private_dir=private_config_dir
        )
        assert started is True
        assert app_state.execution_authority is True
    finally:
        app_state.close_execution()


def test_lifespan_with_a_broken_registry_still_acquires_execution_authority(
    monkeypatch, tmp_path, private_config_dir
):
    (private_config_dir / "models.json").write_text('{"schema_version": 1, "oops": 1}', encoding="utf-8")
    monkeypatch.setenv("GROWIN_EXECUTION_DB_PATH", str(tmp_path / "execution.sqlite3"))
    monkeypatch.setenv("GROWIN_WORKSPACE", "uk")
    monkeypatch.setenv("GROWIN_PRIVATE_DIR", str(private_config_dir))

    with TestClient(app) as lifespan_client:
        health = lifespan_client.get("/health").json()
        assert health["execution_authority"] is True
        assert state.model_registry is None
        assert state.model_registry_error == "FIELD_INVALID"
        assert lifespan_client.get("/api/models/roles").status_code == 503


def test_lifespan_with_a_valid_registry_and_no_workspace_keeps_execution_disabled(
    monkeypatch, tmp_path, private_config_dir, stub, key_env
):
    kit.write_registry(private_config_dir, kit.registry_document(stub.url))
    monkeypatch.setenv("GROWIN_EXECUTION_DB_PATH", str(tmp_path / "execution.sqlite3"))
    monkeypatch.delenv("GROWIN_WORKSPACE", raising=False)
    monkeypatch.setenv("GROWIN_PRIVATE_DIR", str(private_config_dir))

    with TestClient(app) as lifespan_client:
        health = lifespan_client.get("/health").json()
        assert health["execution_authority"] is False
        assert health["execution_mode"] == "disabled"
        assert state.model_registry is not None
        assert lifespan_client.get("/api/models/roles").status_code == 200
    assert not (tmp_path / "execution.sqlite3").exists()


def test_execution_modules_do_not_import_the_registry():
    import ast
    from pathlib import Path

    backend = Path(__file__).resolve().parents[2] / "backend"
    offenders = []
    for package in ("execution", "brokers", "simulation", "market_data", "costs", "private_config"):
        for path in (backend / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    names.append(node.module)
                if isinstance(node, ast.Import):
                    names.extend(alias.name for alias in node.names)
                if any(n.split(".")[0] == "model_registry" for n in names):
                    offenders.append(str(path.relative_to(backend)))
    assert offenders == []

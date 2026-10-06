"""CoordinatorAgent takes its LLM from the registry's coordinator role."""

import pytest

import model_registry_testkit as kit
from agents.coordinator_agent import CoordinatorAgent
from model_registry import ModelRegistryUnavailable, ModelRoleMissing, set_active_registry
from model_registry_testkit import offline_registry  # noqa: F401


class MockMCP:
    pass


def test_coordinator_binds_the_coordinator_role(offline_registry):
    agent = CoordinatorAgent(mcp_client=MockMCP())

    assert agent.llm.role == "coordinator"
    assert agent.llm.model_id == kit.model_id_for("coordinator")
    # Temperature and other sampling fields come from the registry, never a literal.
    assert agent.llm.resolved.temperature is None


def test_coordinator_sampling_comes_from_the_registry(tmp_path, monkeypatch):
    monkeypatch.setenv(kit.XAI_KEY_ENV, "stubkey-test")
    registry = kit.make_registry(
        tmp_path, "http://127.0.0.1:9", role_extra={"coordinator": {"temperature": 0.0}}
    )
    kit.activate(registry)
    try:
        agent = CoordinatorAgent(mcp_client=MockMCP())
        assert agent.llm.resolved.temperature == 0.0
        assert agent.llm.inner.temperature == 0.0
    finally:
        set_active_registry(None)


def test_coordinator_fails_closed_without_a_registry():
    set_active_registry(None)
    with pytest.raises(ModelRegistryUnavailable):
        CoordinatorAgent(mcp_client=MockMCP())


def test_coordinator_fails_closed_without_its_role(tmp_path, monkeypatch):
    monkeypatch.setenv(kit.XAI_KEY_ENV, "stubkey-test")
    kit.activate(kit.make_registry(tmp_path, "http://127.0.0.1:9", roles=["decision"]))
    try:
        with pytest.raises(ModelRoleMissing):
            CoordinatorAgent(mcp_client=MockMCP())
    finally:
        set_active_registry(None)


def test_injected_llm_is_used_as_is(offline_registry):
    sentinel = object()
    assert CoordinatorAgent(mcp_client=MockMCP(), llm=sentinel).llm is sentinel

"""Execution authority requires valid private config and a matching ledger (ISO-03, ISO-01, ISO-02).

Real AppState instances, real files, and the real server lifespan. Every failure
path must leave authority off, install a dispatcher-free service, never raise,
and (for config failures) never create a ledger file.
"""

import inspect
import json
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app_context import AppState, state
from execution import ExecutionDisabledError, ExecutionLedger
from server import app

BAD_VALUE = "sentinel-rejected-value-4242"


def _dump(path):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return list(connection.iterdump())
    finally:
        connection.close()


def _assert_disabled(app_state: AppState):
    assert app_state.execution_authority is False
    assert app_state.execution_service.execution_enabled is False
    assert app_state.workspace_config is None
    assert app_state._execution_ledger is None
    assert app_state.execution_startup_error


async def _assert_cannot_approve(app_state: AppState):
    with pytest.raises(ExecutionDisabledError):
        await app_state.execution_service.approve_signed(
            "p", "c" * 36, b"x", workspace="uk"
        )


@pytest.mark.asyncio
async def test_missing_private_dir_blocks_authority_and_never_touches_the_ledger(tmp_path):
    app_state = AppState()
    ledger_dir = tmp_path / "ledger"

    started = app_state.start_execution(
        ledger_dir / "execution.sqlite3",
        workspace="uk",
        private_dir=tmp_path / "missing",
    )

    assert started is False
    _assert_disabled(app_state)
    assert "PRIVATE_DIR_MISSING" in app_state.execution_startup_error
    assert not ledger_dir.exists()
    assert not list(tmp_path.glob("**/*.sqlite3*"))
    assert not list(tmp_path.glob("**/*.lock"))
    await _assert_cannot_approve(app_state)


def test_malformed_config_blocks_authority_without_echoing_the_value(
    tmp_path, private_config_dir
):
    broken = tmp_path / "broken"
    shutil.copytree(private_config_dir, broken)
    limits_path = broken / "india" / "limits.json"
    limits = json.loads(limits_path.read_text())
    limits["unexpected_key"] = BAD_VALUE
    limits_path.write_text(json.dumps(limits))
    app_state = AppState()
    db_path = tmp_path / "india-ledger" / "execution.sqlite3"

    started = app_state.start_execution(db_path, workspace="india", private_dir=broken)

    assert started is False
    _assert_disabled(app_state)
    assert "SCHEMA_INVALID" in app_state.execution_startup_error
    assert BAD_VALUE not in app_state.execution_startup_error
    assert not db_path.parent.exists()


@pytest.mark.parametrize("workspace", ["us", "", None, "UK", 7])
def test_unknown_workspace_never_raises_out_of_startup(
    tmp_path, private_config_dir, workspace
):
    app_state = AppState()

    started = app_state.start_execution(
        tmp_path / "execution.sqlite3", workspace=workspace, private_dir=private_config_dir
    )

    assert started is False
    _assert_disabled(app_state)
    assert not (tmp_path / "execution.sqlite3").exists()


def test_a_ledger_pinned_to_another_workspace_is_refused_and_left_untouched(
    tmp_path, private_config_dir
):
    india_path = tmp_path / "india.sqlite3"
    with ExecutionLedger(india_path, workspace="india"):
        pass
    before = _dump(india_path)
    app_state = AppState()

    started = app_state.start_execution(
        india_path, workspace="uk", private_dir=private_config_dir
    )

    assert started is False
    _assert_disabled(app_state)
    assert "WorkspaceMismatch" in app_state.execution_startup_error
    assert _dump(india_path) == before


def test_an_unpinned_ledger_is_refused(tmp_path, private_config_dir):
    legacy = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(legacy)
    connection.execute("CREATE TABLE order_intents (proposal_id TEXT)")
    connection.execute("PRAGMA user_version = 5")
    connection.commit()
    connection.close()
    app_state = AppState()

    started = app_state.start_execution(
        legacy, workspace="uk", private_dir=private_config_dir
    )

    assert started is False
    _assert_disabled(app_state)
    assert "LedgerUnpinned" in app_state.execution_startup_error


def test_valid_config_acquires_authority_and_keeps_the_config(
    tmp_path, private_config_dir
):
    app_state = AppState()
    try:
        started = app_state.start_execution(
            tmp_path / "execution.sqlite3", workspace="uk", private_dir=private_config_dir
        )

        assert started is True
        assert app_state.execution_authority is True
        assert app_state.execution_startup_error is None
        assert app_state.workspace_config is not None
        assert app_state.workspace_config.workspace == "uk"
    finally:
        app_state.close_execution()

    assert app_state.workspace_config is None
    assert app_state.execution_authority is False


def test_india_start_keeps_the_loaded_limits_for_later_phases(
    tmp_path, private_config_dir
):
    app_state = AppState()
    try:
        assert app_state.start_execution(
            tmp_path / "india.sqlite3", workspace="india", private_dir=private_config_dir
        )
        assert app_state.workspace_config.workspace == "india"
        assert app_state.workspace_config.limits is not None
    finally:
        app_state.close_execution()


def test_start_execution_has_no_defaults():
    parameters = inspect.signature(AppState.start_execution).parameters

    assert [
        name
        for name in ("db_path", "workspace", "private_dir")
        if parameters[name].default is not inspect.Parameter.empty
    ] == []
    with pytest.raises(TypeError):
        AppState().start_execution(None, workspace="uk")  # type: ignore[call-arg]


# --- the server lifespan ---


def test_lifespan_without_a_workspace_boots_with_authority_off(monkeypatch, tmp_path):
    monkeypatch.setenv("GROWIN_EXECUTION_DB_PATH", str(tmp_path / "execution.sqlite3"))
    monkeypatch.delenv("GROWIN_WORKSPACE", raising=False)
    monkeypatch.delenv("GROWIN_PRIVATE_DIR", raising=False)

    with TestClient(app) as client:
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["execution_authority"] is False
        assert response.json()["execution_mode"] == "disabled"

    assert not (tmp_path / "execution.sqlite3").exists()
    assert state.execution_authority is False


def test_lifespan_with_a_workspace_and_private_dir_acquires_authority(
    monkeypatch, tmp_path, private_config_dir
):
    monkeypatch.setenv("GROWIN_EXECUTION_DB_PATH", str(tmp_path / "execution.sqlite3"))
    monkeypatch.setenv("GROWIN_WORKSPACE", "uk")
    monkeypatch.setenv("GROWIN_PRIVATE_DIR", str(private_config_dir))

    with TestClient(app) as client:
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["execution_authority"] is True

    assert state.execution_authority is False


def test_lifespan_with_a_missing_private_dir_boots_with_authority_off(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("GROWIN_EXECUTION_DB_PATH", str(tmp_path / "ledger" / "execution.sqlite3"))
    monkeypatch.setenv("GROWIN_WORKSPACE", "uk")
    monkeypatch.setenv("GROWIN_PRIVATE_DIR", str(tmp_path / "missing"))

    with TestClient(app) as client:
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["execution_authority"] is False
        assert "PRIVATE_DIR_MISSING" in state.execution_startup_error

    assert not (tmp_path / "ledger").exists()

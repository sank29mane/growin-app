from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app_context import AppState, state
from server import app


def start_body():
    instrument = {
        "workspace": "india",
        "venue": "NSE",
        "segment": "CASH",
        "symbol": "RELIANCE",
        "currency": "INR",
    }
    now = datetime.now(timezone.utc).isoformat()
    common = {
        "source": "local-replay",
        "observed_at": now,
        "received_at": now,
    }
    return {
        "provider": "local-replay",
        "confirmation": "START_READ_ONLY_REPLAY",
        "instruments": [instrument],
        "events": [
            {
                **common,
                "instrument": dict(instrument),
                "kind": "quote",
                "bid": "99",
                "ask": "101",
                "sequence": 1,
            },
            {
                **common,
                "instrument": dict(instrument),
                "kind": "quote",
                "bid": "99.01",
                "ask": "101.01",
                "sequence": 2,
            },
            {
                **common,
                "instrument": dict(instrument),
                "kind": "quote",
                "bid": "99.02",
                "ask": "101.02",
                "sequence": 3,
            },
            {
                **common,
                "instrument": dict(instrument),
                "kind": "trade",
                "price": "100",
                "quantity": "1",
                "sequence": 4,
            },
        ],
    }


@pytest_asyncio.fixture(autouse=True)
async def isolated_market_session():
    original_session = state._market_data_session
    original_mcp = state._mcp_client
    mock_mcp = MagicMock()
    mock_mcp.call_tool = AsyncMock()
    state._market_data_session = None
    state._mcp_client = mock_mcp
    yield mock_mcp
    await state.close_market_data()
    state._market_data_session = original_session
    state._mcp_client = original_mcp


async def request(method, path, **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


async def remote_request(method, path, **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.0.2.10", 1234)),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


def test_app_state_construction_is_market_data_inert():
    fresh = AppState()
    assert fresh._market_data_session is None
    assert fresh.market_data_status()["state"] == "STOPPED"


@pytest.mark.asyncio
async def test_explicit_replay_start_snapshot_and_stop(isolated_market_session):
    initial = await request("GET", "/api/market-data/sessions/current")
    assert initial.json() == {
        "state": "STOPPED",
        "provider": None,
        "instruments": [],
        "read_only": True,
    }

    started = await request("POST", "/api/market-data/sessions", json=start_body())
    assert started.status_code == 201, started.text
    assert started.json()["state"] == "RUNNING"
    assert started.json()["provider"] == "local-replay"

    snapshot = await request("GET", "/api/market-data/snapshots/RELIANCE")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["instrument"]["workspace"] == "india"
    assert snapshot.json()["last_trade_price"] == "100"
    assert len(snapshot.json()["snapshot_id"]) == 64

    duplicate = await request("POST", "/api/market-data/sessions", json=start_body())
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "SESSION_STATE_CONFLICT"

    stopped = await request("DELETE", "/api/market-data/sessions/current")
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "STOPPED"
    isolated_market_session.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_request_requires_explicit_confirmation_and_scoped_events():
    body = start_body()
    body["confirmation"] = "yes"
    rejected = await request("POST", "/api/market-data/sessions", json=body)
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_session_control_is_loopback_only():
    rejected = await remote_request(
        "POST",
        "/api/market-data/sessions",
        json=start_body(),
    )
    assert rejected.status_code == 403
    assert rejected.json()["detail"]["code"] == "LOCAL_ACCESS_REQUIRED"
    assert state.market_data_status()["state"] == "STOPPED"

    status = await remote_request("GET", "/api/market-data/sessions/current")
    assert status.status_code == 403
    snapshot = await remote_request("GET", "/api/market-data/snapshots/RELIANCE")
    assert snapshot.status_code == 403


@pytest.mark.asyncio
async def test_replay_rejects_unknown_url_path_and_credential_fields():
    for field, value in (
        ("url", "https://example.invalid/feed"),
        ("path", "/tmp/feed.jsonl"),
        ("api_key", "secret"),
    ):
        body = start_body()
        body[field] = value
        rejected = await request("POST", "/api/market-data/sessions", json=body)
        assert rejected.status_code == 422

    body = start_body()
    body["events"][0]["api_key"] = "secret"
    rejected = await request("POST", "/api/market-data/sessions", json=body)
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_paper_preparation_is_real_loopback_only_fail_closed_and_reserves_after_admission(tmp_path, isolated_market_session):
    original_authority = state.execution_authority
    original_ledger = state._execution_ledger
    original_service = state._execution_service
    original_policy = state._preflight_policy_connection
    assert state.start_execution(tmp_path / "india.sqlite3", workspace="india")
    try:
        body = {"confirmation": "PREPARE_INDIA_PAPER", "symbol": "RELIANCE", "quantity": "1"}
        stopped = await request("POST", "/api/market-data/paper-preparations", json=body)
        assert stopped.status_code == 201
        assert stopped.json()["admission"]["decision"] == "DENIED"
        assert state._execution_ledger.get_reservation(stopped.json()["proposal_id"]) is None

        override = await request("POST", "/api/market-data/paper-preparations", json={**body, "broker": "t212"})
        assert override.status_code == 422

        await request("POST", "/api/market-data/sessions", json=start_body())
        state._execution_ledger.configure_paper_budget("paper", "INR", "1000")
        prepared = await request("POST", "/api/market-data/paper-preparations", json=body)
        assert prepared.status_code == 201, prepared.text
        assert prepared.json()["admission"]["decision"] == "ADMITTED"
        assert state._execution_ledger.get_reservation(prepared.json()["proposal_id"]) is not None
        isolated_market_session.call_tool.assert_not_awaited()
    finally:
        state.close_execution()
        state.execution_authority = original_authority
        state._execution_ledger = original_ledger
        state._execution_service = original_service
        state._preflight_policy_connection = original_policy

    body = start_body()
    body["events"][0]["instrument"]["symbol"] = "TCS"
    rejected = await request("POST", "/api/market-data/sessions", json=body)
    assert rejected.status_code == 422

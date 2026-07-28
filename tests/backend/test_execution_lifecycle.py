from fastapi.testclient import TestClient

from app_context import state
from server import app


def test_lifespan_uses_explicit_test_ledger_and_closes_it(monkeypatch, tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    monkeypatch.setenv("GROWIN_EXECUTION_DB_PATH", str(db_path))
    monkeypatch.setenv("GROWIN_WORKSPACE", "uk")

    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["execution_authority"] is True
        assert response.json()["execution_mode"] == "paper"
        assert db_path.exists()

    assert state.execution_authority is False
    assert state._execution_ledger is None

    with TestClient(app) as client:
        assert client.get("/health").json()["execution_authority"] is True

    assert state.execution_authority is False

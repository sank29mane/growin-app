import pytest
from fastapi.testclient import TestClient
from server import app
import unittest.mock as mock

client = TestClient(app)

@pytest.mark.asyncio
async def test_get_lmstudio_models_online():
    """Verify that we can list models when LM Studio is online."""
    with mock.patch("cache_manager.cache.get") as mock_cache_get:
        mock_cache_get.return_value = None
        with mock.patch("lm_studio_client.LMStudioClient.list_models") as mock_list:
            mock_list.return_value = [
                {"id": "llama-3-8b", "loaded": True},
                {"id": "mistral-7b", "loaded": False},
                {"id": "nomic-embed", "loaded": True} # Should be filtered out
            ]
            
            response = client.get("/api/models/lmstudio")
            assert response.status_code == 200
            data = response.json()
            assert "models" in data
            assert "llama-3-8b" in data["models"]
            assert "mistral-7b" in data["models"]
            assert "nomic-embed" not in data["models"]
            assert data["count"] == 2
            assert data["status"] == "online"

@pytest.mark.asyncio
async def test_load_lmstudio_model():
    """Verify triggering a model load."""
    with mock.patch("lm_studio_client.LMStudioClient.ensure_model_loaded") as mock_load:
        mock_load.return_value = True
        
        response = client.post(
            "/api/models/lmstudio/load",
            json={"model_id": "llama-3-8b", "context_length": 4096}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "success"
        mock_load.assert_called_once_with("llama-3-8b", context_length=4096, gpu_offload="max")

@pytest.mark.asyncio
async def test_get_lmstudio_status():
    """Verify getting detailed status."""
    with mock.patch("lm_studio_client.LMStudioClient.check_connection") as mock_conn:
        with mock.patch("lm_studio_client.LMStudioClient.list_loaded_models") as mock_loaded:
            mock_conn.return_value = True
            mock_loaded.return_value = ["llama-3-8b"]
            
            response = client.get("/api/models/lmstudio/status")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "online"
            assert data["loaded_model"] == "llama-3-8b"
            assert data["active"] is True

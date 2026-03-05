
import pytest
import asyncio
import json
from unittest.mock import patch, MagicMock, AsyncMock
from backend.lm_studio_client import LMStudioClient
from backend.memory_guard import MemoryGuardError

@pytest.mark.asyncio
async def test_ram_rule_01_concurrency_calculation():
    """
    RAM-RULE-01: Verify _setup_concurrency_limits correctly calculates slots based on total RAM.
    """
    with patch("psutil.virtual_memory") as mock_vm:
        # Simulate 16GB RAM
        mock_vm.return_value.total = 16 * (1024**3)
        client = LMStudioClient()
        # safe_ram = 16 * 0.6 = 9.6GB
        # slots = (9.6 - 8.0) / 0.5 = 1.6 / 0.5 = 3.2 -> 3 slots
        assert client.max_concurrent_predictions == 3

        # Simulate 64GB RAM
        mock_vm.return_value.total = 64 * (1024**3)
        client = LMStudioClient()
        # safe_ram = 64 * 0.6 = 38.4GB
        # slots = (38.4 - 8.0) / 0.5 = 30.4 / 0.5 = 60.8 -> Capped at 16
        assert client.max_concurrent_predictions == 16

        # Simulate 8GB RAM (Too low for 8GB model + KV)
        mock_vm.return_value.total = 8 * (1024**3)
        client = LMStudioClient()
        # safe_ram = 8 * 0.6 = 4.8GB
        # slots = (4.8 - 8.0) / 0.5 = -6.4 -> max(1, ...) = 1
        assert client.max_concurrent_predictions == 1

@pytest.mark.asyncio
async def test_load_01_ensure_model_loaded():
    """
    LOAD-01: Test ensure_model_loaded correctly identifies loaded vs. unloaded models.
    """
    client = LMStudioClient()
    
    # Mock list_models to show one model as loaded
    mock_models = [
        {
            "key": "loaded-model",
            "loaded_instances": [{"instance_id": "inst-1"}]
        },
        {
            "key": "unloaded-model",
            "loaded_instances": []
        }
    ]
    
    with patch.object(client, "list_models", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = mock_models
        
        # Already loaded
        assert await client.ensure_model_loaded("loaded-model") is True
        
        # Not loaded - should trigger load_model
        with patch.object(client, "load_model", new_callable=AsyncMock) as mock_load:
            mock_load.return_value = {"status": "success"}
            assert await client.ensure_model_loaded("unloaded-model") is True
            mock_load.assert_called_once_with("unloaded-model", context_length=8192, gpu="max")

@pytest.mark.asyncio
async def test_recovery_01_channel_error():
    """
    RECOVERY-01: Simulate a "Channel Error" and verify recovery triggers unload -> load -> retry.
    """
    client = LMStudioClient()
    client.active_model_id = "test-model"
    
    # First call fails with Channel Error, second call succeeds
    import httpx
    mock_response_fail = MagicMock()
    mock_response_fail.status_code = 500
    mock_response_fail.text = "Channel Error: Inference engine crashed"
    mock_response_fail.json.return_value = {"error": "Channel Error"}
    
    mock_response_success = MagicMock()
    mock_response_success.status_code = 200
    mock_response_success.json.return_value = {"choices": [{"message": {"content": "Recovered!"}}]}
    
    with patch("httpx.AsyncClient.post") as mock_post:
        # 1. Fail first
        # 2. Success second (after recovery)
        mock_post.side_effect = [
            httpx.HTTPStatusError("Error", request=MagicMock(), response=mock_response_fail),
            mock_response_success
        ]
        
        with patch.object(client, "unload_model", new_callable=AsyncMock) as mock_unload, \
             patch.object(client, "ensure_model_loaded", new_callable=AsyncMock) as mock_ensure_load:
            
            mock_ensure_load.return_value = True
            
            # Shorten sleep for test
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await client.chat(model_id="test-model", input_text="Hello")
                
                assert result["content"] == "Recovered!"
                mock_unload.assert_called_once_with("test-model")
                mock_ensure_load.assert_called_once_with("test-model")

@pytest.mark.asyncio
async def test_stateful_01_context_maintenance():
    """
    STATEFUL-01: Verify stateful_chat returns a response_id and correctly passes previous_response_id.
    """
    client = LMStudioClient()
    
    mock_resp_1 = {
        "response_id": "resp-1",
        "output": [{"type": "message", "content": "Hello! How can I help?"}],
        "stats": {}
    }
    
    mock_resp_2 = {
        "response_id": "resp-2",
        "output": [{"type": "message", "content": "Sure, I can do that."}],
        "stats": {}
    }
    
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.side_effect = [mock_resp_1, mock_resp_2]
        
        # First turn
        res1 = await client.stateful_chat(model_id="model-1", input_text="Hi")
        assert res1["response_id"] == "resp-1"
        assert res1["content"] == "Hello! How can I help?"
        
        # Verify first request payload
        args1, kwargs1 = mock_req.call_args_list[0]
        assert kwargs1["json"]["input"] == "Hi"
        assert "previous_response_id" not in kwargs1["json"]
        
        # Second turn
        res2 = await client.stateful_chat(model_id="model-1", input_text="What is your name?", previous_response_id="resp-1")
        assert res2["response_id"] == "resp-2"
        assert res2["content"] == "Sure, I can do that."
        
        # Verify second request payload
        args2, kwargs2 = mock_req.call_args_list[1]
        assert kwargs2["json"]["input"] == "What is your name?"
        assert kwargs2["json"]["previous_response_id"] == "resp-1"

@pytest.mark.asyncio
async def test_dry_run_edge_cases():
    """
    Test Case 2: Verify dry_run mode handles edge cases as expected.
    """
    client = LMStudioClient(dry_run=True)

    # Empty prompt
    res1 = await client.chat(input_text="")
    assert res1["content"] == ""

    # Overflow
    res2 = await client.chat(input_text="OVERFLOW-TOKENS test")
    assert res2["error"] == "context_window_exceeded"

    # Malformed tool
    res3 = await client.chat(input_text="MALFORMED-TOOL test")
    assert "tool_calls" in res3
    assert res3["tool_calls"][0]["function"]["arguments"] == "{"

    # Normal dry-run
    res4 = await client.chat(input_text="Normal message")
    assert res4["content"] == "Dry run response"

if __name__ == "__main__":
    import sys
    pytest.main([__file__])

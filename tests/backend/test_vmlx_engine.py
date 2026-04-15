import pytest
import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from vmlx_manager import VMLXInferenceEngine, get_vmlx_engine

@pytest.mark.asyncio
async def test_engine_init():
    engine = VMLXInferenceEngine(host="127.0.0.1", port=8001)
    assert engine.host == "127.0.0.1"
    assert engine.port == 8001
    assert "8001" in engine.url

@pytest.mark.asyncio
async def test_singleton():
    e1 = get_vmlx_engine()
    e2 = get_vmlx_engine()
    assert e1 is e2

@pytest.mark.asyncio
async def test_start_server_mock():
    engine = VMLXInferenceEngine(port=8002)
    
    with patch('asyncio.create_subprocess_exec') as mock_exec, \
         patch.object(VMLXInferenceEngine, 'check_health', return_value=True):
        
        mock_process = MagicMock()
        mock_exec.return_value = mock_process
        
        success = await engine.start_server()
        assert success is True
        assert engine.process is not None
        
        mock_exec.assert_called_once()
        args = mock_exec.call_args[0]
        assert "vmlx" in args
        assert "serve" in args
        assert "--memory-limit" in args
        assert "28GB" in args
        assert "--kv-cache-limit" in args
        assert "12GB" in args

@pytest.mark.asyncio
async def test_check_health_mock():
    engine = VMLXInferenceEngine(port=8003)
    
    with patch('aiohttp.ClientSession.get') as mock_get:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_get.return_value.__aenter__.return_value = mock_resp
        
        is_healthy = await engine.check_health()
        assert is_healthy is True

if __name__ == "__main__":
    asyncio.run(test_engine_init())
    print("Basic Engine tests passed.")

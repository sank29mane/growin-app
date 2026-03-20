import pytest
import json
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../backend')))
from docker_mcp_server import DockerMCPServer

pytestmark = pytest.mark.skip(reason="Docker Hub rate limits in CI environment")

@pytest.fixture
def docker_server():
    return DockerMCPServer()

@pytest.mark.asyncio
async def test_docker_run_python_basic(docker_server):
    """Test basic Python execution in Docker."""
    script = "print('hello from docker')"
    # execute_script is synchronous, run in thread
    result = await asyncio.to_thread(docker_server.execute_script, script)
    
    assert result["status"] == "success"
    assert "hello from docker" in result["stdout"]
    assert result["exit_code"] == 0

@pytest.mark.asyncio
async def test_docker_run_python_math(docker_server):
    """Test mathematical operations in Docker."""
    script = "import math; print(math.sqrt(144))"
    result = await asyncio.to_thread(docker_server.execute_script, script)
    
    assert result["status"] == "success"
    assert "12.0" in result["stdout"]

@pytest.mark.asyncio
async def test_docker_timeout(docker_server):
    """Test that timeout works correctly."""
    script = "import time; time.sleep(5)"
    # Set a very short timeout
    result = await asyncio.to_thread(docker_server.execute_script, script, timeout=1)
    
    assert result["status"] == "timeout"
    assert "timed out" in result["error"]

@pytest.mark.asyncio
async def test_docker_isolation(docker_server):
    """Test that the container is isolated (no network)."""
    script = """
import urllib.request
try:
    urllib.request.urlopen('http://google.com', timeout=1)
    print('connected')
except Exception as e:
    print(f'failed: {e}')
"""
    result = await asyncio.to_thread(docker_server.execute_script, script)
    assert "failed" in result["stdout"]

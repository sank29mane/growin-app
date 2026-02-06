import pytest
import asyncio
from unittest.mock import AsyncMock
from resilience import CircuitBreaker, retry_with_backoff, fallback, CircuitState

@pytest.mark.asyncio
async def test_circuit_breaker_state_transitions():
    """Test circuit breaker transitions from CLOSED -> OPEN -> HALF_OPEN -> CLOSED"""
    cb = CircuitBreaker(name="test_cb", failure_threshold=2, recovery_timeout=0.1)
    
    # 1. Initially CLOSED
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request() is True
    
    # 2. Failure 1 (Count 1/2)
    cb.record_failure(Exception("Fail 1"))
    assert cb.state == CircuitState.CLOSED
    
    # 3. Failure 2 (Count 2/2) -> OPEN
    cb.record_failure(Exception("Fail 2"))
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False
    
    # 4. Wait for recovery timeout
    await asyncio.sleep(0.15)
    
    # 5. Access triggers transition to HALF_OPEN
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.allow_request() is True  # Allowed once for testing
    
    # 6. Success -> CLOSED
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request() is True

@pytest.mark.asyncio
async def test_retry_with_backoff():
    """Test retry logic retries correct number of times"""
    mock_func = AsyncMock()
    mock_func.side_effect = [ValueError("Fail 1"), ValueError("Fail 2"), "Success"]
    
    @retry_with_backoff(max_retries=3, base_delay=0.01)
    async def sensitive_op():
        return await mock_func()
    
    result = await sensitive_op()
    
    assert result == "Success"
    assert mock_func.call_count == 3

@pytest.mark.asyncio
async def test_retry_exhausted():
    """Test retry fails after max retries"""
    mock_func = AsyncMock(side_effect=ValueError("Persistent Fail"))
    
    @retry_with_backoff(max_retries=2, base_delay=0.01)
    async def sensitive_op():
        await mock_func()
    
    with pytest.raises(ValueError):
        await sensitive_op()
        
    assert mock_func.call_count == 3  # Initial + 2 retries

@pytest.mark.asyncio
async def test_fallback_value():
    """Test fallback returns static value on failure"""
    @fallback(fallback_value="Default")
    async def failing_op():
        raise ValueError("Boom")
        
    result = await failing_op()
    assert result == "Default"

@pytest.mark.asyncio
async def test_fallback_function():
    """Test dynamic fallback function"""
    async def backup_op(args, kwargs, error):
        return f"Recovered from {error}"
        
    @fallback(fallback_func=backup_op)
    async def failing_op():
        raise ValueError("Crash")
        
    result = await failing_op()
    assert result == "Recovered from Crash"

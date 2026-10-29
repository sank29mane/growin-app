import pytest
from unittest.mock import AsyncMock, patch
from resilience import CircuitBreaker, retry_with_backoff, fallback, CircuitState

class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _no_sleep(_delay: float) -> None:
    return None


@pytest.mark.asyncio
async def test_circuit_breaker_state_transitions():
    """Test circuit breaker transitions from CLOSED -> OPEN -> HALF_OPEN -> CLOSED"""
    clock = _Clock()
    cb = CircuitBreaker(name="test_cb", failure_threshold=2, recovery_timeout=0.1, clock=clock)
    
    # 1. Initially CLOSED
    assert cb.state == CircuitState.CLOSED or str(cb.state) == 'CircuitState.CLOSED'
    assert cb.allow_request() is True
    
    # 2. Failure 1 (Count 1/2)
    cb.record_failure(Exception("Fail 1"))
    assert cb.state == CircuitState.CLOSED or str(cb.state) == 'CircuitState.CLOSED'
    
    # 3. Failure 2 (Count 2/2) -> OPEN
    cb.record_failure(Exception("Fail 2"))
    assert cb.state == CircuitState.OPEN or str(cb.state) == 'CircuitState.OPEN'
    assert cb.allow_request() is False
    
    # 4. Recovery timeout elapses on the injected clock, not the wall clock.
    clock.advance(0.1)
    
    # 5. Access triggers transition to HALF_OPEN
    assert cb.state == CircuitState.HALF_OPEN or str(cb.state) == 'CircuitState.HALF_OPEN'
    assert cb.allow_request() is True  # Allowed once for testing
    
    # 6. Success -> CLOSED
    cb.record_success()
    assert cb.state == CircuitState.CLOSED or str(cb.state) == 'CircuitState.CLOSED'
    assert cb.allow_request() is True

@pytest.mark.asyncio
async def test_retry_with_backoff():
    """Test retry logic retries correct number of times"""
    mock_func = AsyncMock()
    mock_func.side_effect = [ValueError("Fail 1"), ValueError("Fail 2"), "Success"]
    
    @retry_with_backoff(max_retries=3, base_delay=0.01, sleeper=_no_sleep)
    async def sensitive_op():
        return await mock_func()
    
    result = await sensitive_op()
    
    assert result == "Success"
    assert mock_func.call_count == 3

@pytest.mark.asyncio
async def test_retry_without_a_sleeper_looks_up_asyncio_sleep_per_retry():
    """Patching asyncio.sleep after the decorator ran must still take effect."""
    mock_func = AsyncMock(side_effect=[ValueError("Fail 1"), ValueError("Fail 2"), "Success"])

    @retry_with_backoff(max_retries=3, base_delay=0.01, jitter=0.0)
    async def sensitive_op():
        return await mock_func()

    with patch("asyncio.sleep", new=AsyncMock()) as fake_sleep:
        assert await sensitive_op() == "Success"
    assert fake_sleep.await_count == 2


@pytest.mark.asyncio
async def test_retry_exhausted():
    """Test retry fails after max retries"""
    mock_func = AsyncMock(side_effect=ValueError("Persistent Fail"))
    
    @retry_with_backoff(max_retries=2, base_delay=0.01, sleeper=_no_sleep)
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


def test_default_clock_resolves_time_time_per_call():
    """A patch applied after construction must still reach the breaker."""
    cb = CircuitBreaker(name="late_patch", failure_threshold=1, recovery_timeout=10.0)
    with patch("time.time", return_value=5_000.0):
        cb.record_failure(Exception("boom"))
    assert cb._last_failure_time == 5_000.0
    with patch("time.time", return_value=5_009.0):
        assert cb.state == CircuitState.OPEN
    with patch("time.time", return_value=5_010.0):
        assert cb.state == CircuitState.HALF_OPEN

import pytest
import asyncio
import time
from unittest.mock import AsyncMock
from resilience import CircuitBreaker, CircuitBreakerOpenError, CircuitState

@pytest.mark.asyncio
async def test_circuit_breaker_states():
    # Setup
    # Create a circuit that fails after 2 attempts and recovers in 2 seconds
    circuit = CircuitBreaker(name="test", failure_threshold=2, recovery_timeout=2.0)

    # Mock function that fails
    mock_func = AsyncMock(side_effect=ValueError("Failed!"))

    # 1. Verify CLOSED state (initial)
    assert circuit.state == CircuitState.CLOSED

    # 2. Trigger failures to OPEN the circuit
    with pytest.raises(ValueError):
        await circuit.call(mock_func) # Failure 1

    with pytest.raises(ValueError):
        await circuit.call(mock_func) # Failure 2 -> Threshold reached

    # Check if state transitioned to OPEN
    assert circuit.state == CircuitState.OPEN

    # 3. Verify Fail Fast (Circuit Open Exception)
    # The next call should fail immediately with CircuitBreakerOpenError *without* calling mock_func
    with pytest.raises(CircuitBreakerOpenError):
        await circuit.call(mock_func)

    # 4. Wait for Recovery Timeout (Simulate time passing)
    await asyncio.sleep(2.1)

    # 5. Verify HALF-OPEN transition via state access
    assert circuit.state == CircuitState.HALF_OPEN

    # 6. Verify success in HALF-OPEN closes circuit
    mock_func.side_effect = None
    mock_func.return_value = "Success"

    result = await circuit.call(mock_func)
    assert result == "Success"

    # 7. Verify CLOSED (Recovery)
    assert circuit.state == CircuitState.CLOSED
    assert circuit._failure_count == 0

@pytest.mark.asyncio
async def test_decorator_usage():
    circuit = CircuitBreaker(name="decorator_test", failure_threshold=1, recovery_timeout=1.0)

    @circuit.protect
    async def successful_func():
        return "OK"

    @circuit.protect
    async def failing_func():
        raise ValueError("Error")

    assert await successful_func() == "OK"

    with pytest.raises(ValueError):
        await failing_func()

    assert circuit.state == CircuitState.OPEN

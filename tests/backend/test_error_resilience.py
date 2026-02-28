import pytest
import asyncio
import time
from unittest.mock import AsyncMock
from utils.error_resilience import CircuitBreaker, CircuitBreakerOpenException, circuit_breaker

@pytest.mark.asyncio
async def test_circuit_breaker_states():
    # Setup
    # Create a circuit that fails after 2 attempts and recovers in 2 seconds
    circuit = CircuitBreaker(failure_threshold=2, recovery_timeout=2)
    
    # Mock function that fails
    mock_func = AsyncMock(side_effect=ValueError("Failed!"))
    
    # 1. Verify CLOSED state (initial)
    assert circuit.state == "CLOSED"
    
    # 2. Trigger failures to OPEN the circuit
    with pytest.raises(ValueError):
        await circuit.call(mock_func) # Failure 1
    
    with pytest.raises(ValueError):
        await circuit.call(mock_func) # Failure 2 -> Threshold reached
        
    # Check if state transitioned to OPEN
    assert circuit.state == "OPEN"
    
    # 3. Verify Fail Fast (Circuit Open Exception)
    # The next call should fail immediately with CircuitBreakerOpenException *without* calling mock_func
    with pytest.raises(CircuitBreakerOpenException):
        await circuit.call(mock_func)
        
    # 4. Wait for Recovery Timeout (Simulate time passing)
    # We can't really control time.time() easily without mocking time
    # But for a 2s timeout, we can sleep or mock time.
    # Let's mock time just for the circuit breaker instance if possible, or sleep.
    # 2s is short enough for a test.
    await asyncio.sleep(2.1)
    
    # 5. Verify HALF-OPEN
    # Next call should be allowed through.
    # Let's make the mock function succeed this time.
    mock_func.side_effect = None
    mock_func.return_value = "Success"
    
    result = await circuit.call(mock_func)
    assert result == "Success"
    
    # 6. Verify CLOSED (Recovery)
    assert circuit.state == "CLOSED"
    assert circuit.failure_count == 0

@pytest.mark.asyncio
async def test_decorator_usage():
    circuit = CircuitBreaker(failure_threshold=1, recovery_timeout=1)
    
    @circuit_breaker(circuit)
    async def successful_func():
        return "OK"
        
    @circuit_breaker(circuit)
    async def failing_func():
        raise ValueError("Error")
        
    assert await successful_func() == "OK"
    
    with pytest.raises(ValueError):
        await failing_func()
        
    assert circuit.state == "OPEN"

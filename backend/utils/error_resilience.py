import asyncio
import logging
import time
from functools import wraps
from typing import Callable, Any, Dict, Optional, Type

logger = logging.getLogger(__name__)

class CircuitBreakerOpenException(Exception):
    """Raised when the circuit breaker is open."""
    pass

class CircuitBreaker:
    """
    Implements the Circuit Breaker pattern to prevent cascading failures.
    States: CLOSED (normal), OPEN (fail fast), HALF-OPEN (testing recovery).
    """
    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60, expected_exceptions: tuple = (Exception,)):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exceptions = expected_exceptions
        
        self.failure_count = 0
        self.last_failure_time: float = 0.0
        self.state = "CLOSED" # CLOSED, OPEN, HALF-OPEN

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute the function with circuit breaker protection."""
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                logger.info("CircuitBreaker: Transitioning to HALF-OPEN")
                self.state = "HALF-OPEN"
            else:
                raise CircuitBreakerOpenException(f"Circuit is OPEN. Retrying in {int(self.recovery_timeout - (time.time() - self.last_failure_time))}s")

        try:
            result = await func(*args, **kwargs)
            
            if self.state == "HALF-OPEN":
                logger.info("CircuitBreaker: Success in HALF-OPEN. Closing circuit.")
                self.reset()
            elif self.failure_count > 0:
                self.failure_count = 0 
                
            return result

        except self.expected_exceptions as e:
            self.failure_count += 1
            self.last_failure_time = time.time()
            
            logger.warning(f"CircuitBreaker: Failure detected ({self.failure_count}/{self.failure_threshold}). Error: {e}")
            
            if self.failure_count >= self.failure_threshold:
                if self.state != "OPEN":
                    logger.error("CircuitBreaker: Threshold reached. Opening circuit.")
                    self.state = "OPEN"
            
            # Use strict feedback loop: if half-open fails, go back to open immediately
            if self.state == "HALF-OPEN":
                self.state = "OPEN"
                
            raise e

    def reset(self):
        self.failure_count = 0
        self.state = "CLOSED"
        self.last_failure_time = 0.0

def circuit_breaker(circuit: CircuitBreaker):
    """Decorator to apply a specific circuit breaker instance."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await circuit.call(func, *args, **kwargs)
        return wrapper
    return decorator

def retry_with_backoff(retries: int = 3, initial_delay: float = 1.0, backoff_factor: float = 2.0, exceptions: tuple = (Exception,)):
    """
    Decorator for exponential backoff retries.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exception = None
            
            for attempt in range(retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == retries:
                        break
                    
                    logger.warning(f"Retry: Attempt {attempt+1}/{retries} failed. Retrying in {delay}s. Error: {e}")
                    await asyncio.sleep(delay)
                    delay *= backoff_factor
            
            logger.error(f"Retry: All {retries} attempts failed.")
            raise last_exception
        return wrapper
    return decorator

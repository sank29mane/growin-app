"""
Resilience patterns for backend fault tolerance.

Provides:
- CircuitBreaker: Prevent cascade failures by stopping calls to failing services
- retry_with_backoff: Automatic retry with exponential backoff and jitter
- fallback: Graceful degradation when operations fail
"""

import asyncio
import functools
import logging
import random
import time
from enum import Enum
from typing import Any, Callable, Optional, TypeVar, Dict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

T = TypeVar('T')


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"       # Normal operation, requests allowed
    OPEN = "open"          # Failing, requests blocked
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreaker:
    """
    Circuit breaker pattern to prevent cascade failures.
    
    Usage:
        cb = CircuitBreaker(name="trading212")
        
        @cb.protect
        async def call_api():
            return await api.request()
    """
    name: str
    failure_threshold: int = 3
    recovery_timeout: float = 30.0
    half_open_max_calls: int = 1
    
    # Internal state
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _half_open_calls: int = field(default=0, init=False)

    @property
    def state(self) -> CircuitState:
        """Get current state, auto-transitioning from OPEN to HALF_OPEN if timeout passed."""
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0
                logger.info(f"CircuitBreaker[{self.name}]: OPEN -> HALF_OPEN (recovery timeout passed)")
        return self._state

    def record_success(self):
        """Record successful call, potentially closing the circuit."""
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            logger.info(f"CircuitBreaker[{self.name}]: HALF_OPEN -> CLOSED (success)")
        elif self._state == CircuitState.CLOSED:
            # Reset failure count on success
            self._failure_count = 0

    def record_failure(self, error: Exception):
        """Record failed call, potentially opening the circuit."""
        self._failure_count += 1
        self._last_failure_time = time.time()
        
        if self._state == CircuitState.HALF_OPEN:
            # Failure during testing reopens immediately
            self._state = CircuitState.OPEN
            logger.warning(f"CircuitBreaker[{self.name}]: HALF_OPEN -> OPEN (failure during test: {error})")
        elif self._state == CircuitState.CLOSED and self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            logger.warning(f"CircuitBreaker[{self.name}]: CLOSED -> OPEN (threshold reached: {self._failure_count} failures)")

    def allow_request(self) -> bool:
        """Check if a request should be allowed."""
        state = self.state  # This may trigger state transition
        
        if state == CircuitState.CLOSED:
            return True
        elif state == CircuitState.OPEN:
            return False
        else:  # HALF_OPEN
            if self._half_open_calls < self.half_open_max_calls:
                self._half_open_calls += 1
                return True
            return False

    def protect(self, func: Callable) -> Callable:
        """Decorator to protect a function with this circuit breaker."""
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            if not self.allow_request():
                raise CircuitBreakerOpenError(f"CircuitBreaker[{self.name}] is OPEN")
            
            try:
                result = await func(*args, **kwargs)
                self.record_success()
                return result
            except Exception as e:
                self.record_failure(e)
                raise
        
        return wrapper


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open and blocks the request."""
    pass


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exponential_base: float = 2.0,
    jitter: float = 0.5,
    retryable_exceptions: tuple = (Exception,),
):
    """
    Decorator for automatic retry with exponential backoff and jitter.
    
    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay between retries (seconds)
        max_delay: Maximum delay cap (seconds)
        exponential_base: Base for exponential calculation
        jitter: Random jitter range (±jitter seconds)
        retryable_exceptions: Tuple of exceptions that should trigger retry
    
    Usage:
        @retry_with_backoff(max_retries=3, base_delay=1.0)
        async def fetch_data():
            return await api.get_data()
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e
                    
                    if attempt == max_retries:
                        logger.error(f"Retry exhausted for {func.__name__} after {max_retries + 1} attempts: {e}")
                        raise
                    
                    # Calculate delay with exponential backoff and jitter
                    delay = min(base_delay * (exponential_base ** attempt), max_delay)
                    delay += random.uniform(-jitter, jitter)
                    delay = max(0.1, delay)  # Ensure positive delay
                    
                    logger.warning(f"Retry {attempt + 1}/{max_retries} for {func.__name__} in {delay:.2f}s: {e}")
                    await asyncio.sleep(delay)
            
            raise last_exception
        
        return wrapper
    return decorator


def fallback(
    fallback_value: Any = None,
    fallback_func: Optional[Callable] = None,
    log_error: bool = True,
):
    """
    Decorator for graceful degradation when operations fail.
    
    Args:
        fallback_value: Static value to return on failure
        fallback_func: Function to call on failure (receives original args/kwargs and exception)
        log_error: Whether to log the error
    
    Usage:
        @fallback(fallback_value={"prices": [], "error": "Data unavailable"})
        async def get_prices():
            return await api.get_prices()
        
        # Or with a dynamic fallback
        @fallback(fallback_func=lambda args, kwargs, e: get_cached_data(kwargs['ticker']))
        async def get_prices(ticker: str):
            return await api.get_prices(ticker)
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                if log_error:
                    logger.warning(f"Fallback triggered for {func.__name__}: {e}")
                
                if fallback_func is not None:
                    try:
                        result = fallback_func(args, kwargs, e)
                        if asyncio.iscoroutine(result):
                            return await result
                        return result
                    except Exception as fb_error:
                        logger.error(f"Fallback function also failed: {fb_error}")
                        return fallback_value
                
                return fallback_value
        
        return wrapper
    return decorator


# Pre-configured circuit breakers for common services
_circuit_breakers: Dict[str, CircuitBreaker] = {}


def get_circuit_breaker(name: str, **kwargs) -> CircuitBreaker:
    """Get or create a circuit breaker by name."""
    if name not in _circuit_breakers:
        _circuit_breakers[name] = CircuitBreaker(name=name, **kwargs)
    return _circuit_breakers[name]


# Utility function for timeout
async def with_timeout(coro, timeout: float, default: Any = None):
    """Execute coroutine with timeout, returning default on timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"Operation timed out after {timeout}s")
        return default

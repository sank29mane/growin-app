"""
Error Resilience Layer - Redundant Error Handling and Fallback System
Provides robust error handling with automatic retries, circuit breakers, and fallback chains.
"""

import logging
import asyncio
import functools
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, TypeVar
from enum import Enum

logger = logging.getLogger(__name__)

T = TypeVar('T')


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"      # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """
    Circuit breaker pattern to prevent cascading failures.
    Opens circuit after failure_threshold failures, closes after success in HALF_OPEN.
    """
    
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        half_open_requests: int = 3
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_requests = half_open_requests
        
        self.state = CircuitState.CLOSED
        self.failures = 0
        self.successes = 0
        self.last_failure_time: Optional[datetime] = None
        self.half_open_attempts = 0
    
    def record_success(self):
        """Record successful call"""
        if self.state == CircuitState.HALF_OPEN:
            self.successes += 1
            if self.successes >= self.half_open_requests:
                self.close()
        elif self.state == CircuitState.CLOSED:
            self.failures = 0  # Reset failure count on success
    
    def record_failure(self):
        """Record failed call"""
        self.failures += 1
        self.last_failure_time = datetime.now()
        
        if self.state == CircuitState.HALF_OPEN:
            self.open()
        elif self.failures >= self.failure_threshold:
            self.open()
    
    def open(self):
        """Open circuit (reject requests)"""
        if self.state != CircuitState.OPEN:
            logger.warning(f"Circuit breaker '{self.name}' OPENED after {self.failures} failures")
            self.state = CircuitState.OPEN
            self.half_open_attempts = 0
    
    def close(self):
        """Close circuit (allow requests)"""
        if self.state != CircuitState.CLOSED:
            logger.info(f"Circuit breaker '{self.name}' CLOSED after recovery")
            self.state = CircuitState.CLOSED
            self.failures = 0
            self.successes = 0
    
    def half_open(self):
        """Test if service recovered"""
        if self.state != CircuitState.HALF_OPEN:
            logger.info(f"Circuit breaker '{self.name}' entering HALF_OPEN state for testing")
            self.state = CircuitState.HALF_OPEN
            self.successes = 0
    
    def can_proceed(self) -> bool:
        """Check if request should be allowed"""
        if self.state == CircuitState.CLOSED:
            return True
        
        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has elapsed
            if self.last_failure_time:
                elapsed = (datetime.now() - self.last_failure_time).total_seconds()
                if elapsed >= self.recovery_timeout:
                    self.half_open()
                    return True
            return False
        
        # HALF_OPEN: allow limited requests
        if self.half_open_attempts < self.half_open_requests:
            self.half_open_attempts += 1
            return True
        return False


class FallbackChain:
    """
    Manages a chain of fallback providers for data fetching.
    Tries each provider in order until one succeeds.
    """
    
    def __init__(self, name: str):
        self.name = name
        self.providers: List[Dict[str, Any]] = []
        self.circuit_breakers: Dict[str, CircuitBreaker] = {}
    
    def add_provider(
        self,
        name: str,
        func: Callable,
        priority: int = 0,
        circuit_breaker_config: Optional[Dict[str, Any]] = None
    ):
        """Add a provider to the fallback chain"""
        self.providers.append({
            "name": name,
            "func": func,
            "priority": priority
        })
        
        # Sort by priority (higher = tried first)
        self.providers.sort(key=lambda x: x["priority"], reverse=True)
        
        # Create circuit breaker for this provider
        if circuit_breaker_config is None:
            circuit_breaker_config = {}
        
        self.circuit_breakers[name] = CircuitBreaker(
            name=f"{self.name}:{name}",
            **circuit_breaker_config
        )
    
    async def execute(self, *args, **kwargs) -> Optional[Any]:
        """
        Execute the fallback chain.
        Returns result from first successful provider, or None if all fail.
        """
        last_error = None
        
        for provider in self.providers:
            provider_name = provider["name"]
            provider_func = provider["func"]
            circuit_breaker = self.circuit_breakers[provider_name]
            
            # Check circuit breaker
            if not circuit_breaker.can_proceed():
                logger.debug(f"Circuit breaker {provider_name} is OPEN, skipping")
                continue
            
            try:
                logger.debug(f"Trying provider: {provider_name}")
                result = await provider_func(*args, **kwargs)
                
                # Check if result is valid (not None, not empty)
                if result is not None:
                    circuit_breaker.record_success()
                    logger.info(f"✓ Provider '{provider_name}' succeeded")
                    return result
                else:
                    logger.debug(f"Provider '{provider_name}' returned None/empty")
                    circuit_breaker.record_failure()
                    
            except Exception as e:
                logger.warning(f"Provider '{provider_name}' failed: {e}")
                circuit_breaker.record_failure()
                last_error = e
        
        # All providers failed
        logger.error(f"All providers in chain '{self.name}' failed. Last error: {last_error}")
        return None


def with_retry(
    max_attempts: int = 3,
    backoff_base: float = 2.0,
    max_backoff: float = 60.0,
    exceptions: tuple = (Exception,)
):
    """
    Decorator for exponential backoff retry logic.
    
    Args:
        max_attempts: Maximum number of retry attempts
        backoff_base: Base for exponential backoff (seconds)
        max_backoff: Maximum backoff time (seconds)
        exceptions: Tuple of exceptions to catch and retry
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_attempts - 1:
                        # Last attempt, raise the exception
                        logger.error(f"{func.__name__} failed after {max_attempts} attempts: {e}")
                        raise
                    
                    # Calculate backoff time
                    backoff = min(backoff_base ** attempt, max_backoff)
                    logger.warning(
                        f"{func.__name__} attempt {attempt + 1}/{max_attempts} failed: {e}. "
                        f"Retrying in {backoff:.1f}s..."
                    )
                    await asyncio.sleep(backoff)
            
            raise RuntimeError(f"{func.__name__} failed after {max_attempts} attempts")
        
        return wrapper
    return decorator


class DataProviderManager:
    """
    Centralized manager for data providers with fallback chains and circuit breakers.
    """
    
    def __init__(self):
        self.chains: Dict[str, FallbackChain] = {}
        self._talib_warning_shown = False
    
    def get_or_create_chain(self, name: str) -> FallbackChain:
        """Get existing chain or create new one"""
        if name not in self.chains:
            self.chains[name] = FallbackChain(name)
        return self.chains[name]
    
    def register_provider(
        self,
        chain_name: str,
        provider_name: str,
        provider_func: Callable,
        priority: int = 0,
        circuit_breaker_config: Optional[Dict[str, Any]] = None
    ):
        """Register a provider to a fallback chain"""
        chain = self.get_or_create_chain(chain_name)
        chain.add_provider(provider_name, provider_func, priority, circuit_breaker_config)
    
    async def fetch_data(self, chain_name: str, *args, **kwargs) -> Optional[Any]:
        """Fetch data using the specified fallback chain"""
        if chain_name not in self.chains:
            logger.error(f"No fallback chain registered for '{chain_name}'")
            return None
        
        chain = self.chains[chain_name]
        return await chain.execute(*args, **kwargs)
    
    def check_talib_available(self) -> bool:
        """
        Check if TA-Lib is available.
        Shows warning only once to avoid log spam.
        """
        try:
            import talib
            return True
        except ImportError:
            if not self._talib_warning_shown:
                logger.warning(
                    "TA-Lib not available. Technical indicators will be limited. "
                    "Install with: pip install TA-Lib"
                )
                self._talib_warning_shown = True
            return False
    
    def get_circuit_status(self) -> Dict[str, Any]:
        """Get status of all circuit breakers"""
        status = {}
        for chain_name, chain in self.chains.items():
            status[chain_name] = {}
            for provider_name, breaker in chain.circuit_breakers.items():
                status[chain_name][provider_name] = {
                    "state": breaker.state.value,
                    "failures": breaker.failures,
                    "last_failure": breaker.last_failure_time.isoformat() if breaker.last_failure_time else None
                }
        return status


# Global instance
provider_manager = DataProviderManager()


def safe_dict_access(data: Any, *keys, default=None) -> Any:
    """
    Safely access nested dictionary/list data with fallback.
    
    Args:
        data: Input data (dict, list, or other)
        *keys: Keys to access in order
        default: Default value if access fails
    
    Returns:
        Value at nested key path, or default if not found
    
    Examples:
        safe_dict_access({"a": {"b": 1}}, "a", "b") -> 1
        safe_dict_access([1, 2, 3], 0) -> 1
        safe_dict_access({"a": 1}, "b", default=0) -> 0
    """
    result = data
    for key in keys:
        try:
            if isinstance(result, dict):
                result = result.get(key, default)
            elif isinstance(result, (list, tuple)) and isinstance(key, int):
                result = result[key] if 0 <= key < len(result) else default
            else:
                return default
            
            if result is default:
                return default
        except (KeyError, IndexError, TypeError, AttributeError):
            return default
    
    return result


def normalize_response_format(response: Any) -> Dict[str, Any]:
    """
    Normalize various response formats to a consistent dict format with 'data' and 'metadata'.
    
    Handles:
    - Dict with 'data' key
    - Dict with 'bars' key (data provider format)
    - Raw list (legacy format)
    - None/empty responses
    
    Args:
        response: Response from data provider
        
    Returns:
        Normalized dict with 'data' and 'metadata' keys
    """
    if response is None:
        return {"data": [], "metadata": {}}
    
    if isinstance(response, dict):
        # Already has data key
        if "data" in response:
            return response
        
        # Has bars key (provider format)
        if "bars" in response:
            return {
                "data": response["bars"],
                "metadata": {
                    "ticker": response.get("ticker"),
                    "timeframe": response.get("timeframe"),
                    "provider": response.get("provider")
                }
            }
        
        # Dict without data key - treat as metadata wrapper
        return {
            "data": [],
            "metadata": response
        }
    
    if isinstance(response, (list, tuple)):
        # Raw list - wrap it
        return {
            "data": list(response),
            "metadata": {}
        }
    
    # Unknown format
    logger.warning(f"Unknown response format: {type(response)}")
    return {"data": [], "metadata": {"error": "Unknown format"}}


# Convenience functions
async def safe_api_call(
    func: Callable,
    *args,
    fallback_value: Any = None,
    log_errors: bool = True,
    **kwargs
) -> Any:
    """
    Safely execute an API call with error handling.
    
    Args:
        func: Async function to call
        *args: Positional arguments
        fallback_value: Value to return on error
        log_errors: Whether to log errors
        **kwargs: Keyword arguments
        
    Returns:
        Result from func or fallback_value on error
    """
    try:
        return await func(*args, **kwargs)
    except Exception as e:
        if log_errors:
            logger.error(f"API call failed in {func.__name__}: {e}")
        return fallback_value

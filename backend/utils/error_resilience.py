import asyncio
import logging
import functools
import time
from functools import wraps
from enum import Enum
from datetime import datetime
from typing import Callable, Any, Dict, Optional, Type, List, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar('T')

class CircuitBreakerOpenException(Exception):
    """Raised when the circuit breaker is open."""
    pass

class CircuitBreaker:
    """
    Implements the Circuit Breaker pattern to prevent cascading failures.
    States: CLOSED (normal), OPEN (fail fast), HALF-OPEN (testing recovery).
    """
    def __init__(self, name: str = "default", failure_threshold: int = 5, recovery_timeout: int = 60, expected_exceptions: tuple = (Exception,)):
        self.name = name
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

class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"      # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered



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
        Execute the fallback chain using the standardized CircuitBreaker.call() pattern.
        Returns result from first successful provider, or None if all fail.
        """
        last_error = None

        for provider in self.providers:
            provider_name = provider["name"]
            provider_func = provider["func"]
            circuit_breaker = self.circuit_breakers[provider_name]

            async def _call_provider():
                logger.debug(f"Trying provider: {provider_name}")
                res = await provider_func(*args, **kwargs)
                if res is None:
                    logger.debug(f"Provider '{provider_name}' returned None/empty")
                    raise ValueError(f"Provider '{provider_name}' returned None")
                return res

            try:
                result = await circuit_breaker.call(_call_provider)
                logger.info(f"✓ Provider '{provider_name}' succeeded")
                return result
            except CircuitBreakerOpenException:
                logger.debug(f"Circuit breaker {provider_name} is OPEN, skipping")
                continue
            except Exception as e:
                logger.warning(f"Provider '{provider_name}' failed: {e}")
                last_error = e
                continue

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
                    "state": breaker.state,
                    "failures": breaker.failure_count,
                    "last_failure": datetime.fromtimestamp(breaker.last_failure_time).isoformat() if breaker.last_failure_time > 0 else None
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

import asyncio
from typing import Awaitable, TypeVar, Any

T = TypeVar('T')

async def run_with_timeout(coro: Awaitable[T], timeout: float) -> T:
    """
    Run an awaitable with a timeout.

    Provides a unified interface across Python versions, using `asyncio.timeout`
    if available (Python >= 3.11), otherwise falling back to `asyncio.wait_for`.

    Args:
        coro: The coroutine or awaitable to execute.
        timeout: The maximum execution time in seconds.

    Returns:
        The result of the coroutine if it completes within the timeout.

    Raises:
        asyncio.TimeoutError: If the execution exceeds the specified timeout.
    """
    if hasattr(asyncio, 'timeout'):
        async with asyncio.timeout(timeout):
            return await coro
    else:
        return await asyncio.wait_for(coro, timeout=timeout)

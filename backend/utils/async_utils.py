import asyncio
from typing import Awaitable, TypeVar

T = TypeVar('T')

async def run_with_timeout(coro: Awaitable[T], timeout: float) -> T:
    """
    Execute a coroutine with a timeout.
    Compat helper to abstract `asyncio.timeout` (Python 3.11+)
    vs `asyncio.wait_for` (Python <= 3.10) logic.
    """
    if hasattr(asyncio, 'timeout'):
        async with asyncio.timeout(timeout):
            return await coro
    else:
        return await asyncio.wait_for(coro, timeout=timeout)

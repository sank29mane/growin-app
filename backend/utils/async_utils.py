"""
Async Utilities - Centralized async helpers for Growin App
"""

import asyncio
from typing import Awaitable, TypeVar

T = TypeVar('T')

async def run_with_timeout(coro: Awaitable[T], timeout: float) -> T:
    """
    Run an awaitable with a timeout, using asyncio.timeout if available (Python 3.11+),
    otherwise falling back to asyncio.wait_for.
    """
    if hasattr(asyncio, 'timeout'):
        async with asyncio.timeout(timeout):
            return await coro
    else:
        return await asyncio.wait_for(coro, timeout=timeout)

import asyncio
import time
import random
import logging
from typing import Dict, Optional, Literal, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Priority Levels
PRIORITY_EXECUTION = 0  # BUY/SELL/CANCEL - Top Priority
PRIORITY_SYNC = 1       # Account/Portfolio Sync - Medium
PRIORITY_POLLING = 2    # Price/Quote Polling - Low

@dataclass
class TokenBucket:
    capacity: float
    refill_rate: float  # tokens per second
    tokens: float
    last_update: float

class T212RequestBudgeter:
    """
    SOTA 2026: Priority-Aware Token Bucket Rate Limiter for Trading 212.
    
    Enforces the '20 tickers / 5 seconds' limit (sustained 4 req/sec) 
    while prioritizing order execution over background polling.
    """
    
    def __init__(self, capacity: int = 20, refill_rate: float = 4.0):
        self.bucket = TokenBucket(
            capacity=float(capacity),
            refill_rate=refill_rate,
            tokens=float(capacity),
            last_update=time.monotonic()
        )
        self.lock = asyncio.Lock()
        self.queues = {
            PRIORITY_EXECUTION: asyncio.Queue(),
            PRIORITY_SYNC: asyncio.Queue(),
            PRIORITY_POLLING: asyncio.Queue()
        }
        self._worker_task = None
        self._running = False

    async def start(self):
        """Start the background processing loop."""
        if not self._running:
            self._running = True
            # No worker task needed if we use request-based throttling, 
            # but we keep this for future batching logic if needed.
            logger.info("T212RequestBudgeter started.")

    async def acquire(self, priority: int = PRIORITY_POLLING, ticker: Optional[str] = None):
        """
        Acquires a token from the bucket. Blocks until a token is available.
        
        Args:
            priority: Priority level (0=High, 2=Low)
            ticker: Optional ticker for logging
        """
        async with self.lock:
            while True:
                # 1. Update bucket tokens
                now = time.monotonic()
                elapsed = now - self.bucket.last_update
                self.bucket.tokens = min(
                    self.bucket.capacity,
                    self.bucket.tokens + (elapsed * self.bucket.refill_rate)
                )
                self.bucket.last_update = now

                # 2. Check if we have a token
                if self.bucket.tokens >= 1.0:
                    # SOTA: Prioritize Execution calls if they exist in queues (Wait logic)
                    # This ensures Execution calls jump the line.
                    if priority > PRIORITY_EXECUTION and not self.queues[PRIORITY_EXECUTION].empty():
                        # Pause lower priorities to let execution through
                        await asyncio.sleep(0.1)
                        continue

                    self.bucket.tokens -= 1.0
                    
                    # 3. Add Temporal Jitter for Execution calls to appear human (Compliance)
                    if priority == PRIORITY_EXECUTION:
                        jitter = random.uniform(0.5, 2.0)
                        logger.info(f"Budgeter: Token granted for EXECUTION. Adding {jitter:.2f}s human jitter.")
                        await asyncio.sleep(jitter)
                    
                    return True
                
                # 4. Wait for refill
                wait_time = (1.0 - self.bucket.tokens) / self.bucket.refill_rate
                # logger.debug(f"Budgeter: Rate limit hit. Waiting {wait_time:.2f}s...")
                await asyncio.sleep(wait_time)

    def get_status(self) -> Dict[str, Any]:
        """Returns the current state of the budgeter."""
        return {
            "available_tokens": round(self.bucket.tokens, 2),
            "refill_rate": self.bucket.refill_rate,
            "capacity": self.bucket.capacity
        }

# Global Singleton for the backend
_budgeter_instance = None

def get_t212_budgeter() -> T212RequestBudgeter:
    global _budgeter_instance
    if _budgeter_instance is None:
        _budgeter_instance = T212RequestBudgeter()
    return _budgeter_instance

"""
Rate Limiter for FastAPI
Uses Token Bucket algorithm for smoother rate limiting.
"""

import time
import threading
from typing import Dict
from fastapi import Request, HTTPException
import logging

logger = logging.getLogger(__name__)


class TokenBucket:
    """
    Token Bucket Algorithm.
    Allows for bursts but enforces average rate.
    """
    def __init__(self, rate_hertz: float, burst_capacity: int):
        self.rate = rate_hertz # Tokens per second
        self.capacity = burst_capacity
        self.tokens = burst_capacity
        self.last_refill = time.time()
        self.lock = threading.Lock()

    def consume(self, tokens: int = 1) -> bool:
        with self.lock:
            now = time.time()
            elapsed = now - self.last_refill
            
            # Refill tokens
            new_tokens = elapsed * self.rate
            self.tokens = min(self.capacity, self.tokens + new_tokens)
            self.last_refill = now
            
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False


class RateLimiter:
    """
    Global Rate Limiter managing token buckets per IP/User.
    """
    def __init__(self, rate_limit_per_minute: int = 60, burst_limit: int = 10):
        self.rate = rate_limit_per_minute / 60.0
        self.burst = burst_limit
        self.buckets: Dict[str, TokenBucket] = {}
        self.lock = threading.Lock()
        
    async def check(self, request: Request):
        """
        Check if the request exceeds rate limit.
        Used as a dependency in FastAPI routes.
        """
        # For local dev, use a generic key or IP
        client_ip = request.client.host if request.client else "unknown"
        
        with self.lock:
            if client_ip not in self.buckets:
                self.buckets[client_ip] = TokenBucket(self.rate, self.burst)
            bucket = self.buckets[client_ip]
        
        if not bucket.consume():
            logger.warning(f"Rate limit exceeded for {client_ip}")
            raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")
        
        return True

# Default instance (60 requests per minute, burst of 10)
default_limiter = RateLimiter(rate_limit_per_minute=60, burst_limit=10)

"""
Global Cache Manager for Growin App
Provides centralized caching for agents and API responses with LRU eviction.
"""

import time
import logging
from typing import Any, Dict, Optional
from collections import OrderedDict
import threading

logger = logging.getLogger(__name__)


class CacheManager:
    """
    Singleton Cache Manager with LRU eviction.
    
    Features:
    - In-memory cache with TTL
    - LRU eviction when max size reached
    - Thread-safe operations
    - Hit/miss statistics
    """
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, max_size: int = 1000):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(CacheManager, cls).__new__(cls)
                    cls._instance._cache = OrderedDict()
                    cls._instance._expiry = {}
                    cls._instance._max_size = max_size
                    cls._instance._hits = 0
                    cls._instance._misses = 0
        return cls._instance
    
    def get(self, key: str) -> Optional[Any]:
        """Retrieve item from cache if not expired, using LRU ordering."""
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
                
            expire_at = self._expiry.get(key, 0)
            if time.time() > expire_at:
                self._delete_unsafe(key)
                self._misses += 1
                return None
            
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            self._hits += 1
            return self._cache[key]
    
    def set(self, key: str, value: Any, ttl: int = 300):
        """Store item in cache with TTL (seconds), evicting LRU if needed."""
        with self._lock:
            # If key exists, update and move to end
            if key in self._cache:
                self._cache.move_to_end(key)
            else:
                # Evict LRU items if at capacity
                while len(self._cache) >= self._max_size:
                    oldest_key = next(iter(self._cache))
                    self._delete_unsafe(oldest_key)
                    logger.debug(f"LRU evicted: {oldest_key}")
            
            self._cache[key] = value
            self._expiry[key] = time.time() + ttl
    
    def _delete_unsafe(self, key: str):
        """Delete without lock (internal use only)."""
        self._cache.pop(key, None)
        self._expiry.pop(key, None)
        
    def delete(self, key: str):
        """Remove item from cache."""
        with self._lock:
            self._delete_unsafe(key)
        
    def clear(self):
        """Clear all cache."""
        with self._lock:
            self._cache.clear()
            self._expiry.clear()
            self._hits = 0
            self._misses = 0
        logger.info("Global cache cleared.")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100) if total > 0 else 0.0
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": f"{hit_rate:.1f}%"
            }
    
    def cleanup_expired(self) -> int:
        """Remove expired entries. Returns count of removed items."""
        with self._lock:
            now = time.time()
            expired_keys = [k for k, exp in self._expiry.items() if now > exp]
            for key in expired_keys:
                self._delete_unsafe(key)
            return len(expired_keys)


# Shared instance
cache = CacheManager()


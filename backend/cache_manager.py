import time
import logging
import os
import json
import threading
from typing import Any, Dict, Optional
from collections import OrderedDict

logger = logging.getLogger(__name__)

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    msg = "Redis module not found. Install 'redis' for L2 caching."
    logger.warning(msg)
    REDIS_AVAILABLE = False

class CacheManager:
    """
    Two-Level Cache Manager (L1: In-Memory, L2: Redis).
    Singleton pattern.
    """
    _instance = None
    _lock = threading.Lock()
    
    # Type hints for instance attributes
    _cache: OrderedDict
    _expiry: Dict[str, float]
    _max_size: int
    _l1_hits: int
    _l1_misses: int
    _redis: Optional[Any]

    
    def __new__(cls, max_size: int = 1000, redis_url: Optional[str] = None):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(CacheManager, cls).__new__(cls)
                    # L1 Init
                    cls._instance._cache = OrderedDict()
                    cls._instance._expiry = {}
                    cls._instance._max_size = max_size
                    cls._instance._l1_hits = 0
                    cls._instance._l1_misses = 0
                    
                    # L2 Init
                    cls._instance._redis = None
                    if REDIS_AVAILABLE:
                         # Use env var or passed arg
                        url = redis_url or os.getenv("REDIS_URL")
                        if url:
                            try:
                                cls._instance._redis = redis.from_url(url, socket_connect_timeout=1)
                                cls._instance._redis.ping() # Check connection
                                logger.info(f"L2 Cache (Redis) connected: {url}")
                            except Exception as e:
                                logger.warning(f"L2 Cache (Redis) connection failed: {e}. Running in L1-only mode.")
                                cls._instance._redis = None
        return cls._instance
    
    def get(self, key: str) -> Optional[Any]:
        """Retrieve item from L1, then L2."""
        with self._lock:
            # 1. Check L1
            if key in self._cache:
                expire_at = self._expiry.get(key, 0)
                if time.time() > expire_at:
                    self._delete_l1_unsafe(key)
                else:
                    self._cache.move_to_end(key)
                    self._l1_hits += 1
                    return self._cache[key]
            
            self._l1_misses += 1
            
        # 2. Check L2 (Outside lock for IO)
        if self._redis:
            try:
                # Assuming simple string values or serialized JSON
                # For complex objects, we might need pickle, but usually JSON is safer for interoperability
                # Here assuming the user handles serialization if needed, or we use pickle?
                # The prompt implies standard cache. Let's try pickle for Python objects transparency?
                # Or JSON. Let's stick to simple get for now, assuming value is bytes/string.
                # Actually, standard pattern: use pickle for Python object cache.
                import pickle
                if self._redis: # Check again for Mypy
                    data = self._redis.get(key)
                    if data:
                        val = pickle.loads(data)
                        # Populate L1
                        self.set(key, val, ttl=300) # Default TTL refresh
                        return val
            except Exception as e:
                logger.warning(f"L2 Cache get failed: {e}")
                
        return None
    
    def set(self, key: str, value: Any, ttl: int = 300):
        """Set item in L1 and L2."""
        # 1. Set L1
        with self._lock:
            if len(self._cache) >= self._max_size:
                 self._cache.popitem(last=False)
            
            self._cache[key] = value
            self._expiry[key] = time.time() + ttl
            
        # 2. Set L2
        if self._redis:
            try:
                import pickle
                data = pickle.dumps(value)
                if self._redis: # Check again for Mypy
                     self._redis.setex(key, ttl, data)
            except Exception as e:
                logger.warning(f"L2 Cache set failed: {e}")
    
    def _delete_l1_unsafe(self, key: str):
        self._cache.pop(key, None)
        self._expiry.pop(key, None)
        
    def delete(self, key: str):
        with self._lock:
            self._delete_l1_unsafe(key)
        if self._redis:
            try:
                self._redis.delete(key)
            except Exception:
                pass

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._expiry.clear()
            self._l1_hits = 0
            self._l1_misses = 0
        if self._redis:
            try:
                self._redis.flushdb()
            except Exception:
                pass
    
    def get_stats(self) -> Dict[str, Any]:
        return {
            "l1_size": len(self._cache),
            "l1_hits": self._l1_hits,
            "l1_misses": self._l1_misses,
            "l2_status": "Connected" if self._redis else "Disabled"
        }

# Initializer for easy access
cache = CacheManager()


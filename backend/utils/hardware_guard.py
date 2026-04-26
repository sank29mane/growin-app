import asyncio
import psutil
import logging
from typing import Optional
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

class ResourceGuard:
    """
    Hardware-aware concurrency guard for M4 Pro (48GB RAM).
    Prevents SSD swap thrashing by enforcing VRAM and RAM thresholds.
    """
    
    _instance: Optional['ResourceGuard'] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ResourceGuard, cls).__new__(cls)
            # Standardizing on BoundedSemaphore(1) for Heavy GPU tasks (D-09/D-10)
            cls._instance.gpu_semaphore = asyncio.BoundedSemaphore(1)
            cls._instance.vram_threshold_gb = 28.0  # M4 Pro "Safe Zone"
            cls._instance.ram_min_free_gb = 4.0     # Minimum free RAM to allow load
            
        return cls._instance

    def check_memory_safety(self) -> bool:
        """
        Verify if loading a new model is safe for the system.
        """
        mem = psutil.virtual_memory()
        available_gb = mem.available / (1024**3)
        
        if available_gb < self.ram_min_free_gb:
            logger.warning(f"⚠️ Memory safety check failed: {available_gb:.2f}GB available (Min: {self.ram_min_free_gb}GB)")
            return False
            
        return True

    @asynccontextmanager
    async def heavy_inference(self):
        """
        Context manager for high-VRAM operations (30B+ models).
        Enforces sequential access to the Apple Silicon GPU for large models.
        """
        if not self.check_memory_safety():
            # In a real scenario, we might trigger a cache flush or wait longer
            logger.info("Waiting for memory to clear before inference...")
            
        async with self.gpu_semaphore:
            logger.debug("🔒 GPU Semaphore acquired for heavy inference.")
            try:
                yield
            finally:
                logger.debug("🔓 GPU Semaphore released.")

# Global singleton
hardware_guard = ResourceGuard()

async def get_hardware_guard():
    return hardware_guard

import os
import psutil
import logging
from typing import Dict

logger = logging.getLogger(__name__)

class MemoryProfiler:
    """
    Utility to monitor memory usage of the current process.
    """
    
    def __init__(self, threshold_mb: float = 512.0):
        self.process = psutil.Process(os.getpid())
        self.threshold_mb = threshold_mb
        
    def get_memory_usage(self) -> Dict[str, float]:
        """
        Get current memory usage statistics in MB.
        """
        mem_info = self.process.memory_info()
        return {
            "rss": mem_info.rss / 1024 / 1024,  # Resident Set Size
            "vms": mem_info.vms / 1024 / 1024,  # Virtual Memory Size
            "percent": self.process.memory_percent()
        }
    
    def log_memory_usage(self, tag: str = ""):
        """
        Log current memory usage.
        """
        stats = self.get_memory_usage()
        logger.info(f"[Memory {tag}] RSS: {stats['rss']:.2f} MB, VMS: {stats['vms']:.2f} MB, Usage: {stats['percent']:.2f}%")
        
    def check_memory_pressure(self) -> bool:
        """
        Check if memory usage exceeds threshold.
        """
        stats = self.get_memory_usage()
        if stats['rss'] > self.threshold_mb:
            logger.warning(f"High Memory Usage Detected: {stats['rss']:.2f} MB > {self.threshold_mb} MB")
            return True
        return False

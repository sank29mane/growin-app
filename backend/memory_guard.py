import os
import subprocess
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class MemoryGuardError(Exception):
    """Exception raised when memory guard limits are breached."""
    pass

class MemoryGuard:
    """
    SOTA: sysctl-based Memory Guard for Darwin (macOS/Apple Silicon).
    Implements a 60% RAM usage hard-gate and a 4GB free-RAM hard-gate.
    Optimized for high-performance multi-agent bursts on M4 Pro/Max.
    """
    
    # Strategy parameters from research (Phase 27)
    MAX_RAM_USAGE_FRACTION = 0.60
    MIN_FREE_RAM_BYTES = 4 * 1024 * 1024 * 1024  # 4GB
    
    @staticmethod
    def _run_sysctl(key: str) -> int:
        """Runs sysctl and returns the integer value."""
        try:
            output = subprocess.check_output(["sysctl", "-n", key], stderr=subprocess.DEVNULL)
            return int(output.decode("utf-8").strip())
        except (subprocess.CalledProcessError, ValueError) as e:
            logger.error(f"MemoryGuard: Failed to get sysctl key {key}: {e}")
            return 0

    @classmethod
    def get_memory_stats(cls) -> Dict[str, Any]:
        """
        Retrieves memory statistics directly from sysctl.
        Calculates Total, Free, and Used memory in bytes.
        """
        total_ram = cls._run_sysctl("hw.memsize")
        page_size = cls._run_sysctl("hw.pagesize") or 16384  # Default to 16KB on Apple Silicon
        
        # macOS specific: vm.page_free_count + vm.page_speculative_count is "immediately available"
        free_pages = cls._run_sysctl("vm.page_free_count")
        spec_pages = cls._run_sysctl("vm.page_speculative_count")
        
        free_ram = (free_pages + spec_pages) * page_size
        used_ram = total_ram - free_ram
        usage_percent = (used_ram / total_ram) * 100 if total_ram > 0 else 0
        
        return {
            "total_gb": total_ram / (1024**3),
            "free_gb": free_ram / (1024**3),
            "used_gb": used_ram / (1024**3),
            "usage_percent": usage_percent,
            "total_bytes": total_ram,
            "free_bytes": free_ram,
            "used_bytes": used_ram
        }

    @classmethod
    def check_safety(cls, raise_error: bool = True) -> bool:
        """
        Hard-gate check for memory safety.
        Returns True if safe, False if breached (or raises MemoryGuardError).
        """
        stats = cls.get_memory_stats()
        
        # 1. 60% RAM usage hard-gate
        if stats["usage_percent"] > (cls.MAX_RAM_USAGE_FRACTION * 100):
            msg = (f"Memory Guard Breach: RAM usage ({stats['usage_percent']:.1f}%) "
                   f"exceeds hard-gate limit ({cls.MAX_RAM_USAGE_FRACTION*100:.0f}%). "
                   f"Used: {stats['used_gb']:.1f}GB / {stats['total_gb']:.1f}GB.")
            logger.warning(msg)
            if raise_error:
                raise MemoryGuardError(msg)
            return False
            
        # 2. 4GB free-RAM hard-gate
        if stats["free_bytes"] < cls.MIN_FREE_RAM_BYTES:
            msg = (f"Memory Guard Breach: Free RAM ({stats['free_gb']:.2f}GB) "
                   f"below hard-gate limit ({cls.MIN_FREE_RAM_BYTES / (1024**3):.0f}GB).")
            logger.warning(msg)
            if raise_error:
                raise MemoryGuardError(msg)
            return False
            
        logger.info(f"Memory Guard Safe: {stats['free_gb']:.1f}GB free ({stats['usage_percent']:.1f}% used).")
        return True

if __name__ == "__main__":
    # Diagnostic mode
    logging.basicConfig(level=logging.INFO)
    try:
        stats = MemoryGuard.get_memory_stats()
        print(f"Memory Stats: {stats}")
        MemoryGuard.check_safety()
    except MemoryGuardError as e:
        print(f"GUARD TRIGGERED: {e}")

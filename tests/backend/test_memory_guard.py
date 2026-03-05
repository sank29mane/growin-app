import unittest
from unittest.mock import patch, MagicMock
from backend.memory_guard import MemoryGuard, MemoryGuardError

class TestMemoryGuard(unittest.TestCase):
    """Tests for the sysctl-based Memory Guard."""

    def setUp(self):
        # 48GB Total RAM, 16KB Page Size
        self.mock_total_ram = 51539607552
        self.mock_page_size = 16384

    def _mock_sysctl(self, free_pages: int, spec_pages: int):
        """Helper to mock sysctl calls."""
        def side_effect(args, **kwargs):
            # args is ["sysctl", "-n", key]
            key = args[2]
            if key == "hw.memsize":
                return str(self.mock_total_ram).encode()
            if key == "hw.pagesize":
                return str(self.mock_page_size).encode()
            if key == "vm.page_free_count":
                return str(free_pages).encode()
            if key == "vm.page_speculative_count":
                return str(spec_pages).encode()
            return b"0"
        return side_effect

    @patch("subprocess.check_output")
    def test_safe_state(self, mock_run):
        """Test that the guard passes when memory is within limits."""
        # Simulate ~20GB free (well above 4GB and usage well below 60%)
        # 20GB / 16KB = 1,310,720 pages
        mock_run.side_effect = self._mock_sysctl(1000000, 310720)
        
        self.assertTrue(MemoryGuard.check_safety(raise_error=False))
        stats = MemoryGuard.get_memory_stats()
        self.assertLess(stats["usage_percent"], 60.0)
        self.assertGreater(stats["free_bytes"], 4 * 1024**3)

    @patch("subprocess.check_output")
    def test_usage_limit_breach(self, mock_run):
        """Test that the guard triggers when >60% RAM is used."""
        # Total 48GB. 60% = 28.8GB used, 19.2GB free.
        # Let's simulate 10GB free (Usage = 79% > 60%)
        # 10GB / 16KB = 655,360 pages
        mock_run.side_effect = self._mock_sysctl(500000, 155360)
        
        # In this case, free_bytes (10GB) is > 4GB, but usage (79%) is > 60%.
        with self.assertRaises(MemoryGuardError) as cm:
            MemoryGuard.check_safety()
        self.assertIn("usage", str(cm.exception))

    @patch("subprocess.check_output")
    def test_free_limit_breach(self, mock_run):
        """Test that the guard triggers when <4GB is free."""
        # Total 48GB. 4GB = 262,144 pages.
        # Simulate 2GB free.
        # 2GB / 16KB = 131,072 pages.
        mock_run.side_effect = self._mock_sysctl(100000, 31072)
        
        # Free is 2GB (<4GB). Usage is ~95% (>60%).
        # Note: Both guards will trigger, but the usage one is checked first in current impl.
        # If we simulate a total RAM where 2GB free is < 4GB but usage is < 60%:
        self.mock_total_ram = 5 * 1024**3 # 5GB
        # 2GB free = 60% used (boundary). Let's use 3GB free (40% used).
        # 3GB free = 3 * 1024**3 / 16384 = 196,608 pages
        mock_run.side_effect = self._mock_sysctl(150000, 46608)
        
        with self.assertRaises(MemoryGuardError) as cm:
            MemoryGuard.check_safety()
        self.assertIn("Free RAM", str(cm.exception))

if __name__ == "__main__":
    unittest.main()

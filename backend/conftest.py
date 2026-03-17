import sys
from unittest.mock import MagicMock
sys.modules['mlx'] = MagicMock()
sys.modules['mlx.core'] = MagicMock()
sys.modules['mlx.nn'] = MagicMock()

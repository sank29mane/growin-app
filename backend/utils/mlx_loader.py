"""
Centralized utility for importing MLX (Apple Silicon NPU/GPU).
Safely handles environments where MLX is not available (e.g. Linux CI).
"""

try:
    import mlx.core as mx
    import mlx.nn as nn
    HAS_MLX = True
except ImportError:
    mx = None

    class DummyModule:
        def __init__(self, *args, **kwargs): pass
        def __call__(self, *args, **kwargs): pass

    class DummyNN:
        Module = DummyModule

        @staticmethod
        def Linear(*args, **kwargs): return lambda x: x

        @staticmethod
        def TransformerEncoder(*args, **kwargs): return lambda x, mask=None: x

    nn = DummyNN()
    HAS_MLX = False

import os

try:
    if os.getenv("CI") == "true" or os.getenv("GITHUB_ACTIONS") == "true":
        raise RuntimeError("Disabling MLX on CI environment to avoid hardware emulation crashes.")
    import mlx.core as mx
    import mlx.nn as nn
    HAS_MLX = True
except BaseException:
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

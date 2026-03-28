import re

def skip_mlx(filepath):
    with open(filepath, "r") as f:
        code = f.read()

    skip_code = """import pytest
try:
    import mlx.core as mx
except ImportError:
    pytest.skip("MLX not available", allow_module_level=True)
    mx = None
"""
    code = re.sub(r"import mlx\.core as mx", skip_code, code, count=1)

    with open(filepath, "w") as f:
        f.write(code)

skip_mlx("tests/backend/test_jmce_forward.py")
skip_mlx("tests/backend/test_neural_jmce_intraday.py")

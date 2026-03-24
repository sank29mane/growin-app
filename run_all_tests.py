import os
import subprocess
import time
import glob

test_files = sorted(glob.glob('tests/backend/test_*.py'))
results = []
for test_file in test_files:
    start = time.time()
    try:
        proc = subprocess.run(
            ['uv', 'run', '--project', 'backend', 'pytest', test_file],
            env={**os.environ, "CI": "true", "PYTHONPATH": "."},
            capture_output=True,
            text=True,
            timeout=60
        )
        duration = time.time() - start
        if proc.returncode == 0:
            results.append((test_file, 'PASS', duration))
        else:
            if 'AttributeError: \'NoneType\' object has no attribute \'array\'' in proc.stderr or proc.stdout:
                 results.append((test_file, 'MLX_ERROR', duration))
            else:
                 results.append((test_file, 'FAIL', duration))
    except subprocess.TimeoutExpired:
        results.append((test_file, 'TIMEOUT', 10))

for f, s, d in results:
    if s in ['FAIL', 'TIMEOUT']:
        print(f"{f}: {s} ({d:.2f}s)")

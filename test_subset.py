import os
import subprocess
import time

test_files = [
    'tests/backend/test_api_schemas.py',
    'tests/backend/test_architecture_edge_cases.py',
    'tests/backend/test_audit_logging.py',
    'tests/backend/test_chat_endpoints.py',
    'tests/backend/test_chat_manager_functional.py'
]

for test_file in test_files:
    start = time.time()
    try:
        proc = subprocess.run(
            ['uv', 'run', '--project', 'backend', 'pytest', test_file],
            env={**os.environ, "CI": "true", "PYTHONPATH": ".:backend"},
            capture_output=True,
            text=True,
            timeout=30
        )
        duration = time.time() - start
        print(f"{test_file}: {'PASS' if proc.returncode == 0 else 'FAIL'} ({duration:.2f}s)")
        if proc.returncode != 0:
            print(proc.stderr[:200])
    except subprocess.TimeoutExpired:
        print(f"{test_file}: TIMEOUT")

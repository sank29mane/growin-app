import os
import subprocess
import time

test_files = [
    'tests/backend/test_audit_logging.py',
    'tests/backend/test_chat_endpoints.py',
    'tests/backend/test_chat_manager_functional.py',
    'tests/backend/test_coordinator_fixes.py',
    'tests/backend/test_coordinator_model.py',
    'tests/backend/test_cors.py',
    'tests/backend/test_data_engine_fixes.py',
    'tests/backend/test_data_engine_optimization.py',
    'tests/backend/test_data_frayer.py'
]

import os
for test_file in test_files:
    start = time.time()
    try:
        proc = subprocess.run(
            ['uv', 'run', '--project', 'backend', 'pytest', test_file],
            env={**os.environ, "CI": "true", "PYTHONPATH": ".:backend", "OPENAI_API_KEY": "sk-1234"},
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

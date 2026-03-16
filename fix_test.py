import subprocess
import time

start = time.time()
try:
    proc = subprocess.run(
        ['uv', 'run', '--project', 'backend', 'python', '-m', 'unittest', 'tests/backend/test_chat_endpoints.py'],
        capture_output=True,
        text=True,
        timeout=10
    )
    print("RETURN CODE:", proc.returncode)
    print("STDOUT:", proc.stdout)
    print("STDERR:", proc.stderr)
except subprocess.TimeoutExpired:
    print("TIMEOUT!")

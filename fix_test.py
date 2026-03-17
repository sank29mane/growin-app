import subprocess
import os

for test in ['test_chat_message_success_and_timestamp', 'test_conversation_history_timestamps', 'test_list_conversations_timestamps', 'test_error_handling_native_mlx']:
    print(f"Running {test}...")
    try:
        proc = subprocess.run(
            ['uv', 'run', '--project', 'backend', 'pytest', f'tests/backend/test_chat_endpoints.py::TestChatEndpoints::{test}'],
            env={**os.environ, "CI": "true", "PYTHONPATH": ".:backend", "OPENAI_API_KEY": "sk-1234"},
            capture_output=True,
            text=True,
            timeout=10
        )
        print(f"  {test}: {'PASS' if proc.returncode == 0 else 'FAIL'}")
        if proc.returncode != 0:
            print("ERROR", proc.stderr[:100])
            print("STDOUT", proc.stdout[:100])
    except subprocess.TimeoutExpired:
        print(f"  {test}: TIMEOUT")

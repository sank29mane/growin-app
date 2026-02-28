import os
import sys
import subprocess

# Set environment variables explicitly
os.environ["JULES_API_KEY"] = "AQ.Ab8RN6JnSkY0o_0j6N1Wh999chokpNHerofZkoaHyS1tV8Nhxw"
os.environ["JULES_API_URL"] = "https://jules.googleapis.com/v1alpha"

# Path to the jules-mcp executable
executable = "/Users/sanketmane/Codes/Jules_MCP/.venv/bin/jules-mcp"

# Run the server and pipe stdio
process = subprocess.Popen(
    [executable],
    stdin=sys.stdin,
    stdout=sys.stdout,
    stderr=sys.stderr,
    env=os.environ
)
process.wait()

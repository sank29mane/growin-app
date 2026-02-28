import subprocess
import json
import os
import sys

def test_startup(command, cwd=None, env=None):
    cmd_str = " ".join(command)
    print(f"\n--- Testing command: {cmd_str} ---")
    print(f"Working Directory: {cwd}")
    
    # Initialize JSON-RPC message
    init_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "capabilities": {},
            "clientInfo": {"name": "test-debug", "version": "1.0"},
            "protocolVersion": "2024-11-05"
        }
    }
    
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    try:
        stdout_data, stderr_data = process.communicate(input=json.dumps(init_msg) + "\n", timeout=5)
        print(f"Exit Code: {process.returncode}")
        
        # Clean up any log output that might be mixed in
        json_resp = None
        for line in stdout_data.splitlines():
            line = line.strip()
            if line.startswith('{'):
                try:
                    json_resp = json.loads(line)
                    print("\n✅ Successfully parsed JSON-RPC response!")
                    print(json.dumps(json_resp, indent=2))
                    break
                except json.JSONDecodeError:
                    continue
        
        if not json_resp:
            print("\n❌ Failed to find a valid JSON-RPC response in STDOUT.")
            print("\nSTDOUT (raw):")
            print(stdout_data)
        
        if stderr_data:
            print("\nSTDERR:")
            print(stderr_data)
            
    except subprocess.TimeoutExpired:
        print("\n❌ Timeout: Process did not respond within 5 seconds.")
        process.kill()
    except Exception as e:
        print(f"\n❌ Error: {str(e)}")
        process.kill()
        
    return False

if __name__ == "__main__":
    project_dir = "/Users/sanketmane/Codes/Jules_MCP"
    
    # Test 1: uv run
    uv_cmd = ["uv", "--directory", project_dir, "run", "jules-mcp"]
    test_startup(uv_cmd)
    
    # Test 2: docker run
    docker_cmd = [
        "docker", "run", "-i", "--rm", 
        "--env-file", os.path.join(project_dir, ".env"),
        "jules-mcp"
    ]
    test_startup(docker_cmd)

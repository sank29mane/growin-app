"""
Docker MCP Server - Provides tools for agents to execute scripts in isolated containers.
Wrapper around docker-py to expose safe container operations to the Coordinator Agent.
"""

import tarfile
import io
import os
import logging
import asyncio
import json
from typing import Dict, Any, Optional, List
from mcp.server import Server
from mcp.types import Tool, TextContent

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("docker_mcp_server")

class DockerMCPServer:
    def __init__(self):
        self.client = None
        self.image = os.getenv("DOCKER_SANDBOX_IMAGE", "python:3.11-slim")
        self._initialized = False

    def _initialize_client(self):
        """Lazy initialization of the Docker client"""
        if self._initialized:
            return True
            
        try:
            import docker
            self.client = docker.from_env()
            self.client.ping()
            self._initialized = True
            logger.info(f"Docker client initialized. Using image: {self.image}")
            return True
        except ImportError:
            logger.error("The 'docker' Python package is not installed.")
            return False
        except Exception as e:
            logger.error(f"Could not connect to Docker daemon: {e}")
            return False

    def _ensure_image(self):
        """Ensure the execution image is present"""
        if not self._initialize_client():
            return False
            
        try:
            self.client.images.get(self.image)
            return True
        except Exception:
            try:
                logger.info(f"Pulling image {self.image}...")
                self.client.images.pull(self.image)
                return True
            except Exception as e:
                logger.error(f"Failed to pull image {self.image}: {e}")
                return False

    def execute_script(self, script_content: str, timeout: int = 15) -> Dict[str, Any]:
        """
        Execute a Python script in an isolated container.
        
        Args:
            script_content: The Python code to run
            timeout: Max execution time in seconds
            
        Returns:
            Dict with 'stdout', 'stderr', 'exit_code'
        """
        if not self._ensure_image():
            return {
                "error": "Docker is not available or image could not be pulled.",
                "status": "error"
            }

        container = None
        try:
            # Create container with strict limits
            container = self.client.containers.create(
                self.image,
                command="python /app/script.py",
                working_dir="/app",
                network_mode="none",  # No network access
                mem_limit="128m",     # 128MB RAM limit
                pids_limit=20,        # Limit processes
                detach=True,
                tty=False
            )

            # Copy script into container
            script_tar = self._create_tar_stream("script.py", script_content)
            container.put_archive("/app", script_tar)

            # Start and wait
            container.start()
            
            # Use a wait with timeout
            try:
                result = container.wait(timeout=timeout)
                exit_code = result.get("StatusCode", -1)
            except Exception:
                # Handle timeout
                container.kill()
                return {
                    "error": f"Script execution timed out after {timeout}s",
                    "status": "timeout",
                    "exit_code": -1
                }
            
            logs = container.logs(stdout=True, stderr=True)
            return {
                "stdout": logs.decode('utf-8'),
                "exit_code": exit_code,
                "status": "success"
            }

        except Exception as e:
            logger.error(f"Container execution error: {e}")
            return {"error": str(e), "status": "error"}
        finally:
            if container:
                try:
                    container.remove(force=True)
                except Exception as e:
                    logger.debug(f"Failed to remove container: {e}")

    def _create_tar_stream(self, name: str, content: str) -> io.BytesIO:
        """Create a tar stream for file injection"""
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode='w') as tar:
            data = content.encode('utf-8')
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        tar_stream.seek(0)
        return tar_stream

# MCP App Instance
app = Server("docker-sandbox")
docker_tool = DockerMCPServer()

@app.list_tools()
async def list_tools() -> List[Tool]:
    return [
        Tool(
            name="docker_run_python",
            description="Execute a Python script in a secure, isolated Docker container. Use this for data cleaning or logic fixes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": "Python script content to execute"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 15)",
                        "default": 15
                    }
                },
                "required": ["script"]
            }
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> List[TextContent]:
    if name == "docker_run_python":
        script = arguments.get("script")
        timeout = arguments.get("timeout", 15)
        
        if not script:
            return [TextContent(type="text", text="Error: Missing 'script' argument")]
            
        # Run in a thread to not block the event loop since docker-py is synchronous
        result = await asyncio.to_thread(docker_tool.execute_script, script, timeout)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    
    return [TextContent(type="text", text=f"Error: Unknown tool {name}")]

async def main():
    from mcp.server.stdio import stdio_server
    
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream, 
            write_stream, 
            app.create_initialization_options()
        )

if __name__ == "__main__":
    asyncio.run(main())

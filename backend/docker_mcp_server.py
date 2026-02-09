"""
Docker MCP Server - Provides tools for agents to execute scripts in isolated containers.
Wrapper around docker-py to expose safe container operations to the Coordinator Agent.
"""

import docker
import tarfile
import io
import os
from typing import Dict, Any, Optional
from mcp.server import Server
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource

class DockerMCPServer:
    def __init__(self):
        self.client = docker.from_env()
        self.image = "python:3.11-slim"
        self._ensure_image()

    def _ensure_image(self):
        """Pull the execution image if not present"""
        try:
            self.client.images.get(self.image)
        except docker.errors.ImageNotFound:
            print(f"Pulling image {self.image}...")
            self.client.images.pull(self.image)

    def execute_script(self, script_content: str, timeout: int = 10) -> Dict[str, Any]:
        """
        Execute a Python script in an isolated container.
        
        Args:
            script_content: The Python code to run
            timeout: Max execution time in seconds
            
        Returns:
            Dict with 'stdout', 'stderr', 'exit_code'
        """
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
                tty=True
            )

            # Copy script into container
            script_tar = self._create_tar_stream("script.py", script_content)
            container.put_archive("/app", script_tar)

            # Start and wait
            container.start()
            result = container.wait(timeout=timeout)
            
            logs = container.logs(stdout=True, stderr=True)
            return {
                "stdout": logs.decode('utf-8'),
                "exit_code": result.get("StatusCode", -1),
                "status": "success"
            }

        except Exception as e:
            return {"error": str(e), "status": "error"}
        finally:
            if container:
                try:
                    container.remove(force=True)
                except:
                    pass

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

# MCP Tool Exposure
def create_server() -> Server:
    server = Server("docker-sandbox")
    docker_tool = DockerMCPServer()

    @server.tool(
        name="docker_run_python",
        description="Execute a Python script in a secure, isolated Docker container. Use this for data cleaning or logic fixes.",
        input_schema={
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "Python script content to execute"
                }
            },
            "required": ["script"]
        }
    )
    async def docker_run_python(script: str) -> list[TextContent]:
        result = docker_tool.execute_script(script)
        return [TextContent(type="text", text=str(result))]

    return server

if __name__ == "__main__":
    # If run directly, start the MCP server
    from mcp.server.stdio import stdio_server
    server = create_server()
    import asyncio
    asyncio.run(stdio_server(server))

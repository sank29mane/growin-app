#!/usr/bin/env python3
"""
HuggingFace MCP Server
A Model Context Protocol server for HuggingFace model management.
Provides tools to load, unload, and run inference on models from HuggingFace.
"""

import asyncio
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Resource,
    Tool,
    TextContent
)
from utils import sanitize_nan
from utils.process_guard import start_parent_watchdog

# Start watchdog immediately
start_parent_watchdog()

# Global model cache
loaded_models = {}

class HFModelManager:
    """Manages loading and unloading of HuggingFace models."""

    def __init__(self):
        self.loaded_models = {}

    def load_model(self, repo_id: str, task: str = "text-generation") -> str:
        """Load a model from HuggingFace."""
        try:
            if repo_id in self.loaded_models:
                return f"Model {repo_id} already loaded"

            # For demo, we'll use transformers
            from transformers import AutoTokenizer, AutoModelForCausalLM

            print(f"Loading model: {repo_id}", file=sys.stderr)

            tokenizer = AutoTokenizer.from_pretrained(repo_id)
            model = AutoModelForCausalLM.from_pretrained(repo_id)

            self.loaded_models[repo_id] = {
                "model": model,
                "tokenizer": tokenizer,
                "task": task
            }

            return f"Successfully loaded model: {repo_id}"
        except Exception as e:
            return f"Failed to load model {repo_id}: {str(e)}"

    def unload_model(self, repo_id: str) -> str:
        """Unload a model."""
        if repo_id in self.loaded_models:
            del self.loaded_models[repo_id]
            return f"Unloaded model: {repo_id}"
        return f"Model {repo_id} not loaded"

    def list_loaded_models(self) -> list:
        """List currently loaded models."""
        return list(self.loaded_models.keys())

    def run_inference(self, repo_id: str, prompt: str, max_length: int = 100) -> str:
        """Run inference on a loaded model."""
        if repo_id not in self.loaded_models:
            return f"Model {repo_id} not loaded"

        try:
            model_data = self.loaded_models[repo_id]
            model = model_data["model"]
            tokenizer = model_data["tokenizer"]

            inputs = tokenizer(prompt, return_tensors="pt")
            outputs = model.generate(**inputs, max_length=max_length, num_return_sequences=1)
            response = tokenizer.decode(outputs[0], skip_special_tokens=True)

            return response
        except Exception as e:
            return f"Inference failed: {str(e)}"

# Initialize
hf_manager = HFModelManager()
app = Server("huggingface-mcp-server")

@app.list_resources()
async def list_resources() -> list[Resource]:
    """List available HuggingFace resources."""
    return [
        Resource(
            uri="huggingface://models/loaded",
            name="Loaded Models",
            mimeType="application/json",
            description="List of currently loaded models"
        ),
    ]

@app.read_resource()
async def read_resource(uri: str) -> str:
    """Read a HuggingFace resource."""
    if uri == "huggingface://models/loaded":
        return json.dumps(sanitize_nan(hf_manager.list_loaded_models()))
    raise ValueError(f"Unknown resource: {uri}")

@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available HuggingFace tools."""
    return [
        Tool(
            name="load_model",
            description="Load a model from HuggingFace Hub",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_id": {
                        "type": "string",
                        "description": "HuggingFace model repo ID (e.g., 'microsoft/DialoGPT-small')"
                    },
                    "task": {
                        "type": "string",
                        "description": "Task type (default: text-generation)",
                        "default": "text-generation"
                    }
                },
                "required": ["repo_id"]
            }
        ),
        Tool(
            name="unload_model",
            description="Unload a model from memory",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_id": {
                        "type": "string",
                        "description": "HuggingFace model repo ID"
                    }
                },
                "required": ["repo_id"]
            }
        ),
        Tool(
            name="list_loaded_models",
            description="List all currently loaded models",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="run_inference",
            description="Run inference on a loaded model",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_id": {
                        "type": "string",
                        "description": "Loaded model repo ID"
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Input prompt for inference"
                    },
                    "max_length": {
                        "type": "number",
                        "description": "Maximum response length",
                        "default": 100
                    }
                },
                "required": ["repo_id", "prompt"]
            }
        ),
    ]

@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Execute a HuggingFace tool."""
    try:
        if name == "load_model":
            repo_id = arguments["repo_id"]
            task = arguments.get("task", "text-generation")
            result = hf_manager.load_model(repo_id, task)
            return [TextContent(type="text", text=result)]

        elif name == "unload_model":
            repo_id = arguments["repo_id"]
            result = hf_manager.unload_model(repo_id)
            return [TextContent(type="text", text=result)]

        elif name == "list_loaded_models":
            models = hf_manager.list_loaded_models()
            return [TextContent(type="text", text=json.dumps(models))]

        elif name == "run_inference":
            repo_id = arguments["repo_id"]
            prompt = arguments["prompt"]
            max_length = arguments.get("max_length", 100)
            result = hf_manager.run_inference(repo_id, prompt, max_length)
            return [TextContent(type="text", text=result)]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )

if __name__ == "__main__":
    asyncio.run(main())
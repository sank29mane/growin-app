"""
LM Studio v1 API Client
Handles stateful chats, model management, and authentication for local inference.
"""

import os
import httpx
import logging
import json
from typing import List, Dict, Any, Optional, AsyncGenerator

logger = logging.getLogger(__name__)

class LMStudioClient:
    """
    Async client for LM Studio's v1 REST API.

    Supports:
    - Authentication via LM_API_TOKEN
    - Stateful Chat (/api/v1/chat)
    - Model Management (Load/Unload/Download)
    - MCP Integrations
    """

    BASE_URL = "http://127.0.0.1:1234"

    def __init__(self, base_url: str = None, api_token: str = None):
        self.base_url = base_url or os.getenv("LM_STUDIO_URL", self.BASE_URL)
        self.api_token = api_token or os.getenv("LM_API_TOKEN")

        self.headers = {
            "Content-Type": "application/json"
        }
        if self.api_token:
            self.headers["Authorization"] = f"Bearer {self.api_token}"

    async def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """Internal helper for making requests with error handling."""
        # Ensure endpoint starts with /
        if not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"
        url = f"{self.base_url}{endpoint}"
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.request(method, url, headers=self.headers, **kwargs)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"LM Studio API Error ({e.response.status_code}): {e.response.text}")
                raise RuntimeError(f"LM Studio API Error: {e.response.text}") from e
            except httpx.RequestError as e:
                logger.error(f"LM Studio Connection Error: {e}")
                raise RuntimeError(f"Could not connect to LM Studio at {self.base_url}") from e

    async def check_connection(self) -> bool:
        """Verify server is reachable and running."""
        try:
            # Try root models endpoint
            await self._request("GET", "/v1/models")
            return True
        except RuntimeError:
            return False

    async def list_models(self) -> List[Dict[str, Any]]:
        """List all available models using root API."""
        data = await self._request("GET", "/v1/models")
        return data.get("data", [])

    async def list_loaded_models(self) -> List[str]:
        """List IDs of currently loaded models."""
        models = await self.list_models()
        # In standard OpenAI/LMStudio root API, the list is the loaded models
        return [m.get("id") for m in models]

    async def load_model(self, model_id: str, context_length: int = 8192, gpu_offload: str = "max") -> Dict[str, Any]:
        """
        Load a model into memory.

        Args:
            model_id: The specific model ID/path
            context_length: Max context window
            gpu_offload: 'max', 'off', or number of layers
        """
        payload = {
            "model": model_id,
            "config": {
                "context_length": context_length,
                "gpu_offload": gpu_offload
            }
        }
        logger.info(f"Loading model: {model_id}...")
        return await self._request("POST", "/v1/models/load", json=payload)

    async def unload_model(self, model_id: str) -> Dict[str, Any]:
        """Unload a specific model."""
        return await self._request("POST", "/v1/models/unload", json={"model": model_id})

    async def chat(
        self,
        model_id: str,
        messages: List[Dict[str, str]] = None,
        input_text: str = None,
        system_prompt: str = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        tools: List[Dict] = None,
        tool_choice: str = "auto"
    ) -> Dict[str, Any]:
        """Send a chat request to the v1 API with optional tools"""
        if not messages:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            if input_text:
                messages.append({"role": "user", "content": input_text})

        payload = {
            "model": model_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        try:
            # RAG Integration Optimization: Increased timeout for large context processing
            async with httpx.AsyncClient(headers=self.headers, timeout=300.0) as client:
                # Chat uses OpenAI-compatible endpoint
                response = await client.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload
                )
                response.raise_for_status()
                data = response.json()

                choice = data["choices"][0]
                message = choice["message"]

                # Handle tool calls if present
                if message.get("tool_calls"):
                    return await self._handle_tool_calls(model_id, messages, message["tool_calls"], tools)

                return {
                    "content": message.get("content", ""),
                    "role": "assistant"
                }
        except httpx.HTTPStatusError as e:
            error_body = e.response.text
            logger.error(f"LM Studio chat failed ({e.response.status_code}): {error_body}")

            # Specific handling for known LM Studio/Inference engine errors
            if "Channel Error" in error_body:
                error_msg = f"Inference engine reported a Channel Error. This usually means the model ({model_id}) is in a bad state or the context window was exceeded."
                logger.error(error_msg)
                return {"error": error_msg, "content": ""}

            return {"error": f"HTTP {e.response.status_code}: {error_body}", "content": ""}
        except Exception as e:
            err_str = str(e)
            logger.error(f"LM Studio chat failed: {err_str}")

            if "Channel Error" in err_str:
                return {"error": f"Channel Error detected in inference: {err_str}", "content": ""}

            return {"error": err_str, "content": ""}

    async def _handle_tool_calls(
        self,
        model_id: str,
        messages: List[Dict],
        tool_calls: List[Dict],
        tools: List[Dict]
    ) -> Dict[str, Any]:
        """Execute tool calls and resume chat session"""
        # Append the assistant's message with tool calls
        messages.append({
            "role": "assistant",
            "tool_calls": tool_calls
        })

        for tool_call in tool_calls:
            function_name = tool_call["function"]["name"]
            arguments = json.loads(tool_call["function"]["arguments"])

            logger.info(f"LM Studio calling tool: {function_name} with {arguments}")

            # Here we would normally call the actual tool
            # For this integration, we link it to the AppState's MCP client or similar
            result = await self._execute_mcp_tool(function_name, arguments)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "name": function_name,
                "content": json.dumps(result)
            })

        # Resubmit with tool results
        return await self.chat(
            model_id=model_id,
            messages=messages,
            tools=tools
        )

    async def _execute_mcp_tool(self, tool_name: str, arguments: Dict) -> Any:
        """Integration hook for MCP tool execution"""
        try:
            # Try to get the MCP client from global AppState if available
            # This avoids circular imports by doing it inside the method
            from app_context import AppState
            # We assume a global AppState or similar exists.
            # In this project, it's usually initialized in main.py or similar.
            # However, since this is a library client, we might need a better way.
            # For now, let's assume we can import it or it's passed in.

            # Generic fallback: if we have registered local functions, use them
            # For Growin App, we specifically want to interface with the Trading212 MCP
            import app_context
            if hasattr(app_context, "state") and app_context.state.mcp_client:
                 logger.info(f"Routing tool call {tool_name} to Trading212 MCP")
                 return await app_context.state.mcp_client.call_tool(tool_name, arguments)

            return {"error": f"Tool execution for {tool_name} not implemented in client"}
        except Exception as e:
            logger.error(f"Tool execution failed: {e}")
            return {"error": str(e)}

    async def download_model(self, model_path: str) -> Dict[str, Any]:
        """Initiate model download via v1 API"""
        try:
            async with httpx.AsyncClient(headers=self.headers) as client:
                response = await client.post(
                    f"{self.base_url}/api/v1/models/download",
                    json={"path": model_path}
                )
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.error(f"LM Studio download failed: {e}")
            return {"error": str(e)}

    async def get_download_status(self, job_id: str) -> Dict[str, Any]:
        """Check status of a download job."""
        return await self._request("GET", f"/api/v1/models/download/status/{job_id}")

    async def ensure_model_loaded(self, model_id: str) -> bool:
        """
        Helper: Check if model is loaded, if not load it.
        Returns True if successful.
        """
        try:
            # 1. Check loaded models
            loaded = await self.list_loaded_models()
            if model_id in loaded:
                return True

            await self.load_model(model_id)
            return True
        except Exception as e:
            logger.error(f"Failed to ensure model {model_id} is loaded: {e}")
            return False


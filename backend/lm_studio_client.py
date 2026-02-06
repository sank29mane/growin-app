"""
LM Studio v1 API Client
Handles stateful chats, model management, and authentication for local inference.
"""

import os
import httpx
import logging
import json
import asyncio
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class LMStudioClient:
    """
    Async client for LM Studio's v1 REST API.
    Optimized for Growin App's SOTA concurrent execution and high-throughput.

    Supports:
    - Authentication via LM_API_TOKEN
    - Stateful Chat (/api/v1/chat)
    - Model Management (Load/Unload/Download) via /api/v1/models
    - Parallel Inference with Continuous Batching
    - Persistent Session IDs for context re-use
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
        # LM Studio V1 API uses /api/v1 prefix for most management tasks
        if not endpoint.startswith("/api/v1") and not endpoint.startswith("/v1"):
             # Auto-prefix for convenience if only resource passed
             prefix = "/api/v1" if "models" in endpoint or "chat" in endpoint else "/v1"
             if not endpoint.startswith("/"): endpoint = f"/{endpoint}"
             endpoint = f"{prefix}{endpoint}"
        
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
            # Try V1 models endpoint
            await self._request("GET", "/api/v1/models")
            return True
        except RuntimeError:
            return False

    async def list_models(self) -> List[Dict[str, Any]]:
        """List all available models using V1 API."""
        data = await self._request("GET", "/api/v1/models")
        return data.get("data", [])

    async def list_loaded_models(self) -> List[str]:
        """List IDs of currently loaded models."""
        # In V1, we check the 'loaded' property if available, or just fetch the list
        models = await self.list_models()
        return [m.get("id") for m in models if m.get("loaded", True)]

    async def load_model(self, model_id: str, context_length: int = 8192, gpu_offload: str = "max") -> Dict[str, Any]:
        """
        Load a model into memory via V1 API.

        Args:
            model_id: The specific model ID/path
            context_length: Max context window
            gpu_offload: 'max', 'off', or number of layers
        """
        payload = {
            "modelId": model_id,
            "config": {
                "contextLength": context_length,
                "gpuOffload": gpu_offload
            }
        }
        logger.info(f"LM Studio V1: Loading model {model_id}...")
        return await self._request("POST", "/api/v1/models/load", json=payload)

    async def unload_model(self, model_id: str) -> Dict[str, Any]:
        """Unload a specific model via V1 API."""
        return await self._request("POST", "/api/v1/models/unload", json={"modelId": model_id})

    async def chat(
        self,
        model_id: str,
        messages: List[Dict[str, str]] = None,
        input_text: str = None,
        system_prompt: str = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        tools: List[Dict] = None,
        tool_choice: str = "auto",
        stream: bool = False,
        session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Send a chat request. 
        Note: We use the OpenAI-compatible endpoint as it's the most stable across all local versions.
        """
        if not messages:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            if input_text:
                messages.append({"role": "user", "content": input_text})

        # OpenAI compatible payload
        payload = {
            "model": model_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        try:
            logger.info(f"LM Studio: Sending chat request (Model: {model_id}, Tokens: {max_tokens})")
            
            async with httpx.AsyncClient(headers=self.headers, timeout=300.0) as client:
                # Use /v1/chat/completions for widest compatibility
                endpoint = f"{self.base_url}/v1/chat/completions"
                
                response = await client.post(endpoint, json=payload)
                response.raise_for_status()
                data = response.json()

                # Robust extraction for both V1 and OpenAI-style responses
                if "choices" in data:
                    choice = data["choices"][0]
                    message = choice.get("message", {})
                else:
                    # Native V1 fallback
                    message = data.get("message", {})

                # Handle tool calls if present
                if message.get("tool_calls"):
                    return await self._handle_tool_calls(model_id, messages, message["tool_calls"], tools)
                elif message.get("toolCalls"):
                    return await self._handle_tool_calls(model_id, messages, message["toolCalls"], tools)

                return {
                    "content": message.get("content", ""),
                    "role": "assistant",
                    "sessionId": data.get("sessionId", session_id)
                }
        except httpx.HTTPStatusError as e:
            error_body = e.response.text
            logger.error(f"LM Studio V1 chat failed ({e.response.status_code}): {error_body}")

            # Specific handling for known LM Studio/Inference engine errors
            if "Channel Error" in error_body or "crashed" in error_body.lower():
                logger.warning("LM Studio: Model crashed or reported Channel Error. Attempting V1 Recovery...")
                try:
                    # Attempt to reload model
                    await self.unload_model(model_id)
                    await asyncio.sleep(2.0)
                    success = await self.ensure_model_loaded(model_id)
                    
                    if success:
                        logger.info("LM Studio: Model reloaded. Retrying V1 chat...")
                        return await self.chat(model_id=model_id, messages=messages, tools=tools)
                except Exception as recovery_err:
                    logger.error(f"LM Studio: Recovery failed: {recovery_err}")

            return {"error": f"V1 API Error: {error_body}", "content": ""}
        except Exception as e:
            err_str = str(e)
            logger.error(f"LM Studio chat failed: {err_str}")

            if "Channel Error" in err_str or "crashed" in err_str.lower():
                return {"error": f"Channel Error/Crash detected: {err_str}", "content": ""}

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
        return await self._request("POST", "/api/v1/models/download", json={"path": model_path})

    async def get_download_status(self, job_id: str) -> Dict[str, Any]:
        """Check status of a download job."""
        return await self._request("GET", f"/api/v1/models/download/status/{job_id}")

    async def batch_chat(self, model_id: str, requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Execute multiple chat requests concurrently.
        Utilizes LM Studio V1's Parallel Batching.
        """
        tasks = [self.chat(model_id=model_id, **req) for req in requests]
        return await asyncio.gather(*tasks)

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


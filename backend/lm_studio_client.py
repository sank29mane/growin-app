"""
LM Studio v1 API Client
Handles stateful chats, model management, and authentication for local inference.
"""

import os
import httpx
import logging
import json
import asyncio
import psutil
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class LMStudioClient:
    """
    Async client for LM Studio's v1 REST API (0.4.x Stable).
    Optimized for Growin App's SOTA concurrent execution and high-throughput.

    Supports:
    - Authentication via LM_API_TOKEN
    - Stateful Chat (/api/v1/chat) with server-side context (response_id)
    - Stateless Chat (/v1/chat/completions) for OpenAI compatibility
    - Model Management (Load/Unload/Download) via Native V1 Management API
    - Parallel Inference with Continuous Batching (Semaphore-guarded)
    - Content-Based Prefix Caching for TTFT reduction
    """

    BASE_URL = "http://127.0.0.1:1234"

    def __init__(self, base_url: str = None, api_token: str = None):
        self.base_url = base_url or os.getenv("LM_STUDIO_URL", self.BASE_URL)
        self.api_token = api_token or os.getenv("LM_API_TOKEN")
        self.active_model_id: Optional[str] = None

        self.headers = {
            "Content-Type": "application/json"
        }
        if self.api_token:
            self.headers["Authorization"] = f"Bearer {self.api_token}"

        self._setup_concurrency_limits()

    def _setup_concurrency_limits(self):
        """
        SOTA: Implement 60% RAM Rule for M4 Pro/Max memory bandwidth safety.
        Prevents SSD swapping during heavy parallel multi-agent bursts.
        """
        total_ram_gb = psutil.virtual_memory().total / (1024**3)
        
        # Rule: Use only 60% of RAM for LLM tasks to leave room for OS/Apps
        safe_ram_limit = total_ram_gb * 0.6
        
        # Heuristics for M4 Pro optimizations
        # Typical model size (Q4_K_M or Q8) + KV Cache overhead
        estimated_model_size = 8.0  # GB
        kv_cache_per_request = 0.5  # GB (8k context)
        
        concurrency = int((safe_ram_limit - estimated_model_size) / kv_cache_per_request)
        self.max_concurrent_predictions = max(1, min(concurrency, 16)) # Cap at 16 for stability
        
        self.semaphore = asyncio.Semaphore(self.max_concurrent_predictions)
        logger.info(f"LM Studio: Memory Guard active. Total RAM: {total_ram_gb:.1f}GB. Slots: {self.max_concurrent_predictions}")

    async def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        """Internal helper for making requests with error handling."""
        if not path.startswith("/"):
            path = f"/{path}"
            
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.request(method, url, headers=self.headers, **kwargs)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"LM Studio API Error ({e.response.status_code}) at {path}: {e.response.text}")
                raise RuntimeError(f"LM Studio API Error: {e.response.text}") from e
            except httpx.RequestError as e:
                logger.error(f"LM Studio Connection Error at {path}: {e}")
                raise RuntimeError(f"Could not connect to LM Studio at {self.base_url}") from e

    async def check_connection(self) -> bool:
        """Verify server is reachable and running."""
        try:
            # Try V1 models endpoint (management)
            await self._request("GET", "/api/v1/models")
            return True
        except Exception:
            try:
                # Fallback OpenAI compat
                await self._request("GET", "/v1/models")
                return True
            except Exception:
                return False

    async def list_models(self, management: bool = False) -> List[Dict[str, Any]]:
        """
        List all available models.
        management=True uses /api/v1/models (Native management info).
        management=False uses /v1/models (OpenAI compat info).
        """
        path = "/api/v1/models" if management else "/v1/models"
        try:
            data = await self._request("GET", path)
            # Native V1 uses 'models' key, OpenAI uses 'data' key
            return data.get("models") if management else data.get("data", [])
        except Exception as e:
            logger.error(f"LM Studio: Failed to list models via {path}: {e}")
            return []

    async def list_loaded_models(self) -> List[str]:
        """List IDs of currently loaded models using Native V1 API."""
        # In LM Studio 0.4.x+, a model is loaded if 'loaded_instances' has entries
        models = await self.list_models(management=True)
        
        # SOTA: Use 'key' for Native V1 and fallback to 'id'
        loaded_ids = [
            (m.get("key") or m.get("id")) for m in models 
            if m.get("loaded_instances") and len(m.get("loaded_instances", [])) > 0
        ]
        
        logger.info(f"LM Studio: Detected {len(loaded_ids)} loaded models via Native V1 API.")
        return loaded_ids

    async def load_model(self, model_id: str, context_length: int = 8192, gpu: str = "max") -> Dict[str, Any]:
        """
        Load a model into memory via Native V1 API.
        Payload matches 01-LM_STUDIO_API_RESEARCH.md (flat structure), minus 'gpu' which is unrecognized in 0.4.6.
        """
        payload = {
            "model": model_id,
            "context_length": context_length
        }
        logger.info(f"LM Studio V1 (0.4.6): Sending load request for {model_id}...")
        return await self._request("POST", "/api/v1/models/load", json=payload)

    async def unload_model(self, instance_id: str) -> Dict[str, Any]:
        """
        Unload a specific model instance via Native V1 API.
        """
        return await self._request("POST", "/api/v1/models/unload", json={"instance_id": instance_id})

    async def chat(
        self,
        model_id: Optional[str] = None,
        messages: List[Dict[str, str]] = None,
        input_text: str = None,
        system_prompt: str = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        tools: List[Dict] = None,
        tool_choice: str = "auto",
        stream: bool = False,
        session_id: Optional[str] = None,
        enable_thinking: bool = True,
        truncate_thinking: bool = True
    ) -> Dict[str, Any]:
        """
        Send a chat request. 
        Note: We use the OpenAI-compatible endpoint as it's the most stable across all local versions.
        """
        # SOTA: Auto-detect active model if not provided
        model_id = model_id or self.active_model_id
        if not model_id:
             raise ValueError("No model_id provided or active_model_id set on client.")

        if not messages:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            if input_text:
                messages.append({"role": "user", "content": input_text})

        # OpenAI compatible payload with 0.4.0+ specialized fields
        # SOTA: cache_prompt=True enables Content-Based Prefix Caching for TTFT reduction
        payload = {
            "model": model_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
            "cache_prompt": True,
            "extra_fields": {
                "nvidia.nemotron3Nano.enableThinking": enable_thinking,
                "nvidia.nemotron3Nano.truncateThinkingHistory": truncate_thinking
            }
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        async with self.semaphore:
            try:
                logger.info(f"LM Studio: Sending chat request (Model: {model_id}, Slots: {self.semaphore._value})")
                
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

                    # Handle reasoning field (for R1/Reasoning models in LM Studio)
                    content = message.get("content", "")
                    reasoning = message.get("reasoning", "")
                    
                    # If content is empty but reasoning has content, use reasoning
                    if not content and reasoning:
                        content = reasoning

                    # Handle tool calls if present
                    if message.get("tool_calls"):
                        return await self._handle_tool_calls(model_id, messages, message["tool_calls"], tools)
                    elif message.get("toolCalls"):
                        return await self._handle_tool_calls(model_id, messages, message["toolCalls"], tools)

                    return {
                        "content": content,
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

    async def stateful_chat(
        self,
        model_id: str,
        input_text: str,
        previous_response_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        store: bool = True,
        reasoning: str = "auto"
    ) -> Dict[str, Any]:
        """
        Send a stateful chat request to LM Studio Native V1 API.
        Maintains context on the server side using response_id.
        """
        payload = {
            "model": model_id,
            "input": input_text,
            "store": store,
            "temperature": temperature
        }
        
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        if system_prompt:
            payload["system_prompt"] = system_prompt
        if reasoning != "auto":
            payload["reasoning"] = reasoning

        async with self.semaphore:
            try:
                logger.info(f"LM Studio: Stateful Chat (Model: {model_id}, Prev: {previous_response_id})")
                data = await self._request("POST", "/api/v1/chat", json=payload)
                
                # Extract results from V1 structured output
                output_items = data.get("output", [])
                content = ""
                reasoning_content = ""
                
                for item in output_items:
                    if item.get("type") == "message":
                        content += item.get("content", "")
                    elif item.get("type") == "reasoning":
                        reasoning_content += item.get("content", "")
                
                return {
                    "content": content,
                    "reasoning": reasoning_content,
                    "response_id": data.get("response_id"),
                    "model_instance_id": data.get("model_instance_id"),
                    "stats": data.get("stats", {})
                }
            except Exception as e:
                logger.error(f"LM Studio stateful chat failed: {e}")
                return {"error": str(e), "content": ""}

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

    async def ensure_model_loaded(self, model_id: str, context_length: int = 8192, gpu: str = "max") -> bool:
        """
        Helper: Check if model is loaded, if not load it.
        Returns True if successful.
        """
        try:
            # 1. Check loaded models
            loaded = await self.list_loaded_models()
            if model_id in loaded:
                logger.info(f"LM Studio: Model {model_id} is already loaded.")
                return True

            logger.info(f"LM Studio: Model {model_id} not loaded. Triggering load...")
            await self.load_model(model_id, context_length=context_length, gpu=gpu)
            return True
        except Exception as e:
            logger.error(f"Failed to ensure model {model_id} is loaded: {e}")
            return False


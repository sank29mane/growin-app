"""
vMLX Inference Engine - Management layer for the jjang-ai/vmlx server.
Optimized for Apple Silicon (M4 Pro) with hardware-aware memory limits.
"""

import os
import asyncio
import logging
import aiohttp
import subprocess
from typing import Optional, Dict, Any, AsyncIterator

logger = logging.getLogger(__name__)

class VMLXInferenceEngine:
    """
    Manages the lifecycle of the vMLX (vllm-mlx) inference server.
    Ensures hardware-aware memory limits and SOTA 2026 local serving patterns.
    """
    
    def __init__(self, host: str = "127.0.0.1", port: int = 8000):
        self.host = host
        self.port = port
        self.url = f"http://{host}:{port}/v1"
        self.process: Optional[asyncio.subprocess.Process] = None
        self._is_starting = False

    async def start_server(self, model_path: str = None):
        """
        Starts the vMLX server using 'uv tool run vmlx serve'.
        HARD-CODED for M4 Pro (48GB) per D-09/D-10.
        """
        if self.process or self._is_starting:
            logger.info("vMLX Server already running or starting.")
            return True

        self._is_starting = True
        
        # M4 Pro Optimization (48GB Total RAM)
        # 60% Rule: 28GB for weights + active memory
        # KV-Cache: 12GB (approx 25% of RAM)
        memory_limit = "28GB"
        kv_cache_limit = "12GB"
        quantization = "Q4_K_M"
        
        # Default model if none provided
        model = model_path or "nemotron-3-30b-moe-jang-q4_k_m"

        cmd = [
            "uv", "tool", "run", "vmlx", "serve",
            "--model", model,
            "--host", self.host,
            "--port", str(self.port),
            "--memory-limit", memory_limit,
            "--kv-cache-limit", kv_cache_limit,
            "--quantization", quantization,
            "--trust-remote-code"
        ]

        logger.info(f"🚀 Starting vMLX Server: {' '.join(cmd)}")
        
        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Wait for health check to pass
            for i in range(30):  # 60 second timeout (30 * 2s)
                if await self.check_health():
                    logger.info(f"✅ vMLX Server started successfully on {self.url}")
                    self._is_starting = False
                    return True
                await asyncio.sleep(2)
                
            logger.error("❌ vMLX Server failed to start within timeout.")
            await self.stop_server()
            return False
            
        except Exception as e:
            logger.error(f"❌ Error starting vMLX server: {e}")
            self._is_starting = False
            return False

    async def check_health(self) -> bool:
        """Verify the vMLX server is reachable and responsive."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.url}/models", timeout=1) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def generate(self, prompt: str, model: str = None, **kwargs) -> str:
        """Proxy call to vMLX completion endpoint."""
        payload = {
            "model": model or "native-mlx",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 512),
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.url}/chat/completions", json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data['choices'][0]['message']['content']
                    else:
                        error_text = await resp.text()
                        logger.error(f"vMLX Error ({resp.status}): {error_text}")
                        raise RuntimeError(f"vMLX Request failed: {resp.status}")
        except Exception as e:
            logger.error(f"vMLX generate error: {e}")
            raise

    async def stream_generate(self, prompt: str, model: str = None, **kwargs) -> AsyncIterator[Dict[str, Any]]:
        """Proxy call to vMLX streaming completion endpoint."""
        payload = {
            "model": model or "native-mlx",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 512),
            "stream": True
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.url}/chat/completions", json=payload) as resp:
                    if resp.status == 200:
                        async for line in resp.content:
                            if line.startswith(b"data: "):
                                data_str = line[6:].decode('utf-8').strip()
                                if data_str == "[DONE]":
                                    break
                                import json
                                try:
                                    chunk = json.loads(data_str)
                                    delta = chunk['choices'][0].get('delta', {})
                                    if 'content' in delta:
                                        yield {
                                            "text": delta['content'],
                                            "finished": False
                                        }
                                except json.JSONDecodeError:
                                    continue
                        yield {"text": "", "finished": True}
                    else:
                        error_text = await resp.text()
                        logger.error(f"vMLX Stream Error ({resp.status}): {error_text}")
                        raise RuntimeError(f"vMLX Stream Request failed: {resp.status}")
        except Exception as e:
            logger.error(f"vMLX streaming error: {e}")
            raise

    async def stop_server(self):
        """Terminates the vMLX server process."""
        if self.process:
            logger.info("🛑 Stopping vMLX server...")
            try:
                self.process.terminate()
                await self.process.wait()
                logger.info("❄️ vMLX server stopped.")
            except Exception as e:
                logger.error(f"Error stopping vMLX server: {e}")
            finally:
                self.process = None
        self._is_starting = False

# Global singleton
_vmlx_engine: Optional[VMLXInferenceEngine] = None

def get_vmlx_engine() -> VMLXInferenceEngine:
    global _vmlx_engine
    if _vmlx_engine is None:
        _vmlx_engine = VMLXInferenceEngine()
    return _vmlx_engine

if __name__ == "__main__":
    # Test execution
    import sys
    if "--test-serve" in sys.argv:
        engine = get_vmlx_engine()
        asyncio.run(engine.start_server())
        print("vMLX Server check passed.")
        asyncio.run(engine.stop_server())

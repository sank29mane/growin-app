"""
Worker Client - Asynchronous interface to the long-running Model Worker service.
"""

import os
import sys
import json
import asyncio
import logging
from typing import Dict, Any, Optional, List
from pathlib import Path

logger = logging.getLogger("WorkerClient")

class WorkerClient:
    """
    Manages a long-running subprocess of worker_service.py.
    """
    
    def __init__(self):
        self.process: Optional[asyncio.subprocess.Process] = None
        self.lock = asyncio.Lock()
        self.io_lock = asyncio.Lock()
        self._worker_path = str(Path(__file__).parent.absolute() / "worker_service.py")
        self._python_exe = sys.executable

    async def _ensure_worker(self):
        """Start the worker process if not already running"""
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                logger.info(f"🚀 Starting Model Worker Service: {self._python_exe} {self._worker_path}")
                
                self.process = await asyncio.create_subprocess_exec(
                    self._python_exe, self._worker_path,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                # Start a task to log stderr from the worker
                asyncio.create_task(self._read_stderr())
                
                # Verify start with a ping
                try:
                    await self._send_request({"action": "ping"}, timeout=5.0)
                    logger.info("✅ Model Worker Service is online")
                except Exception as e:
                    logger.error(f"Failed to ping worker service: {e}")
                    await self.stop()
                    raise

    async def _read_stderr(self):
        """Log stderr from the worker process to the main logger"""
        if not self.process or not self.process.stderr:
            return
            
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                logger.debug(f"[Worker] {line.decode().strip()}")
        except Exception:
            pass

    async def _send_request(self, request: Dict[str, Any], timeout: float = 60.0) -> Dict[str, Any]:
        """Send a JSON request and wait for a JSON response"""
        if self.process is None or self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("Worker process not initialized")
            
        async with self.io_lock:
            try:
                # Encode request
                req_json = json.dumps(request) + "\n"
                self.process.stdin.write(req_json.encode())
                await self.process.stdin.drain()
                
                # Read response
                line = await asyncio.wait_for(self.process.stdout.readline(), timeout=timeout)
                if not line:
                    raise RuntimeError("Worker process closed unexpectedly")
                    
                return json.loads(line.decode())
            except asyncio.TimeoutError:
                logger.error(f"Timeout waiting for worker response (action: {request.get('action')})")
                raise
            except Exception as e:
                logger.error(f"Error communicating with worker: {e}")
                raise

    async def load_mlx_model(self, model_path: str, quantize: bool = True) -> bool:
        """Tell the worker to load an MLX model"""
        await self._ensure_worker()
        response = await self._send_request({
            "action": "load_mlx",
            "model_path": model_path,
            "quantize": quantize
        })
        return response.get("status") == "success"

    async def generate_mlx(self, prompt: str, max_tokens: int = 512, temperature: float = 0.7) -> str:
        """Perform text generation via the worker"""
        await self._ensure_worker()
        response = await self._send_request({
            "action": "generate_mlx",
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": temperature
        })
        
        if "response" in response:
            return response["response"]
        raise RuntimeError(response.get("error", "Unknown MLX generation error"))

    async def load_jmce_model(self, n_assets: int = 50, resolution: str = "daily") -> bool:
        """Tell the worker to load the Neural JMCE model"""
        await self._ensure_worker()
        response = await self._send_request({
            "action": "load_jmce",
            "n_assets": n_assets,
            "resolution": resolution
        })
        return response.get("status") == "success"

    async def load_ttm_model(self, model_id: str = "ibm-granite/granite-timeseries-ttm-r2") -> bool:
        """Tell the worker to pin the TTM model in memory"""
        await self._ensure_worker()
        response = await self._send_request({
            "action": "load_ttm",
            "model_id": model_id
        })
        return response.get("status") == "success"

    async def forecast_fused(self, ohlcv_data: List[Dict[str, Any]], prediction_steps: int = 96, timeframe: str = "1Hour", returns_data: Optional[List[List[float]]] = None) -> Dict[str, Any]:
        """Perform Fused TTM-JMCE forecasting via the worker"""
        await self._ensure_worker()
        response = await self._send_request({
            "action": "forecast_fused",
            "ohlcv_data": ohlcv_data,
            "prediction_steps": prediction_steps,
            "timeframe": timeframe,
            "returns_data": returns_data
        }, timeout=45.0)
        
        return response

    async def get_status(self) -> Dict[str, Any]:
        """Get the current status of the worker"""
        if self.process is None:
            return {"status": "offline"}
        try:
            return await self._send_request({"action": "status"}, timeout=2.0)
        except Exception:
            return {"status": "error"}

    async def stop(self):
        """Shut down the worker process"""
        if self.process:
            try:
                async with self.lock:
                    if self.process.stdin:
                        self.process.stdin.write(json.dumps({"action": "shutdown"}).encode() + b"\n")
                        await self.process.stdin.drain()
                    await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except Exception:
                if self.process:
                    self.process.kill()
            finally:
                self.process = None

# Global singleton
_worker_client: Optional[WorkerClient] = None

def get_worker_client() -> WorkerClient:
    """Get the global worker client instance"""
    global _worker_client
    if _worker_client is None:
        _worker_client = WorkerClient()
    return _worker_client

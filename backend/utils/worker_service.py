"""
Worker Service - Long-running process to keep high-fidelity models resident in memory.
Supports MLX (LFM/Granite) and TTM-R2 (via TS-FM) pinning.
"""

import os
import sys
import json
import logging
import time
import threading
from typing import Dict, Any, Optional, List
from pathlib import Path

# Add parent directory to path to allow imports from backend
sys.path.append(str(Path(__file__).parent.parent.absolute()))

from utils.process_guard import start_parent_watchdog
from utils.memory_profiler import MemoryProfiler

# Configure logging to stderr
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("WorkerService")

class ModelWorker:
    """
    Manages resident models in memory to avoid reload latency.
    SOTA 2026: Supports fused TTM-JMCE execution on GPU/MLX.
    """
    
    def __init__(self):
        self.mlx_engine = None
        self.ttm_model = None
        self.ttm_pipeline = None
        self.jmce_model = None
        self.loaded_models: Dict[str, Any] = {}
        self.memory_profiler = MemoryProfiler(threshold_mb=32768.0) # 32GB warning for M4 Pro
        self.is_running = True
        
    def _get_mlx_engine(self):
        if self.mlx_engine is None:
            from mlx_engine import get_mlx_engine
            self.mlx_engine = get_mlx_engine()
        return self.mlx_engine

    def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Route requests to appropriate handlers"""
        action = request.get("action")
        
        try:
            if action == "load_mlx":
                return self._load_mlx(request)
            elif action == "load_jmce":
                return self._load_jmce(request)
            elif action == "load_ttm":
                return self._load_ttm(request)
            elif action == "generate_mlx":
                return self._generate_mlx(request)
            elif action == "forecast_ttm":
                return self._forecast_ttm(request)
            elif action == "forecast_fused":
                return self._forecast_fused(request)
            elif action == "status":
                return self._get_status()
            elif action == "ping":
                return {"status": "pong", "timestamp": time.time()}
            elif action == "shutdown":
                self.is_running = False
                return {"status": "shutting_down"}
            else:
                return {"error": f"Unknown action: {action}"}
        except Exception as e:
            logger.error(f"Error handling {action}: {e}", exc_info=True)
            return {"error": str(e)}

    def _load_mlx(self, request: Dict[str, Any]) -> Dict[str, Any]:
        model_path = request.get("model_path")
        quantize = request.get("quantize", True)
        
        engine = self._get_mlx_engine()
        success = engine.load_model(model_path, quantize_8bit=quantize)
        
        if success:
            self.loaded_models["mlx"] = model_path
            return {"status": "success", "model": model_path}
        else:
            return {"status": "error", "message": "Failed to load MLX model"}

    def _load_jmce(self, request: Dict[str, Any]) -> Dict[str, Any]:
        from utils.jmce_model import get_jmce_model, TimeResolution
        
        n_assets = request.get("n_assets", 50)
        res_str = request.get("resolution", "daily")
        res = TimeResolution(res_str)
        
        # SOTA: Always use GPU/MLX for resident JMCE correction to enable fusion
        self.jmce_model = get_jmce_model(n_assets=n_assets, use_ane=False, resolution=res)
        self.loaded_models["jmce"] = f"NeuralJMCE-{res_str}"
        return {"status": "success"}

    def _load_ttm(self, request: Dict[str, Any]) -> Dict[str, Any]:
        from tsfm_public.models.tinytimemixer import TinyTimeMixerForPrediction
        
        model_id = request.get("model_id", "ibm-granite/granite-timeseries-ttm-r2")
        logger.info(f"Pinning TTM model: {model_id}")
        
        self.ttm_model = TinyTimeMixerForPrediction.from_pretrained(
            model_id,
            context_length=512,
            prediction_length=96
        )
        self.ttm_model.eval()
        self.loaded_models["ttm"] = model_id
        return {"status": "success"}

    def _generate_mlx(self, request: Dict[str, Any]) -> Dict[str, Any]:
        import asyncio
        prompt = request.get("prompt")
        max_tokens = request.get("max_tokens", 512)
        temp = request.get("temperature", 0.7)
        
        engine = self._get_mlx_engine()
        if not engine.is_loaded():
            return {"error": "MLX model not loaded"}
        
        loop = asyncio.get_event_loop()
        response = loop.run_until_complete(engine.generate(
            prompt, 
            max_tokens=max_tokens, 
            temperature=temp
        ))
        
        return {"status": "success", "response": response}

    def _forecast_ttm(self, request: Dict[str, Any]) -> Dict[str, Any]:
        from forecast_bridge import run_forecast
        ohlcv = request.get("ohlcv_data", [])
        steps = request.get("prediction_steps", 96)
        timeframe = request.get("timeframe", "1Hour")
        ticker = request.get("ticker")
        return run_forecast(ohlcv, steps, timeframe=timeframe, ticker=ticker)

    def _forecast_fused(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        SOTA 2026: Fused TTM-JMCE execution.
        Zero-copy memory sharing on GPU via MLX.
        """
        from utils.mlx_loader import mx
        import numpy as np
        
        # 1. Run standard TTM
        base_result = self._forecast_ttm(request)
        if not base_result.get("success"):
            return base_result
            
        # 2. Check if JMCE is available for correction
        if self.jmce_model is None:
            logger.warning("JMCE model not loaded for fused correction. Returning base TTM.")
            return base_result
            
        try:
            # 3. Extract residuals from TTM result
            residuals = base_result.get("debug_scaling", {}).get("residuals")
            if not residuals:
                return base_result
                
            # 4. Prepare JMCE input (Zero-Copy Intent)
            # TTM residuals are converted to mx.array for GPU processing
            res_array = mx.array(np.array(residuals).astype(np.float32)).reshape(1, 96, -1)
            
            # Dummy return sequences for JMCE input (Actual returns should be passed in request)
            # For now, we use the residuals themselves as a proxy or expect 'returns' in request
            returns = request.get("returns_data", residuals)
            ret_array = mx.array(np.array(returns).astype(np.float32)).reshape(1, -1, res_array.shape[2])
            
            # 5. Execute JMCE Correction
            # MLX fuses these operations automatically
            mu, L, _ = self.jmce_model(ret_array, error_vector=res_array)
            
            # 6. Apply Bounded Correction to Forecast
            # JMCE mu acts as an additive shift, bounded by tanh
            correction_shift = np.array(mx.tanh(mu).flatten()) * 0.02 # Max 2% shift
            
            forecast = base_result["forecast"]
            for i, bar in enumerate(forecast[:len(correction_shift)]):
                bar["close"] *= (1.0 + correction_shift[i])
                bar["high"] *= (1.0 + correction_shift[i])
                bar["low"] *= (1.0 + correction_shift[i])
                bar["open"] *= (1.0 + correction_shift[i])
                
            base_result["note"] += " | Neural JMCE Corrected (Fused GPU/MLX)"
            base_result["jmce_uncertainty"] = np.array(self.jmce_model.get_covariance(L).flatten()).tolist()
            
            return base_result
            
        except Exception as e:
            logger.error(f"Fused correction failed: {e}", exc_info=True)
            return base_result

    def _get_status(self) -> Dict[str, Any]:
        mem_stats = self.memory_profiler.get_memory_usage()
        return {
            "status": "online",
            "loaded_models": self.loaded_models,
            "memory": mem_stats,
            "pid": os.getpid()
        }

def main():
    """Main loop for the worker service"""
    # 1. Start parent watchdog
    start_parent_watchdog(interval=5.0)
    
    worker = ModelWorker()
    logger.info(f"🚀 Worker Service started (PID: {os.getpid()})")
    
    # Simple JSON-over-stdin/stdout protocol
    while worker.is_running:
        line = sys.stdin.readline()
        if not line:
            break
            
        try:
            request = json.loads(line)
            response = worker.handle_request(request)
            
            # Sanitize response for JSON
            def sanitize(obj):
                import math
                if isinstance(obj, float):
                    return 0.0 if math.isnan(obj) or math.isinf(obj) else obj
                if isinstance(obj, dict):
                    return {k: sanitize(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [sanitize(v) for v in obj]
                return obj
            
            sys.stdout.write(json.dumps(sanitize(response), default=str) + "\n")
            sys.stdout.flush()
        except json.JSONDecodeError:
            logger.error("Failed to decode JSON request")
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")

if __name__ == "__main__":
    main()

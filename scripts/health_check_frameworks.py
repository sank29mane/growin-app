import asyncio
import time
import gc
import psutil
import logging
import sys
import os
from typing import Dict, Any

# Add backend to path
sys.path.append(os.path.abspath("backend"))

try:
    import mlx.core as mx
    HAS_MLX = True
except ImportError:
    HAS_MLX = False

from vmlx_manager import get_vmlx_engine
from vllm_engine import get_vllm_engine
# from mlx_vlm_engine import get_vlm_engine # Skip if no small model found

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger("health-check")

def get_mem_usage_gb():
    mem = psutil.virtual_memory()
    return mem.used / (1024**3)

def get_active_vram_gb():
    if HAS_MLX:
        return mx.metal.get_active_memory() / (1024**3)
    return 0.0

async def verify_vmlx():
    logger.info("\n--- 🟢 Testing vMLX Framework ---")
    engine = get_vmlx_engine()
    # Use Gemma-4 26B as specified by the user
    model = "dealignai/Gemma-4-26B-A4B-JANG_2L-CRACK"
    
    baseline = get_mem_usage_gb()
    logger.info(f"Baseline System Memory: {baseline:.2f} GB")
    
    logger.info(f"Loading {model} (vMLX)...")
    # vMLX manager will use its internal hardcoded limits for M4 Pro
    success = await engine.start_server(model_path=model)
    if not success:
        logger.error("❌ vMLX failed to load model.")
        return False
    
    loaded = get_mem_usage_gb()
    logger.info(f"Loaded System Memory: {loaded:.2f} GB (Delta: {loaded-baseline:.2f} GB)")
    
    logger.info("Verifying inference...")
    try:
        resp = await engine.generate("Explain the impact of liquidity on leveraged ETFs in one sentence.", max_tokens=32)
        logger.info(f"Inference response: {repr(resp)}")
    except Exception as e:
        logger.error(f"❌ vMLX Inference failed: {e}")
        return False
    
    logger.info("Unloading vMLX...")
    await engine.stop_server()
    
    # Allow some time for process cleanup
    await asyncio.sleep(3)
    gc.collect()
    if HAS_MLX:
        mx.metal.clear_cache()
    
    final = get_mem_usage_gb()
    logger.info(f"Final System Memory: {final:.2f} GB (Delta from baseline: {final-baseline:.2f} GB)")
    
    if final - baseline < 2.0: # Allowing a slightly larger buffer for Gemma-4 overhead
        logger.info("✅ vMLX Clean Unload Verified.")
        return True
    else:
        logger.warning("⚠️ vMLX might have a memory leak or orphaned process.")
        return False

async def verify_vllm():
    logger.info("\n--- 🟢 Testing vLLM-MLX Framework ---")
    engine = get_vllm_engine()
    # Use Gemma-4 26B as specified by the user
    model = "dealignai/Gemma-4-26B-A4B-JANG_2L-CRACK"
    
    baseline = get_mem_usage_gb()
    vram_baseline = get_active_vram_gb()
    logger.info(f"Baseline System Memory: {baseline:.2f} GB | VRAM: {vram_baseline:.2f} GB")
    
    logger.info(f"Loading {model} (vLLM)...")
    # vLLM-MLX needs force_mllm=False for Gemma-4 if we only want text health check
    success = await engine.load_model(model, force_mllm=False)
    if not success:
        logger.error("❌ vLLM failed to load model.")
        return False
    
    loaded = get_mem_usage_gb()
    vram_loaded = get_active_vram_gb()
    logger.info(f"Loaded System Memory: {loaded:.2f} GB (Delta: {loaded-baseline:.2f} GB)")
    logger.info(f"Loaded Active VRAM: {vram_loaded:.2f} GB (Delta: {vram_loaded-vram_baseline:.2f} GB)")
    
    logger.info("Verifying inference...")
    try:
        resp = await engine.generate("Explain the impact of liquidity on leveraged ETFs in one sentence.", max_tokens=32)
        logger.info(f"Inference response: {repr(resp)}")
    except Exception as e:
        logger.error(f"❌ vLLM Inference failed: {e}")
        return False
    
    logger.info("Unloading vLLM...")
    await engine.stop()
    
    await asyncio.sleep(2)
    gc.collect()
    if HAS_MLX:
        mx.metal.clear_cache()
        
    final = get_mem_usage_gb()
    vram_final = get_active_vram_gb()
    logger.info(f"Final System Memory: {final:.2f} GB | VRAM: {vram_final:.2f} GB")
    
    if final - baseline < 2.0:
        logger.info("✅ vLLM Clean Unload Verified.")
        return True
    else:
        logger.warning("⚠️ vLLM might have a memory leak.")
        return False

async def main():
    logger.info("🚀 Starting Inference Framework Health Check")
    
    vmlx_ok = await verify_vmlx()
    vllm_ok = await verify_vllm()
    
    logger.info("\n" + "="*40)
    logger.info("FINAL HEALTH CHECK REPORT")
    logger.info(f"vMLX:     {'✅ PASS' if vmlx_ok else '❌ FAIL'}")
    logger.info(f"vLLM-MLX: {'✅ PASS' if vllm_ok else '❌ FAIL'}")
    logger.info("="*40)

if __name__ == "__main__":
    asyncio.run(main())

"""
Verification script for Model Worker Service.
Tests basic connectivity and model residency.
"""

import asyncio
import sys
import os
import logging
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent.absolute() / 'backend'))
sys.path.append(str(Path(__file__).parent.parent.absolute() / "backend"))

from utils.worker_client import get_worker_client
from model_config import DECISION_MODELS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VerifyWorker")

async def test_worker():
    client = get_worker_client()
    
    # 1. Test Ping
    logger.info("Testing ping...")
    status = await client.get_status()
    logger.info(f"Worker status: {status}")
    
    if status.get("status") == "offline":
        # Force start
        await client._ensure_worker()
        status = await client.get_status()
        logger.info(f"Worker status after start: {status}")

    # 2. Test MLX Loading (using the native-mlx path)
    model_path = DECISION_MODELS["native-mlx"]["model_path"]
    logger.info(f"Attempting to load MLX model: {model_path}")
    
    success = await client.load_mlx_model(model_path)
    if success:
        logger.info("✅ MLX Model loaded successfully")
    else:
        logger.error("❌ Failed to load MLX model")
        return

    # 3. Test Status after load
    status = await client.get_status()
    logger.info(f"Worker status after load: {status}")
    
    # 4. Cleanup
    logger.info("Shutting down worker...")
    await client.stop()
    logger.info("✅ Verification complete")

if __name__ == "__main__":
    asyncio.run(test_worker())

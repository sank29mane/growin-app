"""Benchmark script for local OpenAI-compatible API performance (e.g., LM Studio)."""
import asyncio
import time
import json
import logging
import aiohttp
from typing import List, Dict, Any

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("api-benchmark")

# Configuration
API_URL = "http://127.0.0.1:8001/v1/chat/completions"
MODEL_ID = "nvidia-cascade"  # LM Studio often allows any string or uses the currently loaded model

# Benchmark prompts
PROMPTS = [
    "Explain the theory of general relativity in three paragraphs.",
    "Write a Python script for a simple FastAPI server with one GET endpoint.",
    "What are the key technical differences between MLX and PyTorch?",
    "Summarize the main benefits of using vLLM for local LLM inference on macOS."
]

async def stream_request(session: aiohttp.ClientSession, prompt: str) -> Dict[str, Any]:
    """Send a streaming request and measure TTFT and TPS."""
    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 256,
        "temperature": 0.7,
        "stream": True
    }
    
    ttft = 0.0
    start_time = time.perf_counter()
    tokens_count = 0
    full_text = ""
    
    try:
        async with session.post(API_URL, json=payload) as response:
            if response.status != 200:
                text = await response.text()
                logger.error(f"Error from server: {response.status} - {text}")
                return {"error": response.status}
                
            async for line in response.content:
                line = line.decode('utf-8').strip()
                if not line or line == "data: [DONE]":
                    continue
                    
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    delta = data['choices'][0].get('delta', {})
                    content = delta.get('content', '')
                    
                    if content:
                        if ttft == 0.0:
                            ttft = time.perf_counter() - start_time
                        tokens_count += 1  # Approximate tokens as chunks for simplicity
                        full_text += content
                        
            end_time = time.perf_counter()
            total_duration = end_time - start_time
            gen_duration = total_duration - ttft
            
            # Note: tokens_count here is actually "chunks" received. 
            # In LM Studio, usually 1 chunk = 1 token or similar.
            tps = (tokens_count - 1) / gen_duration if gen_duration > 0 else 0
            
            return {
                "ttft": ttft,
                "tps": tps,
                "tokens": tokens_count,
                "duration": total_duration
            }
    except Exception as e:
        logger.error(f"Request failed: {e}")
        return {"error": str(e)}

async def run_sequential_benchmark():
    """Run prompts one by one to measure baseline latency."""
    logger.info("\n--- Starting Sequential API Benchmark ---")
    results = []
    
    async with aiohttp.ClientSession() as session:
        for i, prompt in enumerate(PROMPTS):
            res = await stream_request(session, prompt)
            if "error" not in res:
                logger.info(f"Prompt {i+1}: TTFT={res['ttft']:.3f}s, TPS={res['tps']:.1f}, Tokens={res['tokens']}")
                results.append(res)
            else:
                logger.error(f"Prompt {i+1} failed.")
                
    if not results:
        return None
        
    avg_ttft = sum(r["ttft"] for r in results) / len(results)
    avg_tps = sum(r["tps"] for r in results) / len(results)
    return {"avg_ttft": avg_ttft, "avg_tps": avg_tps}

async def run_concurrent_benchmark():
    """Run all prompts simultaneously to measure throughput."""
    logger.info("\n--- Starting Concurrent API Benchmark (4 requests) ---")
    start_time = time.perf_counter()
    
    async with aiohttp.ClientSession() as session:
        tasks = [stream_request(session, prompt) for prompt in PROMPTS]
        results = await asyncio.gather(*tasks)
        
    end_time = time.perf_counter()
    total_time = end_time - start_time
    
    valid_results = [r for r in results if "error" not in r]
    if not valid_results:
        return None
        
    total_tokens = sum(r["tokens"] for r in valid_results)
    aggregate_tps = total_tokens / total_time
    
    logger.info(f"Aggregate TPS: {aggregate_tps:.1f}")
    logger.info(f"Total time:    {total_time:.2f}s")
    
    return {"agg_tps": aggregate_tps, "time": total_time}

async def main():
    logger.info(f"Connecting to: {API_URL}")
    
    # 1. Warmup
    async with aiohttp.ClientSession() as session:
        await stream_request(session, "Hello.")
        
    # 2. Sequential
    seq_res = await run_sequential_benchmark()
    
    # 3. Concurrent
    conc_res = await run_concurrent_benchmark()
    
    if seq_res and conc_res:
        logger.info("\n" + "="*50)
        logger.info(f"BENCHMARK SUMMARY (LM Studio @ 127.0.0.1:8001)")
        logger.info("-" * 50)
        logger.info(f"Avg TTFT:      {seq_res['avg_ttft']:.3f}s")
        logger.info(f"Sequential TPS: {seq_res['avg_tps']:.1f}")
        logger.info(f"Concurrent TPS: {conc_res['agg_tps']:.1f}")
        logger.info("="*50)
        
        # Save results
        summary_path = ".planning/phases/42-model-performance-comparison/42-SUMMARY.md"
        with open(summary_path, "w") as f:
            f.write("# Phase 42: Model Performance Comparison Summary\n\n")
            f.write("## LIVE Server Benchmark Results (M4 Pro 48GB)\n\n")
            f.write(f"- **Server**: {API_URL}\n")
            f.write(f"- **Avg TTFT**: {seq_res['avg_ttft']:.3f}s\n")
            f.write(f"- **Sequential TPS**: {seq_res['avg_tps']:.1f}\n")
            f.write(f"- **Concurrent TPS**: {conc_res['agg_tps']:.1f}\n\n")
            f.write("## Decision\n\n")
            f.write("The core inference engine will be served via this high-throughput API interface.\n")
            
        logger.info(f"Results exported to {summary_path}")

if __name__ == "__main__":
    asyncio.run(main())

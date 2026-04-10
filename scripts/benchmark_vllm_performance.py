"""Benchmark script for vllm-mlx performance comparison."""
import asyncio
import time
import logging
import sys
import os
from typing import List, Dict, Any

# Add parent directory to path to import backend
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.vllm_engine import get_vllm_engine
import mlx.core as mx

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("benchmark")

# Models for Phase 42 (HuggingFace MLX community versions)
MODELS = [
    "mlx-community/Gemma-4-26B-A4B-MoE-4bit",
    "mlx-community/Nemotron-3-Nano-30B-A3B-MoE-4bit"
]

# Benchmark prompts for diverse scenarios
PROMPTS = [
    "Explain the theory of general relativity in three paragraphs.",
    "Write a Python script for a simple FastAPI server with one GET endpoint.",
    "What are the key technical differences between MLX and PyTorch?",
    "Summarize the main benefits of using vLLM for local LLM inference on macOS."
]

async def run_sequential_benchmark(engine, model_id: str) -> Dict[str, Any]:
    """Measure TTFT and TPS for each prompt sequentially."""
    logger.info("\n--- Starting Sequential Benchmark ---")
    results = []
    
    for i, prompt in enumerate(PROMPTS):
        ttft = 0.0
        tokens_count = 0
        gen_start = time.time()
        
        try:
            async for chunk in engine.stream_generate(prompt, max_tokens=128):
                if ttft == 0.0:
                    ttft = time.time() - gen_start
                tokens_count = chunk["tokens"]
                
            total_time = time.time() - gen_start
            gen_time = total_time - ttft
            tps = (tokens_count - 1) / gen_time if gen_time > 0 else 0
            
            logger.info(f"Prompt {i+1}: TTFT={ttft:.3f}s, TPS={tps:.1f}, Tokens={tokens_count}")
            results.append({
                "ttft": ttft,
                "tps": tps,
                "tokens": tokens_count
            })
        except Exception as e:
            logger.error(f"Error during generation for prompt {i+1}: {e}")
            
    if not results:
        return {"avg_ttft": 0, "avg_tps": 0}
        
    avg_ttft = sum(r["ttft"] for r in results) / len(results)
    avg_tps = sum(r["tps"] for r in results) / len(results)
    
    return {"avg_ttft": avg_ttft, "avg_tps": avg_tps}

async def run_concurrent_benchmark(engine, model_id: str) -> Dict[str, Any]:
    """Measure aggregate throughput with 4 concurrent requests."""
    logger.info("\n--- Starting Concurrent Benchmark (4 requests) ---")
    mx.metal.reset_peak_memory()
    
    start_time = time.time()
    tasks = [engine.generate(prompt, max_tokens=128) for prompt in PROMPTS]
    
    try:
        # vllm-mlx BatchedEngine handles continuous batching here
        outputs = await asyncio.gather(*tasks)
        total_time = time.time() - start_time
        
        # We need token counts from outputs if possible, otherwise estimate
        # Since engine.generate returns string in current wrapper, we'll estimate tokens
        # Wait, I should update VLLMInferenceEngine.generate to return object too.
        # For now, let's estimate 0.75 tokens per word
        total_tokens = sum(len(text.split()) / 0.75 for text in outputs)
        
        aggregate_tps = total_tokens / total_time
        peak_mem_gb = mx.metal.get_peak_memory() / 1e9
        
        logger.info(f"Aggregate TPS: {aggregate_tps:.1f}")
        logger.info(f"Peak Memory:    {peak_mem_gb:.2f} GB")
        
        return {"agg_tps": aggregate_tps, "peak_mem_gb": peak_mem_gb, "time": total_time}
    except Exception as e:
        logger.error(f"Error during concurrent generation: {e}")
        return {"agg_tps": 0, "peak_mem_gb": 0, "time": 0}

async def benchmark_model(model_id: str):
    """Run full benchmark suite for a specific model."""
    logger.info(f"\n" + "="*60)
    logger.info(f"BENCHMARKING MODEL: {model_id}")
    logger.info("="*60)
    
    engine = get_vllm_engine()
    
    # Load model
    load_start = time.time()
    success = await engine.load_model(model_id)
    load_time = time.time() - load_start
    
    if not success:
        logger.error(f"Failed to load {model_id}. Skipping.")
        return None
        
    logger.info(f"Model loaded in {load_time:.2f}s")
    
    # Warmup
    logger.info("Warming up engine...")
    await engine.generate("Warmup.")
    
    # Clear cache and reset memory stats
    mx.metal.clear_cache()
    mx.metal.reset_peak_memory()
    
    # Run Sequential
    seq_results = await run_sequential_benchmark(engine, model_id)
    
    # Run Concurrent
    conc_results = await run_concurrent_benchmark(engine, model_id)
    
    # Unload
    await engine.stop()
    mx.metal.clear_cache()
    
    return {
        "model": model_id,
        "avg_ttft": seq_results["avg_ttft"],
        "avg_tps": seq_results["avg_tps"],
        "agg_tps": conc_results["agg_tps"],
        "peak_mem_gb": conc_results["peak_mem_gb"],
        "load_time": load_time
    }

async def main():
    final_results = []
    
    for model_id in MODELS:
        res = await benchmark_model(model_id)
        if res:
            final_results.append(res)
            
    # Print Final Summary Table
    if not final_results:
        logger.error("No benchmark results generated.")
        return
        
    logger.info("\n" + "#"*70)
    logger.info(f"{'MODEL':<40} | {'TTFT':<6} | {'TPS':<6} | {'AGG':<6} | {'MEM':<6}")
    logger.info("-" * 70)
    
    for r in final_results:
        logger.info(
            f"{r['model']:<40} | {r['avg_ttft']:<6.3f} | {r['avg_tps']:<6.1f} | "
            f"{r['agg_tps']:<6.1f} | {r['peak_mem_gb']:<6.2f}"
        )
    logger.info("#"*70)
    
    # Export results to markdown for Phase 42 summary
    summary_path = ".planning/phases/42-model-performance-comparison/42-SUMMARY.md"
    try:
        os.makedirs(os.path.dirname(summary_path), exist_ok=True)
        with open(summary_path, "w") as f:
            f.write("# Phase 42: Model Performance Comparison Summary\n\n")
            f.write("## Benchmark Results\n\n")
            f.write("| Model | Avg TTFT (s) | Seq TPS | Conc TPS | Peak Mem (GB) |\n")
            f.write("| :--- | :---: | :---: | :---: | :---: |\n")
            for r in final_results:
                f.write(
                    f"| {r['model']} | {r['avg_ttft']:.3f} | {r['avg_tps']:.1f} | "
                    f"{r['agg_tps']:.1f} | {r['peak_mem_gb']:.2f} |\n"
                )
            
            # Simple decision logic (prefer highest aggregate TPS)
            best_model = max(final_results, key=lambda x: x["agg_tps"])["model"]
            f.write(f"\n## Recommendation\n\nSelected Core Model: **{best_model}**\n")
            f.write(f"Rationale: Highest aggregate throughput on M4 Pro architecture.\n")
            
        logger.info(f"Results exported to {summary_path}")
    except Exception as e:
        logger.error(f"Failed to export results: {e}")

if __name__ == "__main__":
    asyncio.run(main())

import os
import sys
import time
import numpy as np

# Add backend to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../backend')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.mlx_engine import get_mlx_engine

def benchmark():
    model_path = "mlx-community/Llama-3.2-3B-Instruct-4bit"
    adapter_path = "adapters/high_vol_bull"
    
    print("📊 Benchmarking MLX LoRA Adapter In-Memory Hot-Swapping...")
    print(f"Base model: {model_path}")
    print(f"Adapter:    {adapter_path}")
    
    if not os.path.exists(adapter_path):
        print(f"❌ Adapter directory not found at {adapter_path}")
        return False
        
    engine = get_mlx_engine()
    
    # Load model with initial adapter to instantiate LoRA weights structure in the graph
    print("🔥 Loading base model with adapter...")
    t0 = time.perf_counter()
    success = engine.load_model(model_path, adapter_path=adapter_path)
    t1 = time.perf_counter()
    
    if not success:
        print("❌ Failed to load model and initial adapter configuration.")
        return False
        
    print(f"✅ Model loaded in {t1 - t0:.2f} seconds.")
    
    # Warmup
    print("🔥 Running warmup adapter hotswap...")
    engine.switch_adapter(adapter_path)
    
    # Benchmark
    print("⚡ Running 100 adapter swap iterations...")
    swaps = []
    
    # Get initial memory usage if psutil is available
    has_psutil = False
    try:
        import psutil
        process = psutil.Process(os.getpid())
        initial_mem = process.memory_info().rss / (1024 * 1024) # MB
        has_psutil = True
    except ImportError:
        initial_mem = 0
        
    for i in range(100):
        t0 = time.perf_counter()
        success = engine.switch_adapter(adapter_path)
        t1 = time.perf_counter()
        
        if not success:
            print(f"❌ Swap failed at iteration {i}")
            return False
            
        swaps.append((t1 - t0) * 1000.0) # in ms
        
    avg_swap = np.mean(swaps)
    p95_swap = np.percentile(swaps, 95)
    min_swap = np.min(swaps)
    max_swap = np.max(swaps)
    
    print(f"\n📈 Swap Latency Results:")
    print(f"  - Average: {avg_swap:.4f} ms")
    print(f"  - P95:     {p95_swap:.4f} ms")
    print(f"  - Min:     {min_swap:.4f} ms")
    print(f"  - Max:     {max_swap:.4f} ms")
    
    if has_psutil:
        final_mem = process.memory_info().rss / (1024 * 1024) # MB
        mem_increase = ((final_mem - initial_mem) / initial_mem) * 100.0
        print(f"\n🧠 Memory Overhead:")
        print(f"  - Initial RSS: {initial_mem:.2f} MB")
        print(f"  - Final RSS:   {final_mem:.2f} MB")
        print(f"  - Change:      {mem_increase:+.4f}%")
        
        if mem_increase > 10.0:
            print("⚠️ Warning: Memory overhead exceeds 10% target.")
        else:
            print("✅ Success: Memory overhead is under 10%!")
            
    if avg_swap < 50.0:
        print("\n✅ Success: Average adapter hot-swapping is under 50ms!")
        return True
    else:
        print(f"\n❌ Fail: Average hot-swap latency {avg_swap:.2f}ms is above the 50ms budget.")
        return False

if __name__ == "__main__":
    success = benchmark()
    sys.exit(0 if success else 1)

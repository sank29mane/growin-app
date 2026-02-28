
import sys
import os

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

def test_mlx_import():
    print("Testing MLX Engine Import...")
    try:
        from mlx_engine import MLXInferenceEngine
        print("✅ MLXInferenceEngine imported successfully")
        
        # Optional: Check if mlx is available
        import mlx.core as mx
        print(f"   MLX Version: {mx.__version__}")
        
    except ImportError as e:
        print(f"❌ Import Failed: {e}")
        print("This is expected if running on non-Apple Silicon or missing deps.")
    except Exception as e:
        print(f"❌ Unexpected Error: {e}")

if __name__ == "__main__":
    test_mlx_import()

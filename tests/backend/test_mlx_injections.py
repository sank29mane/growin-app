
import mlx.core as mx
from backend.utils.mlx_injections import MLX_INJECTIONS

def test_injections():
    # Load injections into local namespace
    for name, code in MLX_INJECTIONS.items():
        exec(code, globals())
    
    print("Testing monte_carlo_sim...")
    paths = monte_carlo_sim(100.0, 0.05, 0.2, 1.0, 1/252, 10)
    print(f"Paths shape: {paths.shape}")
    assert paths.shape == (10, 253) # 1 + 252 steps
    
    print("Testing black_scholes_tensor...")
    price = black_scholes_tensor(100.0, 100.0, 1.0, 0.05, 0.2)
    print(f"BS Price: {price.item()}")
    assert price > 0
    
    print("Testing rsi_mlx...")
    prices = mx.array([100.0, 101.0, 102.0, 101.0, 100.0, 99.0, 98.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0])
    rsi = rsi_mlx(prices, period=5)
    print(f"RSI last 3: {rsi[-3:]}")
    assert len(rsi) == len(prices) - 1

    print("All tests passed!")

if __name__ == "__main__":
    test_injections()

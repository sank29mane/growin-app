import asyncio
import numpy as np
from utils.jmce_model import NeuralJMCE, TimeResolution
from utils.mlx_loader import HAS_MLX, mx

print(f"HAS_MLX: {HAS_MLX}")
print(f"mx: {mx}")

async def test_velocity():
    model = NeuralJMCE(n_assets=1, resolution=TimeResolution.INTRADAY_5MIN)
    rets = np.random.randn(1, 10, 1).astype(np.float32)
    x = np.array(rets)
    _, _, V_mx = model(x, return_velocity=True)
    print("V_mx type:", type(V_mx))

if __name__ == "__main__":
    asyncio.run(test_velocity())

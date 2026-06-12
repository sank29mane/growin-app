import asyncio
import numpy as np
from decimal import Decimal
import sys
import os

# Ensure backend is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../backend')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Force HAS_MLX to False before importing other modules
import utils.mlx_loader
utils.mlx_loader.HAS_MLX = False

from utils.portfolio_analyzer import PortfolioAnalyzer
from utils.jmce_model import TimeResolution

async def run():
    print("Testing PortfolioAnalyzer with HAS_MLX = False...")
    analyzer = PortfolioAnalyzer(n_assets=2, resolution=TimeResolution.DAILY)
    
    # Generate mock returns for 2 assets, 10 days
    returns = np.random.normal(0.0001, 0.01, (10, 2))
    
    # test optimize_weights
    print("Running optimize_weights...")
    signals = {"vix": 18.0, "tnx": 4.0}
    weights = await analyzer.optimize_weights(returns, signals, persona='aggressive')
    print("Weights returned:", weights)
    assert len(weights) == 2
    assert abs(float(sum(weights)) - 1.0) < 1e-4
    
    # test get_covariance_velocity
    print("Running get_covariance_velocity...")
    velocity = await analyzer.get_covariance_velocity(returns)
    print("Velocity returned:", velocity)
    assert velocity is None
    
    print("All tests passed under HAS_MLX = False!")

if __name__ == "__main__":
    asyncio.run(run())

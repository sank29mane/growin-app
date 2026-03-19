import mlx.core as mx
import mlx.nn as nn
import numpy as np
from typing import Dict, Any, Tuple, Optional

class PPOActor(nn.Module):
    """
    3-Layer MLP Action Head (Actor) for Portfolio Optimization.
    Inputs: 64-dim State Vector from RLStateFabricator.
    Outputs: Target Portfolio Weights in range [-1, 1].
    
    Architecture:
    - Linear(64, 128) -> ReLU
    - Linear(128, 128) -> ReLU
    - Linear(128, N_ASSETS) -> Tanh
    """
    def __init__(self, input_dim: int = 64, hidden_dim: int = 128, output_dim: int = 10):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, output_dim),
            nn.Tanh()
        )
        
    def __call__(self, x: mx.array) -> mx.array:
        return self.net(x)

class PPOCritic(nn.Module):
    """
    Value Function Head to estimate state-value V(s).
    Essential for calculating Advantages in PPO.
    """
    def __init__(self, input_dim: int = 64, hidden_dim: int = 128):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 1)
        )
        
    def __call__(self, x: mx.array) -> mx.array:
        return self.net(x)

class RLPolicy(nn.Module):
    """
    PPO Policy Orchestrator optimized for Apple M4 Pro (MLX).
    Integrates Actor and Critic heads.
    """
    def __init__(self, n_assets: int = 10, state_dim: int = 64, hidden_dim: int = 128):
        super().__init__()
        self.actor = PPOActor(state_dim, hidden_dim, n_assets)
        self.critic = PPOCritic(state_dim, hidden_dim)
        self.n_assets = n_assets
        self.log_std = mx.zeros((n_assets,))
        
    def scale_for_gbx(self, weights: mx.array, capital_gbp: float, ticker_prices_gbx: mx.array) -> mx.array:
        """
        Scales policy weights for LSE Leveraged ETFs (GBX precision).
        Converts GBX (pence) to GBP (pounds) internally for correct fractional share routing.
        """
        capital_gbp_array = mx.array([capital_gbp])
        allocations_gbp = weights * capital_gbp_array
        prices_gbp = ticker_prices_gbx / 100.0
        shares = allocations_gbp / prices_gbp
        return shares

    def __call__(self, state: mx.array) -> Tuple[mx.array, mx.array]:
        """
        Forward pass for both Actor and Critic.
        Returns (weights, value).
        """
        raw_weights = self.actor(state)
        weights = mx.clip(raw_weights, 0.0, 1.0) # Clip weights to valid long-only range [0, 1]
        value = self.critic(state)
        return weights, value

    def ppo_objective(
        self, 
        alpha: mx.array, 
        transaction_cost: mx.array, 
        vol_tax: mx.array,
        ratios: mx.array,
        advantages: mx.array,
        epsilon: float = 0.2
    ) -> mx.array:
        """
        Calculates the PPO Objective with custom components.
        Objective = Maximize Cumulative Alpha - (Transaction Cost + Volatility Tax).
        
        Args:
            alpha: Cumulative excess returns.
            transaction_cost: Turnover penalties.
            vol_tax: Drag from high volatility (0.5 * sigma^2).
            ratios: Prob(new_policy) / Prob(old_policy).
            advantages: Estimated advantage from Critic.
            epsilon: PPO clipping range.
        """
        # 1. PPO Clipped Surrogate Objective
        surr1 = ratios * advantages
        surr2 = mx.clip(ratios, 1.0 - epsilon, 1.0 + epsilon) * advantages
        ppo_loss = mx.mean(mx.minimum(surr1, surr2))
        
        # 2. Financial Penalty Term (Alpha Optimization)
        # Net Financial Reward = Alpha - (TransactionCost + VolatilityTax)
        financial_reward = alpha - (transaction_cost + vol_tax)
        
        # The policy aims to maximize both the RL surrogate and the Financial Reward
        # In a training loop, financial_reward would typically be the 'reward' 
        # that drives the 'advantages'. 
        
        return ppo_loss + mx.mean(financial_reward)

    @mx.compile
    def get_action(self, state: mx.array) -> mx.array:
        """
        Fast inference for production/backtesting.
        Optimized for GPU execution.
        """
        return mx.clip(self.actor(state), 0.0, 1.0)

def create_policy(n_assets: int = 10) -> RLPolicy:
    """Factory function for Phase 37 integration."""
    return RLPolicy(n_assets=n_assets)

if __name__ == '__main__':
    # Test for M4 Pro GPU Compatibility
    policy = create_policy(n_assets=10)
    
    # Mock State (64-dim)
    dummy_state = mx.random.normal((1, 64))
    
    # Inference
    weights, val = policy(dummy_state)
    
    # Calculate dummy ppo_objective
    dummy_alpha = mx.array([0.05])
    dummy_tc = mx.array([0.001])
    dummy_vt = mx.array([0.002])
    dummy_ratios = mx.array([1.0])
    dummy_adv = mx.array([0.1])
    
    obj = policy.ppo_objective(dummy_alpha, dummy_tc, dummy_vt, dummy_ratios, dummy_adv)
    
    print(f'Policy Output Range: [{mx.min(weights).item():.4f}, {mx.max(weights).item():.4f}]')
    print(f'Policy Output Shape: {weights.shape}')
    print(f'Value Estimate: {val.item():.4f}')
    print(f'PPO Objective: {obj.item():.4f}')
    
    assert weights.shape == (1, 10), 'Weight output dimension mismatch'
    assert mx.all(weights >= -1.0) and mx.all(weights <= 1.0), 'Tanh range violation'
    print('M4 Pro Action Head Test Passed.')

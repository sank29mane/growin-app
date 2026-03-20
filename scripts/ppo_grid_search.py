import mlx.core as mx
import numpy as np
import json
import os
import time
import argparse
from typing import Dict, List, Any, Tuple
from backend.agents.ppo_agent import PPOAgent
from backend.agents.rl_utils import financial_reward

class SimulatedTradingEnv:
    """Mock environment for LSE Leveraged ETF Rebalancing."""
    def __init__(self, n_assets: int = 10, state_dim: int = 64):
        self.n_assets = n_assets
        self.state_dim = state_dim
        self.current_step = 0
        self.max_steps = 100
        
    def reset(self):
        self.current_step = 0
        return mx.random.normal((1, self.state_dim))
    
    def step(self, action: mx.array):
        self.current_step += 1
        
        # Simulate Alpha (higher if action aligns with 'market trend')
        # In a real grid search, this would use historical data
        alpha = mx.mean(action).item() * 0.01 
        
        # Volatility Tax Calculation (Variance in actions/rebalances)
        variance = mx.var(action).item()
        
        # Institutional Window (2PM GMT Simulation)
        # Reward actions that are 'decisive' during specific steps
        is_2pm = (self.current_step % 20 == 0)
        window_bonus = 0.005 if (is_2pm and mx.max(action).item() > 0.8) else 0.0
        
        # turnover calculation (simplified)
        turnover = mx.sum(mx.abs(action)).item()
        
        reward = financial_reward(alpha + window_bonus, turnover, variance)
        
        next_state = mx.random.normal((1, self.state_dim))
        done = self.current_step >= self.max_steps
        
        return next_state, reward, done, {}

def run_grid_search(smoke_test: bool = False):
    print("🚀 Starting PPO Hyperparameter Grid Search for LSE ETPs")
    
    # Search Space
    lrs = [3e-4, 1e-4]
    epsilons = [0.1, 0.2]
    entropy_coefs = [0.001, 0.01]
    
    if smoke_test:
        lrs = [3e-4]
        epsilons = [0.2]
        entropy_coefs = [0.01]
        print("🧪 Smoke test mode: evaluating 1 combination")

    results = []
    env = SimulatedTradingEnv()
    
    for lr in lrs:
        for eps in epsilons:
            for ent in entropy_coefs:
                print(f"Testing: lr={lr}, eps={eps}, ent={ent}")
                
                agent = PPOAgent(
                    n_assets=10, 
                    state_dim=64, 
                    lr=lr, 
                    clip_epsilon=eps, 
                    entropy_coef=ent
                )
                
                total_rewards = []
                for episode in range(2 if smoke_test else 5):
                    state = env.reset()
                    episode_reward = 0
                    for _ in range(100):
                        action, log_prob, value = agent.select_action(state)
                        next_state, reward, done, _ = env.step(action)
                        
                        agent.buffer.add(
                            state, action, log_prob, reward, value.item(), 1.0 - float(done)
                        )
                        state = next_state
                        episode_reward += reward
                        
                        if done:
                            _, _, next_value = agent.select_action(next_state)
                            loss = agent.train_on_batch(mx.squeeze(next_value), batch_size=32, epochs=3)
                            break
                    total_rewards.append(episode_reward)
                
                mean_reward = np.mean(total_rewards)
                final_loss_val = 0.0
                if 'loss' in locals() and isinstance(loss, dict):
                    final_loss_val = float(loss.get("loss", 0.0))
                elif 'loss' in locals():
                    final_loss_val = float(loss)

                results.append({
                    "params": {"lr": lr, "clip_epsilon": eps, "entropy_coef": ent},
                    "mean_reward": float(mean_reward),
                    "final_loss": final_loss_val
                })

    # Sort by mean reward
    results.sort(key=lambda x: x["mean_reward"], reverse=True)
    
    # Save results
    output_path = "backend/data/ppo_results.json"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    
    print(f"✅ Grid search complete. Results saved to {output_path}")
    for i, r in enumerate(results[:3]):
        print(f"{i+1}. Reward: {r['mean_reward']:.4f} | Params: {r['params']}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()
    
    run_grid_search(smoke_test=args.smoke_test)

import mlx.core as mx
import mlx.nn as nn
import mlx.optimizers as optim
import mlx.utils as utils
import numpy as np
import time
import asyncio
from typing import List, Tuple, Dict, Any, Optional
from backend.agents.rl_policy import RLPolicy
from backend.agents.rl_utils import compute_gae

class TrajectoryBuffer:
    def __init__(self):
        self.states: List[mx.array] = []
        self.actions: List[mx.array] = []
        self.log_probs: List[mx.array] = []
        self.rewards: List[mx.array] = []
        self.values: List[mx.array] = []
        self.masks: List[mx.array] = []

    def clear(self):
        self.states.clear()
        self.actions.clear()
        self.log_probs.clear()
        self.rewards.clear()
        self.values.clear()
        self.masks.clear()

    def add(self, state: mx.array, action: mx.array, log_prob: mx.array, reward: float, value: float, mask: float):
        self.states.append(state)
        self.actions.append(action)
        self.log_probs.append(log_prob)
        self.rewards.append(mx.array([reward]))
        self.values.append(mx.array([value]))
        self.masks.append(mx.array([mask]))

    def get_tensors(self) -> Tuple[mx.array, mx.array, mx.array, mx.array, mx.array, mx.array]:
        return (
            mx.concatenate(self.states, axis=0),
            mx.concatenate(self.actions, axis=0),
            mx.concatenate(self.log_probs, axis=0),
            mx.concatenate(self.rewards, axis=0),
            mx.concatenate(self.values, axis=0),
            mx.concatenate(self.masks, axis=0)
        )

class PPOAgent:
    def __init__(self, n_assets: int = 10, state_dim: int = 64, lr: float = 3e-4, gamma: float = 0.99, lam: float = 0.95, clip_epsilon: float = 0.2, entropy_coef: float = 0.01, value_coef: float = 0.5, max_grad_norm: float = 0.5, reward_scaling: float = 1.0, metrics_queue: Optional[asyncio.Queue] = None):
        # Force GPU if available AND not in CI (VMs often crash on Metal access)
        if mx.metal.is_available() and os.getenv("CI") != "true":
            try:
                mx.set_default_device(mx.gpu)
            except Exception:
                # Fallback to CPU if GPU initialization fails
                mx.set_default_device(mx.cpu)
            
        self.policy = RLPolicy(n_assets=n_assets, state_dim=state_dim)
        self.optimizer = optim.Adam(learning_rate=lr)
        self.log_std = mx.zeros((n_assets,))
        self.gamma = gamma
        self.lam = lam
        self.clip_epsilon = clip_epsilon
        self.entropy_coef = entropy_coef
        self.value_coef = value_coef
        self.max_grad_norm = max_grad_norm
        self.reward_scaling = reward_scaling
        self.buffer = TrajectoryBuffer()
        self._prev_params = None
        self.metrics_queue = metrics_queue

    def get_log_prob(self, mean: mx.array, action: mx.array, log_std: mx.array) -> mx.array:
        std = mx.exp(log_std)
        var = std ** 2
        log_prob = -0.5 * (((action - mean) ** 2) / var + 2 * mx.log(std) + mx.log(2 * np.pi))
        return mx.sum(log_prob, axis=-1)

    def select_action(self, state: mx.array) -> Tuple[mx.array, mx.array, mx.array]:
        mean, value = self.policy(state)
        std = mx.exp(self.log_std)
        noise = mx.random.normal(mean.shape)
        action = mean + noise * std
        action = mx.clip(action, 0.0, 1.0)
        log_prob = self.get_log_prob(mean, action, self.log_std)
        return action, log_prob, value

    def loss_fn(self, model: nn.Module, log_std: mx.array, states: mx.array, actions: mx.array, old_log_probs: mx.array, advantages: mx.array, returns: mx.array) -> Tuple[mx.array, mx.array, mx.array, mx.array]:
        mean, values = model(states)
        values = mx.squeeze(values, -1)
        std = mx.exp(log_std)
        var = std ** 2
        new_log_probs = -0.5 * (((actions - mean) ** 2) / var + 2 * mx.log(std) + mx.log(2 * np.pi))
        new_log_probs = mx.sum(new_log_probs, axis=-1)
        ratio = mx.exp(new_log_probs - old_log_probs)
        surr1 = ratio * advantages
        surr2 = mx.clip(ratio, 1.0 - self.clip_epsilon, 1.0 + self.clip_epsilon) * advantages
        policy_loss = -mx.mean(mx.minimum(surr1, surr2))
        value_loss = mx.mean((returns - values) ** 2)
        entropy = mx.sum(0.5 * (1.0 + mx.log(2 * np.pi * var)))
        entropy_loss = -mx.mean(entropy)
        total_loss = policy_loss + self.value_coef * value_loss + self.entropy_coef * entropy_loss
        return total_loss, policy_loss, value_loss, entropy_loss

    def train_on_batch(self, next_value: mx.array, batch_size: int = 64, epochs: int = 10) -> Dict[str, Any]:
        states, actions, log_probs, rewards, values, masks = self.buffer.get_tensors()
        
        # Apply Reward Scaling
        scaled_rewards = rewards * self.reward_scaling
        
        advantages, returns = compute_gae(scaled_rewards, values, masks, next_value, self.gamma, self.lam)
        advantages = (advantages - mx.mean(advantages)) / (mx.std(advantages) + 1e-8)
        
        # Snapshot params for stability score
        if self._prev_params is None:
            self._prev_params = [mx.array(p) for _, p in utils.tree_flatten(self.policy.parameters())]
        
        loss_value_and_grad = nn.value_and_grad(self.policy, self.loss_fn)
        dataset_size = states.shape[0]
        
        # Collect metric tensors without calling .item() inside the loop
        all_losses = []
        all_entropies = []
        
        for epoch in range(epochs):
            indices = np.random.permutation(dataset_size)
            for i in range(0, dataset_size, batch_size):
                idx = indices[i : i + batch_size]
                if len(idx) < batch_size and i > 0: continue
                batch_indices = mx.array(idx)
                b_states = states[batch_indices]
                b_actions = actions[batch_indices]
                b_log_probs = log_probs[batch_indices]
                b_advantages = advantages[batch_indices]
                b_returns = returns[batch_indices]
                
                (loss, p_loss, v_loss, e_loss), grads = loss_value_and_grad(
                    self.policy, self.log_std, b_states, b_actions, b_log_probs, b_advantages, b_returns
                )
                
                self.optimizer.update(self.policy, grads)
                all_losses.append(loss)
                all_entropies.append(e_loss)

        # Evaluate all updates and metrics at once (One big GPU push)
        mx.eval(self.policy.parameters(), self.optimizer.state, all_losses, all_entropies)
        
        # Calculate Stability Score
        current_params = [mx.array(p) for _, p in utils.tree_flatten(self.policy.parameters())]
        stability_score = self._calculate_stability(self._prev_params, current_params)
        self._prev_params = current_params
        
        # Compute final scalars - only 4 .item() calls for the entire training run!
        avg_loss = mx.mean(mx.array(all_losses)).item()
        avg_entropy = -mx.mean(mx.array(all_entropies)).item()
        mean_reward = mx.mean(rewards).item()
        
        self.buffer.clear()
        
        metrics = {
            "loss": float(avg_loss),
            "entropy": float(avg_entropy),
            "mean_reward": float(mean_reward),
            "stability_score": stability_score,
            "timestamp": time.time(),
            "epoch": epochs
        }
        
        if self.metrics_queue is not None:
            try:
                self.metrics_queue.put_nowait(metrics)
            except asyncio.QueueFull:
                # Discard if full to avoid blocking training
                pass
                
        return metrics

    def _calculate_stability(self, old_params: List[mx.array], new_params: List[mx.array]) -> float:
        """
        Calculates stability as 1.0 - normalized mean squared difference.
        """
        diffs = []
        for p1, p2 in zip(old_params, new_params):
            diffs.append(mx.mean((p1 - p2) ** 2))
        
        avg_diff = mx.mean(mx.array(diffs)).item()
        # Scale: very small differences mean high stability (1.0).
        # We use a logarithmic scale or a steep linear drop.
        stability = max(0.0, 1.0 - (avg_diff * 500)) # Increased sensitivity
        return float(stability)

if __name__ == "__main__":
    agent = PPOAgent(n_assets=5, state_dim=64)
    for _ in range(128):
        state = mx.random.normal((1, 64))
        action, log_prob, value = agent.select_action(state)
        agent.buffer.add(state, action, log_prob, 0.01, value.item(), 1.0)
    next_state = mx.random.normal((1, 64))
    _, _, next_value = agent.select_action(next_state)
    metrics = agent.train_on_batch(mx.squeeze(next_value), batch_size=32, epochs=5)
    print(f"Training Loss: {metrics['loss']:.4f}")
    print(f"Stability Score: {metrics['stability_score']:.4f}")
    print("PPOAgent Operational.")

import mlx.core as mx
import mlx.nn as nn
import mlx.optimizers as optim
import numpy as np
from typing import List, Tuple, Dict, Any
try:
    from .rl_policy import RLPolicy
    from .rl_utils import compute_gae
except (ImportError, ValueError):
    from rl_policy import RLPolicy
    from rl_utils import compute_gae

class TrajectoryBuffer:
    def __init__(self):
        self.states = []
        self.actions = []
        self.log_probs = []
        self.rewards = []
        self.values = []
        self.masks = []

    def clear(self):
        self.states.clear()
        self.actions.clear()
        self.log_probs.clear()
        self.rewards.clear()
        self.values.clear()
        self.masks.clear()

    def add(self, state, action, log_prob, reward, value, mask):
        self.states.append(state)
        self.actions.append(action)
        self.log_probs.append(log_prob)
        self.rewards.append(mx.array([reward]))
        self.values.append(mx.array([value]))
        self.masks.append(mx.array([mask]))

    def get_tensors(self):
        return (
            mx.concatenate(self.states, axis=0),
            mx.concatenate(self.actions, axis=0),
            mx.concatenate(self.log_probs, axis=0),
            mx.concatenate(self.rewards, axis=0),
            mx.concatenate(self.values, axis=0),
            mx.concatenate(self.masks, axis=0)
        )

class PPOAgent:
    def __init__(self, n_assets=10, state_dim=64, lr=3e-4, gamma=0.99, lam=0.95, clip_epsilon=0.2, entropy_coef=0.01, value_coef=0.5, max_grad_norm=0.5):
        self.policy = RLPolicy(n_assets=n_assets, state_dim=state_dim)
        self.optimizer = optim.Adam(learning_rate=lr)
        self.log_std = mx.zeros((n_assets,))
        self.gamma = gamma
        self.lam = lam
        self.clip_epsilon = clip_epsilon
        self.entropy_coef = entropy_coef
        self.value_coef = value_coef
        self.max_grad_norm = max_grad_norm
        self.buffer = TrajectoryBuffer()

    def get_log_prob(self, mean, action, log_std):
        std = mx.exp(log_std)
        var = std ** 2
        log_prob = -0.5 * (((action - mean) ** 2) / var + 2 * mx.log(std) + mx.log(2 * np.pi))
        return mx.sum(log_prob, axis=-1)

    def select_action(self, state):
        mean, value = self.policy(state)
        std = mx.exp(self.log_std)
        noise = mx.random.normal(mean.shape)
        action = mean + noise * std
        action = mx.clip(action, 0.0, 1.0)
        log_prob = self.get_log_prob(mean, action, self.log_std)
        return action, log_prob, value

    def loss_fn(self, model, log_std, states, actions, old_log_probs, advantages, returns):
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
        return total_loss

    def train_on_batch(self, next_value, batch_size=64, epochs=10):
        states, actions, log_probs, rewards, values, masks = self.buffer.get_tensors()
        advantages, returns = compute_gae(rewards, values, masks, next_value, self.gamma, self.lam)
        advantages = (advantages - mx.mean(advantages)) / (mx.std(advantages) + 1e-8)
        loss_value_and_grad = nn.value_and_grad(self.policy, self.loss_fn)
        dataset_size = states.shape[0]
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
                loss, grads = loss_value_and_grad(self.policy, self.log_std, b_states, b_actions, b_log_probs, b_advantages, b_returns)
                self.optimizer.update(self.policy, grads)
                mx.eval(self.policy.parameters(), self.optimizer.state)
        self.buffer.clear()
        return loss

if __name__ == "__main__":
    agent = PPOAgent(n_assets=5, state_dim=64)
    for _ in range(128):
        state = mx.random.normal((1, 64))
        action, log_prob, value = agent.select_action(state)
        agent.buffer.add(state, action, log_prob, 0.01, value.item(), 1.0)
    next_state = mx.random.normal((1, 64))
    _, _, next_value = agent.select_action(next_state)
    loss = agent.train_on_batch(mx.squeeze(next_value), batch_size=32, epochs=5)
    print(f"Training Loss: {loss.item():.4f}")
    print("PPOAgent Operational.")

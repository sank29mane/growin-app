try:
    import mlx.core as mx
    HAS_MLX = True
except ImportError:
    mx = None
    HAS_MLX = False

from typing import Tuple

def compute_gae(rewards: 'mx.array', values: 'mx.array', masks: 'mx.array', next_value: 'mx.array', gamma: float = 0.99, lam: float = 0.95) -> Tuple['mx.array', 'mx.array']:
    if not HAS_MLX:
        return rewards, rewards
    T = rewards.shape[0]
    adv_list = []
    current_gae = mx.zeros_like(next_value)
    # v_all includes next_value for bootstrapping
    v_all = mx.concatenate([values, mx.expand_dims(next_value, 0)], axis=0)
    for t in reversed(range(T)):
        mask = masks[t]
        delta = rewards[t] + gamma * v_all[t+1] * mask - v_all[t]
        current_gae = delta + gamma * lam * mask * current_gae
        adv_list.append(current_gae)
    advantages = mx.stack(adv_list[::-1], axis=0)
    returns = advantages + values
    return advantages, returns

def financial_reward(delta_alpha, turnover, variance, tc_penalty=0.0005):
    transaction_cost = turnover * tc_penalty
    volatility_tax = 0.5 * variance
    reward = delta_alpha - (transaction_cost + volatility_tax)
    return reward

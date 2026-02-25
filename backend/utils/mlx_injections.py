"""
NPU Injections Library: Optimized MLX snippets for financial math.
These snippets are injected into the sandbox before executing generated scripts.
"""

import mlx.core as mx

MLX_INJECTIONS = {
    "monte_carlo_sim": """
def monte_carlo_sim(S0, mu, sigma, T, dt, num_sims):
    """Vectorized Monte Carlo simulation for price paths on NPU."""
    import mlx.core as mx
    num_steps = int(T / dt)
    # Batch generation of random normals
    Z = mx.random.normal(shape=(num_sims, num_steps))
    
    drift = (mu - 0.5 * sigma**2) * dt
    diffusion = sigma * mx.sqrt(mx.array(dt)) * Z
    daily_returns = mx.exp(drift + diffusion)
    
    # Prepend initial price
    initial_prices = mx.full((num_sims, 1), float(S0))
    price_paths = mx.concatenate([initial_prices, daily_returns], axis=1)
    
    # Cumulative product along the time axis (axis=1)
    return mx.cumprod(price_paths, axis=1)
""",

    "black_scholes_tensor": """
def black_scholes_tensor(S, K, T, r, sigma, option_type='call'):
    """Tensorized Black-Scholes option pricing on NPU."""
    import mlx.core as mx
    
    def norm_cdf(x):
        return 0.5 * (1.0 + mx.erf(x / mx.sqrt(mx.array(2.0))))
    
    # Ensure inputs are tensors for broadcasting
    S, K, T, r, sigma = [mx.array(x) if not isinstance(x, mx.array) else x for x in [S, K, T, r, sigma]]
    
    d1 = (mx.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * mx.sqrt(T))
    d2 = d1 - sigma * mx.sqrt(T)
    
    if option_type == 'call':
        price = S * norm_cdf(d1) - K * mx.exp(-r * T) * norm_cdf(d2)
    else:
        price = K * mx.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
    return price
""",

    "technical_indicators_vectorized": """
def rsi_mlx(prices, period=14):
    """Vectorized RSI calculation on NPU."""
    import mlx.core as mx
    if len(prices) < period + 1:
        return mx.array([])
        
    delta = prices[1:] - prices[:-1]
    gain = mx.where(delta > 0, delta, 0.0)
    loss = mx.where(delta < 0, -delta, 0.0)
    
    def sma_inner(x, n):
        cs = mx.cumsum(x)
        # Shift and subtract to get window sums
        res = (cs[n:] - cs[:-n]) / n
        return mx.concatenate([mx.full((n,), 0.0), res])

    avg_gain = sma_inner(gain, period)
    avg_loss = sma_inner(loss, period)
    
    # Avoid division by zero
    rs = avg_gain / mx.maximum(avg_loss, 1e-9)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi

def sma_mlx(prices, period):
    """Vectorized Simple Moving Average on NPU."""
    import mlx.core as mx
    if len(prices) < period:
        return mx.array([])
    cs = mx.cumsum(prices)
    res = (cs[period:] - cs[:-period]) / period
    return mx.concatenate([mx.full((period,), 0.0), res])
"""
}

def get_all_injections() -> str:
    """Returns all injection snippets joined as a single string."""
    return "

".join(MLX_INJECTIONS.values())

def get_injection(name: str) -> str:
    """Returns a specific injection snippet by name."""
    return MLX_INJECTIONS.get(name, "")

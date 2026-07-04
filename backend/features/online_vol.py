import numpy as np
from numba import njit

@njit(fastmath=True, cache=True)
def update_vol_jit(price: float, last_price: float, var: float, alpha: float) -> tuple[float, float, float]:
    """
    JIT-compiled update step for Exponential Moving Average (EMA) of squared returns.
    
    Parameters:
        price: Current price tick
        last_price: Previous price tick
        var: Current variance state (EMA of squared returns)
        alpha: Smoothing factor
        
    Returns:
        tuple: (new_last_price, new_var, volatility)
    """
    if last_price == 0.0:
        return price, 0.0, 0.0
    ret = (price - last_price) / last_price
    new_var = alpha * (ret * ret) + (1.0 - alpha) * var
    return price, new_var, np.sqrt(new_var)

# Pre-compile the JIT function to prevent compilation latency on the first tick
_ = update_vol_jit(100.0, 99.0, 0.0001, 0.05)

class OnlineVolatility:
    """
    Tracks real-time volatility using an Exponential Moving Average (EMA) of squared returns.
    Computations are delegated to a JIT-compiled Numba function for sub-microsecond latency.
    """
    def __init__(self, alpha: float):
        if not (0.0 < alpha <= 1.0):
            raise ValueError("alpha must be in the range (0.0, 1.0]")
        self.alpha = float(alpha)
        self.last_price = 0.0
        self.var = 0.0
        self.vol = 0.0
        self.initialized = False

    def update(self, price: float) -> float:
        """
        Updates the internal state with a new price tick.
        
        Parameters:
            price: The current price tick
            
        Returns:
            float: The current volatility (standard deviation estimate)
        """
        price_val = float(price)
        if not self.initialized:
            self.last_price = price_val
            self.initialized = True
            return 0.0
        
        self.last_price, self.var, self.vol = update_vol_jit(
            price_val, self.last_price, self.var, self.alpha
        )
        return self.vol

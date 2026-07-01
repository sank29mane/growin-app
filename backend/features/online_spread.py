from numba import njit

@njit(fastmath=True, cache=True)
def calculate_spread_jit(bid: float, ask: float) -> float:
    """
    JIT-compiled calculation of relative bid-ask spread.
    
    Formula: (Ask - Bid) / MidPrice, where MidPrice = (Ask + Bid) / 2.0
    
    Parameters:
        bid: Current bid price
        ask: Current ask price
        
    Returns:
        float: Relative spread (0.0 if mid price is 0.0)
    """
    mid = (ask + bid) / 2.0
    if mid == 0.0:
        return 0.0
    return (ask - bid) / mid

# Pre-compile the JIT function to prevent compilation latency on the first tick
_ = calculate_spread_jit(100.0, 100.02)

class RelativeSpread:
    """
    Tracks and computes normalized bid-ask spread on a tick-by-tick basis.
    Delegates calculations to a JIT-compiled Numba function.
    """
    def __init__(self):
        self.spread = 0.0

    def update(self, bid: float, ask: float) -> float:
        """
        Updates state and returns relative spread.
        
        Parameters:
            bid: Current bid price
            ask: Current ask price
            
        Returns:
            float: Relative spread
        """
        self.spread = calculate_spread_jit(float(bid), float(ask))
        return self.spread

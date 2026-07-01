import numpy as np
from numba import njit

@njit(fastmath=True, cache=True)
def welford_update_jit(x: float, count: int, mean: float, M2: float) -> tuple[int, float, float]:
    """
    Updates the running mean and sum of squared differences (M2) using Welford's algorithm.
    
    Parameters:
        x: New observation value
        count: Current number of observations
        mean: Current running mean
        M2: Running sum of squared differences from the mean
        
    Returns:
        tuple: (new_count, new_mean, new_M2)
    """
    count += 1
    delta = x - mean
    mean += delta / count
    delta2 = x - mean
    M2 += delta * delta2
    return count, mean, M2

@njit(fastmath=True, cache=True)
def welford_zscore_jit(x: float, count: int, mean: float, M2: float, ddof: int) -> float:
    """
    Computes the Z-score of value x based on current running statistics.
    
    Parameters:
        x: The value to normalize
        count: Running number of observations
        mean: Running mean
        M2: Running sum of squared differences from the mean
        ddof: Delta degrees of freedom (0 for population variance, 1 for sample variance)
        
    Returns:
        float: Z-score value (0.0 if standard deviation is zero)
    """
    if count < 1:
        return 0.0
    div = count - ddof
    if div <= 0:
        return 0.0
    var = M2 / div
    if var <= 1e-12:
        return 0.0
    std = np.sqrt(var)
    return (x - mean) / std

# Pre-compile the JIT functions to avoid compilation latency on the first tick
_ = welford_update_jit(1.0, 0, 0.0, 0.0)
_ = welford_zscore_jit(1.0, 2, 1.0, 0.5, 0)

class WelfordStandardizer:
    """
    Online Z-score standardizer that maintains running mean and variance using Welford's algorithm.
    All high-frequency logic is JIT-compiled via Numba to execute in sub-microsecond time.
    """
    def __init__(self, ddof: int = 0):
        self.ddof = int(ddof)
        self.count = 0
        self.mean = 0.0
        self.M2 = 0.0

    def update(self, x: float) -> float:
        """
        Updates the running statistics with a new value and returns its Z-score.
        
        Parameters:
            x: The value to incorporate
            
        Returns:
            float: Updated Z-score
        """
        x_val = float(x)
        self.count, self.mean, self.M2 = welford_update_jit(
            x_val, self.count, self.mean, self.M2
        )
        return self.zscore(x_val)

    def zscore(self, x: float) -> float:
        """
        Computes the Z-score for the given value based on current statistics without modifying the state.
        
        Parameters:
            x: The value to scale
            
        Returns:
            float: Z-score
        """
        return welford_zscore_jit(float(x), self.count, self.mean, self.M2, self.ddof)

    def initialize_state(self, mean: float, variance: float, count: int = 10000) -> None:
        """
        Initializes the standardizer with pre-calculated statistics (e.g. from historical offline fitting).
        
        Parameters:
            mean: Precomputed mean
            variance: Precomputed variance
            count: Observation weight to assign to this initialization (default 10000)
        """
        self.count = int(count)
        self.mean = float(mean)
        # Reconstruct M2: M2 = variance * (count - ddof)
        div = self.count - self.ddof
        self.M2 = float(variance) * max(div, 1)

    @property
    def variance(self) -> float:
        """Returns the current running variance."""
        div = self.count - self.ddof
        if div <= 0:
            return 0.0
        return self.M2 / div

    @property
    def std(self) -> float:
        """Returns the current running standard deviation."""
        return np.sqrt(self.variance)

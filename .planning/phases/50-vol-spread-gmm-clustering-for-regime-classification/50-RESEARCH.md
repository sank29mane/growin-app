# Phase 50 Research: Vol-Spread GMM Clustering for Regime Classification

**Goal**: Build a GMM clustering classifier to trigger 2ms hot-swaps of local QLoRA adapters based on market regimes.

## 1. Input Features: Volatility and Spread
To ensure the GMM accurately captures regimes and computes efficiently under 2ms latency:
*   **Rolling Volatility:** Standard rolling windows (like `pd.Series.rolling.std()`) have $O(N)$ overhead. Use an **Exponential Moving Average (EMA) of squared returns** or **Welford’s Online Algorithm** for $O(1)$ constant-time updates for every new tick/bar.
*   **Bid-Ask Spread:** Calculate the relative spread: `(Ask - Bid) / MidPrice` to normalize the spread across different price levels. 
*   **Normalization (Crucial):** GMMs are highly sensitive to feature scale. Since Volatility and Spread have different magnitudes, they must be standardized (Z-scored). In a live loop, maintain a running mean and variance (using Welford's algorithm) to Z-score the incoming Volatility and Spread before feeding them to the GMM.

## 2. GMM for Regime Classification
GMMs use probabilistic "soft" clustering, ideal for finance where market states overlap. 
*   **Regimes:** Usually, $K=2$ or $K=3$ components are chosen:
    *   *Regime 0 (Trend / Quiet):* Low volatility, narrow spread.
    *   *Regime 1 (Mean-Reverting / Choppy):* Medium/high volatility, average to wide spread.
    *   *Regime 2 (Crisis / Liquidity Vacuum):* Extreme volatility, abnormally wide spreads.
*   **Covariance Configuration:** Use `covariance_type='full'` for the GMM. Unlike K-Means (which assumes spherical clusters), a full covariance matrix allows the model to capture the directional correlation between volatility and spreads (e.g., how spreads widen non-linearly as volatility spikes).
*   **Application:** The model outputs probabilities (e.g., 80% chance of Regime 0). These probabilities continuously scale position sizes or dynamically switch trading logic and trigger MLX QLoRA adapter hot-swaps.

## 3. Ultra-Low Latency Implementation (< 2ms)
To achieve sub-millisecond latency, training must be strictly decoupled from inference.

### A. Offline Training (Background Process)
Fit `sklearn.mixture.GaussianMixture` periodically (e.g., daily or hourly) on historical DuckDB data in a separate background thread/process.
*   **Hyperparameter Tuning:** Automatically select the optimal number of components ($K$) and covariance constraints using **Bayesian Information Criterion (BIC)** or Akaike Information Criterion (AIC) minimization to prevent overfitting.
*   **Non-Stationarity Management:** Financial data is non-stationary; update the global Z-score scaling parameters offline during fitting and store them along with the model weights to align online normalization.
*   **Serialization:** Extract and serialize the learned parameters: `weights_`, `means_`, and `precisions_cholesky_` (the pre-computed Cholesky decomposition of the precision matrix to speed up Mahalanobis distance calculation during online evaluation).

### B. Live Inference (Trading Loop)
Do not call `sklearn`'s `predict()` in the live loop due to Python object overhead. Instead, implement a custom evaluation function using `NumPy` and compile it to machine code using `@numba.njit`. 

**Architectural Pattern:**
```python
import numpy as np
from numba import njit

@njit(fastmath=True)
def fast_gmm_predict_proba(x, weights, means, precisions):
    K = weights.shape[0]
    log_probs = np.empty(K)
    
    for k in range(K):
        diff = x - means[k]
        dist = np.dot(diff, np.dot(precisions[k], diff))
        # Add precomputed log determinant of precision
        log_probs[k] = -0.5 * dist + np.log(weights[k]) 
        
    # Log-Sum-Exp trick for numerical stability
    max_log_prob = np.max(log_probs)
    exp_probs = np.exp(log_probs - max_log_prob)
    return exp_probs / np.sum(exp_probs)
```

## Summary Build Plan
1.  **State Management:** Create an $O(1)$ class holding running EMA for Volatility and running Z-score normalizers.
2.  **Model Separation:** Write a background training script using `scikit-learn` that exports the GMM parameters to JSON/binary.
3.  **JIT Inference:** Write the `@numba.njit` fast path for computing the Multivariate Normal log-PDF and probabilities. This executes in <10 microseconds, leaving ample room for the <2ms trading loop.

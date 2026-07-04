import numpy as np
import numba

@numba.njit(fastmath=True)
def fast_gmm_predict_proba(
    x: np.ndarray,
    weights: np.ndarray,
    means: np.ndarray,
    precisions_cholesky: np.ndarray,
    scaler_mean: np.ndarray,
    scaler_var: np.ndarray
) -> np.ndarray:
    """
    Compute GMM regime probabilities for a single input feature vector.
    
    This function is optimized using Numba JIT compiling and fastmath for sub-10-microsecond latency.
    
    Parameters:
        x (np.ndarray): 1D array of shape (n_features,) containing the raw features (volatility, spread)
        weights (np.ndarray): 1D array of shape (n_components,) containing the GMM mixture weights
        means (np.ndarray): 2D array of shape (n_components, n_features) containing the component means
        precisions_cholesky (np.ndarray): 3D array of shape (n_components, n_features, n_features) containing
                                         the Cholesky decomposition of precision matrices
        scaler_mean (np.ndarray): 1D array of shape (n_features,) containing scaler means for Z-scoring
        scaler_var (np.ndarray): 1D array of shape (n_features,) containing scaler variances for Z-scoring
        
    Returns:
        np.ndarray: 1D array of shape (n_components,) containing the computed regime probabilities
    """
    n_components, n_features = means.shape
    
    # 1. Z-score standardise the input features
    # Z = (x - mean) / sqrt(variance)
    x_scaled = np.empty(n_features, dtype=x.dtype)
    for i in range(n_features):
        x_scaled[i] = (x[i] - scaler_mean[i]) / np.sqrt(scaler_var[i])
        
    log_probs = np.empty(n_components, dtype=x.dtype)
    
    # Pre-calculate constant term: -0.5 * D * log(2 * pi)
    log_2pi = np.log(2.0 * np.pi)
    const_term = -0.5 * n_features * log_2pi
    
    for k in range(n_components):
        # 2. Compute difference (x_scaled - mean_k)
        diff = np.empty(n_features, dtype=x.dtype)
        for i in range(n_features):
            diff[i] = x_scaled[i] - means[k, i]
            
        # 3. Project difference using Cholesky precision matrix: y = diff @ precisions_cholesky[k]
        # Equivalent to matrix multiplication: y_j = sum_i diff_i * prec_chol_ij
        y = np.zeros(n_features, dtype=x.dtype)
        for j in range(n_features):
            val = 0.0
            for i in range(n_features):
                val += diff[i] * precisions_cholesky[k, i, j]
            y[j] = val
            
        # 4. Compute Mahalanobis distance term: sum(y^2)
        mahalanobis = 0.0
        for i in range(n_features):
            mahalanobis += y[i] * y[i]
            
        # 5. Compute log-determinant of Cholesky precision matrix
        # Since it is upper/lower triangular, it's the sum of log of diagonal elements.
        log_det_chol = 0.0
        for i in range(n_features):
            log_det_chol += np.log(precisions_cholesky[k, i, i])
            
        # 6. Compute log Gaussian probability
        log_gaussian_prob = const_term - 0.5 * mahalanobis + log_det_chol
        
        # 7. Add prior weight log probability
        log_probs[k] = log_gaussian_prob + np.log(weights[k])
        
    # 8. Log-Sum-Exp trick for numeric overflow/underflow stability
    max_log_prob = np.max(log_probs)
    
    # Scale back to probabilities
    exp_probs = np.empty(n_components, dtype=x.dtype)
    sum_exp_probs = 0.0
    for k in range(n_components):
        exp_val = np.exp(log_probs[k] - max_log_prob)
        exp_probs[k] = exp_val
        sum_exp_probs += exp_val
        
    return exp_probs / sum_exp_probs

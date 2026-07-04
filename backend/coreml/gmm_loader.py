import os
import numpy as np

def load_gmm_params(params_path: str = None) -> dict:
    """
    Load serialized GMM clustering and Z-score standardization parameters from a compressed NPZ file.
    
    Parameters:
        params_path: Path to the .npz file containing the GMM parameters.
                     If None, defaults to 'models/gmm_regime_params.npz'.
                     
    Returns:
        dict: A dictionary containing the loaded NumPy arrays:
            - 'weights': Prior weights of each GMM component
            - 'means': Mean of each GMM component
            - 'precisions_cholesky': Cholesky decomposition of precision matrices
            - 'scaler_mean': Feature-wise mean for standardization
            - 'scaler_var': Feature-wise variance for standardization
            
    Raises:
        FileNotFoundError: If the parameters file is not found.
        ValueError: If the parameters file does not contain all required arrays,
                    or if any array contains NaN or infinity values.
    """
    if params_path is None:
        # Default to models/gmm_regime_params.npz relative to the root
        # Check current working directory relative path first, then try absolute lookup
        params_path = "models/gmm_regime_params.npz"
        if not os.path.exists(params_path):
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
            params_path = os.path.join(base_dir, "models/gmm_regime_params.npz")
            
    if not os.path.exists(params_path):
        raise FileNotFoundError(f"GMM parameters file not found at: {params_path}")
        
    try:
        data = np.load(params_path)
    except Exception as e:
        raise ValueError(f"Failed to load numpy archive: {e}")
        
    required_keys = ["weights", "means", "precisions_cholesky", "scaler_mean", "scaler_var"]
    loaded_params = {}
    
    # Read variables into memory and ensure we close the numpy file handle
    try:
        for key in required_keys:
            if key not in data:
                raise ValueError(f"Missing required parameter key '{key}' in serialized file.")
            
            val = data[key]
            
            # Check for NaN or Inf values
            if np.isnan(val).any():
                raise ValueError(f"Loaded parameter '{key}' contains NaN values.")
            if np.isinf(val).any():
                raise ValueError(f"Loaded parameter '{key}' contains infinity values.")
                
            # Extract to active NumPy array (forces load from lazy reader)
            loaded_params[key] = np.array(val, copy=True)
    finally:
        data.close()
        
    return loaded_params

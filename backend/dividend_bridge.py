import numpy as np
from typing import List, Optional
from scipy import signal
from backend.data_models import DividendData

class DividendBridge:
    """
    Data normalization and transformation bridge for dividend forecasts.
    Prepares sparse dividend data for TTM-R2 modeling.
    """

    @staticmethod
    def robust_iqr_scale(data: np.ndarray) -> np.ndarray:
        """
        Applies Robust IQR Scaling: f_t = (f_t - median) / IQR.
        Handles sparse data and outliers effectively.
        """
        if data.size == 0:
            return data
            
        median = np.median(data)
        q75, q25 = np.percentile(data, [75, 25])
        iqr = q75 - q25
        
        # Avoid division by zero if IQR is 0 (all values same)
        if iqr == 0:
            # If all values are the same, scaled values are 0
            # If we just return data - median, it will be 0 as well
            return data - median
            
        return (data - median) / iqr

    @staticmethod
    def sempo_easd_filter(data: np.ndarray) -> np.ndarray:
        """
        SEMPO EASD (Spectral Estimation for Monthly/Periodic Observations - 
        Empirical Adaptive Signal Decomposition).
        
        Isolates periodic dividend cycles from market noise using adaptive smoothing.
        Implementation uses Savitzky-Golay filter to preserve periodic peaks.
        """
        if data.size < 5:  # Minimum points for Savgol with window 5
            return data
            
        try:
            # Savitzky-Golay filter: window length 5, polyorder 2
            # Good for preserving peaks while removing noise
            window_length = min(11, data.size)
            if window_length % 2 == 0:
                window_length -= 1
                
            if window_length < 3:
                return data
                
            filtered_signal = signal.savgol_filter(data, window_length, polyorder=min(2, window_length-1))
            return filtered_signal
        except Exception:
            # Fallback to lowpass filter if Savgol fails
            try:
                b, a = signal.butter(3, 0.2, btype='low', analog=False)
                return signal.filtfilt(b, a, data)
            except Exception:
                return data

    def process_dividend_history(self, dividends: List[DividendData]) -> List[DividendData]:
        """
        Processes a list of DividendData objects, applying scaling and filtering.
        """
        if not dividends:
            return []
            
        amounts = np.array([float(d.amount) for d in dividends])
        
        scaled_amounts = self.robust_iqr_scale(amounts)
        filtered_signals = self.sempo_easd_filter(amounts)
        
        for i, div in enumerate(dividends):
            div.iqr_scaled_amount = float(scaled_amounts[i])
            div.sempo_filtered_signal = float(filtered_signals[i])
            
        return dividends

    def prepare_for_ttm(self, dividends: List[DividendData], context_points: int = 512) -> np.ndarray:
        """
        Prepares dividend data for IBM Granite TTM-R2 model.
        Handles context window requirements and padding if necessary.
        """
        amounts = np.array([float(d.amount) for d in dividends])
        
        # If we have less than required context points, handle sparsity
        # (Stub for fallback mechanism logic)
        if len(amounts) < context_points:
            # In a real scenario, we might pad or signal the forecaster to use XGBoost fallback
            pass
            
        return self.robust_iqr_scale(amounts)

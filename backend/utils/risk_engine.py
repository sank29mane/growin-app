import numpy as np
from decimal import Decimal
from typing import List, Union, Optional
from utils.financial_math import create_decimal
from app_logging import setup_logging

logger = setup_logging("risk_engine")

class RiskEngine:
    """
    Deterministic risk engine for institutional-grade metrics.
    Acts as the "Source of Truth" to prevent LLM hallucinations.
    """

    @staticmethod
    def calculate_cvar_95(returns: Union[List[float], np.ndarray, List[Decimal]]) -> Decimal:
        """
        Calculates Conditional Value at Risk (Expected Shortfall) at 95% confidence level.
        This captures the average loss in the worst 5% of cases (tail risk).
        
        Args:
            returns: List or array of historical returns (e.g., [0.01, -0.02, ...])
            
        Returns:
            Decimal: The 95% CVaR as a positive absolute value.
        """
        if not returns or len(returns) == 0:
            return create_decimal(0)
            
        try:
            # Convert to float numpy array for efficient percentile math
            # Returns are usually small floats (e.g. 0.05 for 5%)
            if isinstance(returns[0], Decimal):
                ret_array = np.array([float(r) for r in returns])
            else:
                ret_array = np.array(returns)
                
            # 1. Find the 5th percentile (Value at Risk)
            var_95 = np.percentile(ret_array, 5)
            
            # 2. Filter returns that are less than or equal to VaR
            tail_losses = ret_array[ret_array <= var_95]
            
            # 3. Calculate mean of these tail losses
            if len(tail_losses) == 0:
                cvar = var_95
            else:
                cvar = np.mean(tail_losses)
                
            # Return absolute value for consistency in risk reporting
            return create_decimal(abs(cvar))
            
        except Exception as e:
            logger.error(f"CVaR calculation failed: {e}")
            return create_decimal(0)

    @staticmethod
    def calculate_volatility(returns: Union[List[float], np.ndarray, List[Decimal]], annualized: bool = True) -> Decimal:
        """Calculates historical volatility."""
        if not returns or len(returns) < 2:
            return create_decimal(0)
            
        try:
            if isinstance(returns[0], Decimal):
                ret_array = np.array([float(r) for r in returns])
            else:
                ret_array = np.array(returns)
                
            vol = np.std(ret_array)
            if annualized:
                vol = vol * np.sqrt(252) # Assuming daily returns
                
            return create_decimal(vol)
        except Exception as e:
            logger.error(f"Volatility calculation failed: {e}")
            return create_decimal(0)

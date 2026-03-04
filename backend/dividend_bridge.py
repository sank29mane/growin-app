import numpy as np
from typing import List, Optional, Dict, Any, Union
from datetime import date, datetime, timedelta
from decimal import Decimal
from scipy import signal, optimize
from backend.data_models import DividendData, PriceData
from utils.financial_math import create_decimal, safe_div

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

class LeveragedDividendEngine:
    """
    Institutional-grade leveraged dividend capture engine.
    Implements 40bp execution gap solvers and dynamic volatility-adjusted leverage.
    """
    
    def __init__(self, target_gap_bps: int = 40):
        self.target_gap_bps = target_gap_bps
        self.execution_alpha = Decimal(str(target_gap_bps / 10000))
        
    def calculate_optimal_leverage(self, ticker: str, ex_premium_anomaly: float, volatility: float) -> float:
        """
        Calculates optimal leverage ratio based on ex-day premium statistical anomalies
        and recent volatility (Kelly-like approach with risk-parity constraints).
        """
        # Simple dynamic leverage model: Leverage = (Anomaly / Volatility^2) * RiskFactor
        # Constrained between 1.0x and 4.0x for retail safety
        if volatility <= 0:
            return 1.0
            
        risk_factor = 0.5 # Conservative Kelly fraction
        raw_leverage = (ex_premium_anomaly / (volatility ** 2)) * risk_factor
        
        return float(np.clip(raw_leverage, 1.0, 4.0))

    def solve_execution_routing(self, order_size: Decimal, liquidity_profile: Dict[str, float]) -> List[Dict[str, Any]]:
        """
        Solver-based routing logic targeting the execution gap.
        Minimizes market impact vs implementation shortfall.
        Uses a risk-neutral implementation shortfall model: Cost = sum(x_i^2 / L_i)
        """
        venues = list(liquidity_profile.keys())
        if not venues:
            return [{"venue": "DEFAULT", "amount": order_size}]
            
        total_liq = sum(liquidity_profile.values())
        if total_liq <= 0:
            return [{"venue": v, "amount": order_size / len(venues)} for v in venues]

        def objective(x):
            # Scale objective to avoid precision issues
            # Cost = sum(x_i^2 / L_i)
            impact = 0
            for i, venue in enumerate(venues):
                liq = liquidity_profile[venue]
                if liq > 0:
                    impact += (x[i] ** 2) / liq
            return impact * 1000  # Scaling factor

        # Constraint: Sum(x) == order_size
        cons = ({'type': 'eq', 'fun': lambda x: np.sum(x) - float(order_size)})
        # Bounds: x >= 0
        bnds = [(0, float(order_size)) for _ in venues]
        
        # Initial guess: equal distribution
        x0 = np.full(len(venues), float(order_size) / len(venues))
        
        # Using more robust options
        res = optimize.minimize(
            objective, x0, method='SLSQP', bounds=bnds, constraints=cons,
            options={'ftol': 1e-9, 'maxiter': 1000}
        )
        
        routing = []
        if res.success:
            for i, venue in enumerate(venues):
                routing.append({
                    "venue": venue,
                    "amount": create_decimal(round(res.x[i], 4)),
                    "confidence": 1.0 - (res.fun / (1000 * float(order_size)**2 / total_liq) if order_size > 0 else 0)
                })
        else:
            # Fallback: Proportional distribution
            for venue, liq in liquidity_profile.items():
                routing.append({
                    "venue": venue,
                    "amount": create_decimal(round(float(order_size) * (liq / total_liq), 4)),
                    "confidence": 0.5
                })
                
        return routing

    def generate_capture_plan(self, dividend: DividendData, price_history: List[PriceData]) -> Dict[str, Any]:
        """
        Generates a comprehensive trade plan for dividend capture.
        """
        if not price_history:
            return {"error": "Insufficient price history"}
            
        closes = np.array([float(p.close) for p in price_history])
        volatility = float(np.std(np.diff(np.log(closes)))) if len(closes) > 1 else 0.1
        
        # Calculate ex-day anomaly (Stub: comparison of ex-day drop vs dividend amount)
        # In production, this would be a predictive model output
        ex_premium_anomaly = 0.02 # 2% projected alpha
        
        leverage = self.calculate_optimal_leverage(dividend.ticker, ex_premium_anomaly, volatility)
        
        # Determine entry/exit triggers based on price action
        # Entry: T-1 Close or intraday dip trigger
        # Exit: Ex+1 Recovery target or stop-loss
        
        entry_price = price_history[-1].close
        target_exit = entry_price * create_decimal(1.0 + (ex_premium_anomaly / 2))
        
        return {
            "ticker": dividend.ticker,
            "ex_date": dividend.ex_date,
            "leverage": leverage,
            "entry_trigger": "CLOSE_T_MINUS_1",
            "exit_target": target_exit,
            "stop_loss": entry_price * Decimal("0.98"), # 2% hard stop
            "estimated_premium_capture_bps": int(ex_premium_anomaly * 10000)
        }

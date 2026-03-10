import numpy as np
import pandas as pd
from typing import List, Dict, Any, Optional, Union
from decimal import Decimal
from datetime import datetime, timedelta
from utils.financial_math import create_decimal, safe_div

class PortfolioAnalyzer:
    """
    Institutional-grade Portfolio Analytics Engine.
    Calculates time-series risk metrics (Sharpe, Beta, Volatility) from daily returns.
    """

    @staticmethod
    def generate_backcast_history(positions: List[Dict[str, Any]], 
                                 market_data: Dict[str, List[Dict[str, Any]]],
                                 period_days: int = 365) -> pd.DataFrame:
        """
        Generates synthetic historical portfolio value by back-calculating 
        current positions against historical prices.
        
        Args:
            positions: List of current holdings with 'ticker', 'qty', and optional 'entry_date'.
            market_data: Dict mapping ticker to OHLCV list.
            period_days: How far back to cast.
            
        Returns:
            DataFrame with 'timestamp' and 'total_value'.
        """
        # 1. Align all tickers to a common timeline
        all_series = {}
        for pos in positions:
            ticker = pos['ticker']
            qty = float(pos.get('qty', pos.get('quantity', 0)))
            entry_date = pos.get('entry_date')
            if isinstance(entry_date, str):
                entry_date = pd.to_datetime(entry_date)
            
            if ticker in market_data:
                df = pd.DataFrame(market_data[ticker])
                # Handle different timestamp keys
                ts_key = 't' if 't' in df.columns else 'timestamp'
                df['dt'] = pd.to_datetime(df[ts_key], unit='ms' if df[ts_key].dtype != 'object' else None)
                df.set_index('dt', inplace=True)
                
                # Close price
                close_key = 'c' if 'c' in df.columns else 'close'
                close_series = df[close_key]
                
                # Apply entry date filter: value is 0 before entry_date
                if entry_date:
                    close_series = close_series.copy()
                    close_series.loc[close_series.index < entry_date] = 0.0
                
                all_series[ticker] = close_series * qty

        if not all_series:
            return pd.DataFrame(columns=['total_value'])

        # 2. Combine into single Portfolio Value series
        combined_df = pd.concat(all_series.values(), axis=1).fillna(0.0)
        portfolio_value = combined_df.sum(axis=1)
        
        res = pd.DataFrame(portfolio_value, columns=['total_value'])
        res.index.name = 'timestamp'
        return res.sort_index()

    @staticmethod
    def calculate_daily_returns(prices: Union[List[float], np.ndarray], method: str = 'log') -> np.ndarray:
        """
        Calculates daily returns from price history.
        'log' returns are preferred for mathematical properties in time-series.
        """
        arr = np.array(prices)
        if len(arr) < 2:
            return np.array([0.0])
            
        if method == 'log':
            return np.diff(np.log(arr))
        else:
            return np.diff(arr) / arr[:-1]

    @staticmethod
    def calculate_volatility(returns: np.ndarray, annualize: bool = True) -> float:
        """
        Calculates standard deviation of daily returns.
        Annualized by multiplying by sqrt(252).
        """
        if len(returns) < 2:
            return 0.0
            
        vol = np.std(returns)
        if annualize:
            vol *= np.sqrt(252)
            
        return float(vol)

    @staticmethod
    def calculate_sharpe_ratio(returns: np.ndarray, risk_free_rate: float = 0.04) -> float:
        """
        Calculates Annualized Sharpe Ratio.
        Sharpe = (Annualized Return - Risk Free Rate) / Annualized Volatility
        """
        if len(returns) < 2:
            return 0.0
            
        avg_daily_return = np.mean(returns)
        annualized_return = avg_daily_return * 252
        
        vol = PortfolioAnalyzer.calculate_volatility(returns, annualize=True)
        if vol == 0:
            return 0.0
            
        return float((annualized_return - risk_free_rate) / vol)

    @staticmethod
    def calculate_sortino_ratio(returns: np.ndarray, risk_free_rate: float = 0.04) -> float:
        """
        Calculates Annualized Sortino Ratio (uses only downside deviation).
        """
        if len(returns) < 2:
            return 0.0
            
        avg_daily_return = np.mean(returns)
        annualized_return = avg_daily_return * 252
        
        # Downside deviation
        downside_returns = returns[returns < 0]
        if len(downside_returns) < 2:
            # Fallback to standard vol or 0 if no downside
            downside_vol = np.std(returns) * np.sqrt(252)
        else:
            downside_vol = np.std(downside_returns) * np.sqrt(252)
            
        if downside_vol == 0:
            return 0.0
            
        return float((annualized_return - risk_free_rate) / downside_vol)

    @staticmethod
    def calculate_beta(asset_returns: np.ndarray, benchmark_returns: np.ndarray) -> float:
        """
        Calculates Beta against a benchmark using linear regression.
        Beta = Cov(Asset, Benchmark) / Var(Benchmark)
        """
        # Ensure lengths match
        min_len = min(len(asset_returns), len(benchmark_returns))
        if min_len < 2:
            return 1.0 # Default to market beta
            
        a = asset_returns[-min_len:]
        b = benchmark_returns[-min_len:]
        
        covariance = np.cov(a, b)[0, 1]
        variance = np.var(b)
        
        if variance == 0:
            return 1.0
            
        return float(covariance / variance)

    def analyze_performance(self, price_history: List[float], benchmark_history: Optional[List[float]] = None) -> Dict[str, Any]:
        """
        Comprehensive performance analysis report.
        """
        returns = self.calculate_daily_returns(price_history)
        
        vol = self.calculate_volatility(returns)
        sharpe = self.calculate_sharpe_ratio(returns)
        sortino = self.calculate_sortino_ratio(returns)
        
        result = {
            "volatility": vol,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "daily_returns_mean": float(np.mean(returns)) if len(returns) > 0 else 0.0
        }
        
        if benchmark_history:
            bench_returns = self.calculate_daily_returns(benchmark_history)
            beta = self.calculate_beta(returns, bench_returns)
            result["beta"] = beta
            
        return result

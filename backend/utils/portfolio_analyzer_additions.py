import pandas as pd
import numpy as np
import logging
from typing import Dict, List, Optional, Union, Any
from app_logging import setup_logging

logger = setup_logging("portfolio_analyzer_additions")

class PortfolioAnalyzerAdditions:
    @staticmethod
    def calculate_daily_returns(price_history: List[float], method: str = 'simple') -> np.ndarray:
        if not price_history or len(price_history) < 2:
            return np.array([])
        prices = np.array(price_history)
        if method == 'log':
            return np.diff(np.log(prices))
        return np.diff(prices) / prices[:-1]

    @staticmethod
    def calculate_sharpe_ratio(returns: np.ndarray, risk_free_rate: float = 0.04) -> float:
        if len(returns) == 0:
            return 0.0
        rf_daily = risk_free_rate / 252
        excess_returns = returns - rf_daily
        if np.std(excess_returns) == 0:
            return 0.0 if np.mean(excess_returns) == 0 else float(np.inf * np.sign(np.mean(excess_returns)))
        return float(np.mean(excess_returns) / np.std(excess_returns) * np.sqrt(252))

    @staticmethod
    def generate_backcast_history(positions: List[Dict[str, Any]], market_data: Dict[str, List[Dict[str, float]]]) -> pd.DataFrame:
        if not positions or not market_data:
            return pd.DataFrame()

        dfs = []
        for pos in positions:
            ticker = pos["ticker"]
            qty = pos["qty"]
            if ticker in market_data:
                data = market_data[ticker]
                df = pd.DataFrame(data)
                if df.empty:
                    continue
                df['t'] = pd.to_datetime(df['t'], unit='ms')
                df.set_index('t', inplace=True)
                df = df.rename(columns={'c': f'{ticker}_price'})
                df[f'{ticker}_val'] = df[f'{ticker}_price'] * qty
                dfs.append(df[[f'{ticker}_val']])

        if not dfs:
            return pd.DataFrame()

        combined = pd.concat(dfs, axis=1)
        combined.fillna(method='ffill', inplace=True)
        combined.fillna(method='bfill', inplace=True)
        combined['total_value'] = combined.sum(axis=1)
        return combined


import pandas as pd
import xgboost as xgb
from typing import List, Dict, Any
import logging
from utils.indicators import add_intraday_features

logger = logging.getLogger(__name__)

class MLIntradayForecaster:
    """
    SOTA Intraday Forecaster using XGBoost with Technical Indicators.
    Specialized for Mean Reversion and short-term momentum.
    """
    
    def __init__(self, model_type: str = "xgboost"):
        self.model_type = model_type
        self.lags = [1, 2, 3, 5, 8]
        self.feature_cols = [f'lag_{l}' for l in self.lags] + ['rsi', 'vol_ma', 'ma_dist', 'roc_3']
        self.model = None

    def _prepare_data(self, ohlcv_data: List[Dict[str, Any]]) -> pd.DataFrame:
        """Convert OHLCV list to feature-enriched DataFrame."""
        df = pd.DataFrame(ohlcv_data)
        # Handle different key names if necessary
        rename_map = {'t': 'timestamp', 'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'}
        df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
        
        # Add technical indicators
        df = add_intraday_features(df)
        
        # Add Lags
        for lag in self.lags:
            df[f'lag_{lag}'] = df['close'].shift(lag)
            
        return df.dropna()

    def train_and_predict(self, ohlcv_data: List[Dict[str, Any]], steps: int) -> List[float]:
        """Trains on history and generates recursive predictions."""
        try:
            df = self._prepare_data(ohlcv_data)
            if len(df) < 50:
                raise ValueError("Insufficient feature-ready data points")

            X = df[self.feature_cols]
            y = df['close']

            # Train XGBoost
            self.model = xgb.XGBRegressor(
                n_estimators=100,
                learning_rate=0.08,
                max_depth=6,
                objective='reg:squarederror',
                random_state=42
            )
            self.model.fit(X, y)

            # Recursive Forecasting
            # We take the state of the last row to begin
            current_features = df.tail(1).copy()
            recent_closes = df['close'].tail(max(self.lags)).tolist()
            predictions = []

            for _ in range(steps):
                # Predict next step
                X_next = current_features[self.feature_cols]
                pred_val = float(self.model.predict(X_next)[0])
                predictions.append(pred_val)

                # Update state for next recursion
                recent_closes.append(pred_val)
                # Note: We keep indicators (RSI, Vol) static during the projection 
                # to avoid compounding complex indicator errors, focusing on price lags.
                new_row = current_features.copy()
                for i, lag in enumerate(self.lags):
                    new_row[f'lag_{lag}'] = recent_closes[-(lag+1)]
                current_features = new_row

            return predictions

        except Exception as e:
            logger.error(f"MLIntradayForecaster failed: {e}")
            return []

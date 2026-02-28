
import os
import sys
import pandas as pd
import numpy as np
import logging
from typing import List, Tuple
from datetime import datetime, timedelta

# Ensure backend path is in sys.path
sys.path.append(os.path.abspath(os.path.dirname(__file__) + "/.."))

from forecaster import TTMForecaster
# We will implement the new wrappers here or import them once created.
# For now, let's include the Prophet/XGBoost logic directly in this lab script
# to prove the concept before moving to production code.

import xgboost as xgb
from sklearn.ensemble import RandomForestRegressor
from prophet import Prophet
from sklearn.metrics import mean_absolute_percentage_error

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BacktestLab")

# --- FEATURE ENGINEERING (Intraday Optimized) ---
def add_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Simple RSI implementation
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # Volatility (ATR-like)
    df['volatility'] = df['high'] - df['low']
    df['vol_ma'] = df['volatility'].rolling(window=14).mean()
    
    # Distance from MA
    df['ma_20'] = df['close'].rolling(window=20).mean()
    df['ma_dist'] = df['close'] - df['ma_20']
    
    return df.fillna(method='bfill').fillna(0)

def create_features(df: pd.DataFrame, lags: List[int]) -> Tuple[pd.DataFrame, List[str]]:
    df_feat = add_technical_indicators(df)
    feature_cols = []
    
    # Lags
    for lag in lags:
        col = f'lag_{lag}'
        df_feat[col] = df_feat['close'].shift(lag)
        feature_cols.append(col)
        
    # Technicals
    feature_cols.extend(['rsi', 'vol_ma', 'ma_dist'])
    
    return df_feat.dropna(), feature_cols

# --- PROPHET IMPLEMENTATION (Daily Focus) ---
def forecast_prophet(df: pd.DataFrame, periods: int = 7) -> pd.DataFrame:
    pdf = df.rename(columns={'timestamp': 'ds', 'close': 'y'})
    model = Prophet(daily_seasonality=True, yearly_seasonality=False)
    model.fit(pdf)
    future = model.make_future_dataframe(periods=periods, freq='H') 
    forecast = model.predict(future)
    return forecast.tail(periods)[['ds', 'yhat']].rename(columns={'ds': 'timestamp', 'yhat': 'close'})

# --- ML MODELS (XGB & RF) ---
def forecast_ml(df: pd.DataFrame, periods: int = 7, model_type="xgboost") -> pd.DataFrame:
    lags = [1, 2, 3, 5]
    df_train, feature_cols = create_features(df, lags)
    
    X = df_train[feature_cols]
    y = df_train['close']
    
    if model_type == "xgboost":
        model = xgb.XGBRegressor(n_estimators=100, learning_rate=0.1, max_depth=5)
    else: # Random Forest
        model = RandomForestRegressor(n_estimators=100, max_depth=10)
        
    model.fit(X, y)
    
    # Recursive Prediction (Simplified: Technicals kept static for window)
    current_row = df_train.tail(1).copy()
    predictions = []
    
    for _ in range(periods):
        pred_val = float(model.predict(current_row[feature_cols])[0])
        predictions.append(pred_val)
        
        # Update current row for next step (approximate)
        current_row['close'] = pred_val
        # Shift lags
        for i in range(len(lags)-1, 0, -1):
            current_row[f'lag_{lags[i]}'] = current_row[f'lag_{lags[i-1]}']
        current_row['lag_1'] = pred_val
    
    return predictions

# --- DATA FETCHING ---
def get_test_data(ticker="1HOUR_INTRADAY", days=20):
    points = days * 24 # Hourly 20 days
    dates = [datetime.now() - timedelta(hours=x) for x in range(points)]
    dates.reverse()
    
    # Mean-reverting noisy process
    price = [100.0]
    for i in range(1, points):
        # Revert to 100
        change = (100 - price[-1]) * 0.05 + np.random.normal(0, 0.5)
        price.append(price[-1] + change)
    
    price = np.array(price)
    df = pd.DataFrame({
        'timestamp': dates,
        'open': price * (1 - 0.001),
        'high': price * (1 + 0.002),
        'low': price * (1 - 0.002),
        'close': price,
        'volume': 1000
    })
    return df

# --- RUNNER ---
def run_benchmark():
    # FOCUS ON INTRADAY
    df = get_test_data()
    print("\n--- Benchmarking INTRADAY (Hourly) ---")
    
    train_size = int(len(df) * 0.95)
    train_df = df.iloc[:train_size]
    test_df = df.iloc[train_size:]
    actuals = test_df['close'].tolist()
    steps = len(test_df)

    # 1. Holt-Winters
    fc = TTMForecaster()
    ohlcv = train_df.rename(columns={'timestamp': 't', 'open':'o','high':'h','low':'l','close':'c','volume':'v'}).to_dict('records')
    for x in ohlcv: x['t'] = int(x['t'].timestamp()*1000)
    hw_res = fc._statistical_forecast(ohlcv, steps, timeframe="1Hour")
    hw_preds = [x['close'] for x in hw_res['forecast']]

    # 2. XGBoost (Enhanced)
    xgb_preds = forecast_ml(train_df, periods=steps, model_type="xgboost")
    
    # 3. Random Forest (Enhanced)
    rf_preds = forecast_ml(train_df, periods=steps, model_type="rf")

    results = {
        "Holt-Winters": mean_absolute_percentage_error(actuals, hw_preds),
        "XGBoost (RSI)": mean_absolute_percentage_error(actuals, xgb_preds),
        "Random Forest (RSI)": mean_absolute_percentage_error(actuals, rf_preds)
    }

    print("\nMAPE Scores (Lower is Better):")
    for k, v in results.items():
        print(f"  {k}: {v:.6f}")
    
    if results["Random Forest (RSI)"] < results["Holt-Winters"]:
        print("\n🏆 Random Forest wins on Intraday!")
    elif results["XGBoost (RSI)"] < results["Holt-Winters"]:
        print("\n🏆 XGBoost wins on Intraday!")
    else:
        print("\n🛡️ Holt-Winters remains king of stability.")

if __name__ == "__main__":
    run_benchmark()

if __name__ == "__main__":
    run_benchmark()

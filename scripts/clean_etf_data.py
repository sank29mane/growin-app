import os
import sys
import pandas as pd
import numpy as np
import duckdb

# Add backend to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../backend')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.analytics_db import AnalyticsDB

def clean_data(db_path="backend/data/analytics.duckdb"):
    print(f"🧹 Initializing Data Cleaning Pipeline (Database: {db_path})...")
    db = AnalyticsDB(db_path=db_path)
    
    # 1. Fetch all raw data from DuckDB
    query = "SELECT ticker, timestamp, open, high, low, close, volume FROM raw_market_data ORDER BY ticker, timestamp ASC"
    rows = db.execute(query).fetchall()
    
    if not rows:
        print("❌ No raw data found in raw_market_data table.")
        return False
        
    df_raw = pd.DataFrame(rows, columns=['ticker', 'timestamp', 'open', 'high', 'low', 'close', 'volume'])
    print(f"Fetched {len(df_raw)} rows of raw data.")
    
    # Clear target table first to keep it idempotent
    db.execute("DELETE FROM clean_market_data")
    
    tickers = df_raw['ticker'].unique()
    total_inserted = 0
    
    for ticker in tickers:
        df_ticker = df_raw[df_raw['ticker'] == ticker].copy()
        df_ticker['timestamp'] = pd.to_datetime(df_ticker['timestamp'])
        df_ticker.set_index('timestamp', inplace=True)
        
        # Sort index to ensure temporal order
        df_ticker.sort_index(inplace=True)
        
        # --- Task 2.3: Outlier Detection and Correction ---
        # Calculate percentage return between consecutive ticks
        close_pct_change = df_ticker['close'].pct_change().abs()
        outliers = close_pct_change > 0.30  # > 30% price change in 10 minutes is highly likely a bad print/glitch
        
        if outliers.any():
            outlier_count = outliers.sum()
            print(f"  ⚠️ Detected {outlier_count} outliers for {ticker}. Fixing them via forward-fill...")
            df_ticker.loc[outliers, 'close'] = np.nan
            df_ticker.loc[outliers, 'open'] = np.nan
            df_ticker.loc[outliers, 'high'] = np.nan
            df_ticker.loc[outliers, 'low'] = np.nan
            
            # Forward fill the price outliers
            df_ticker['close'] = df_ticker['close'].ffill()
            df_ticker['open'] = df_ticker['open'].ffill().fillna(df_ticker['close'])
            df_ticker['high'] = df_ticker['high'].ffill().fillna(df_ticker['close'])
            df_ticker['low'] = df_ticker['low'].ffill().fillna(df_ticker['close'])
            
        # --- Task 2.1: Forward-fill missing periods across 10-minute intervals ---
        # Reindex to strict 10-minute intervals between min and max timestamps
        min_ts = df_ticker.index.min()
        max_ts = df_ticker.index.max()
        strict_range = pd.date_range(start=min_ts, end=max_ts, freq='10min')
        
        df_ticker = df_ticker.reindex(strict_range)
        df_ticker.index.name = 'timestamp'
        
        # Forward-fill prices
        df_ticker['close'] = df_ticker['close'].ffill()
        df_ticker['open'] = df_ticker['open'].ffill().fillna(df_ticker['close'])
        df_ticker['high'] = df_ticker['high'].ffill().fillna(df_ticker['close'])
        df_ticker['low'] = df_ticker['low'].ffill().fillna(df_ticker['close'])
        
        # Fill missing volume with 0
        df_ticker['volume'] = df_ticker['volume'].fillna(0).astype(int)
        
        # Drop any remaining NaNs at the start of the series (before the first valid tick)
        df_ticker.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
        
        # Build records for insertion
        records = []
        for ts, row in df_ticker.iterrows():
            records.append({
                'timestamp': ts.isoformat(),
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close']),
                'volume': int(row['volume'])
            })
            
        if records:
            inserted = db.bulk_insert_ohlcv(ticker, records, table_name='clean_market_data')
            total_inserted += inserted
            print(f"  Processed {ticker}: {len(df_ticker)} cleaned rows (Inserted: {inserted}).")
        
    print(f"\n🎉 Cleaning completed! Total rows inserted into clean_market_data: {total_inserted}")
    return True

if __name__ == "__main__":
    success = clean_data()
    sys.exit(0 if success else 1)

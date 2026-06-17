import os
import sys
import json
import pandas as pd
import numpy as np
import duckdb

# Add backend to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../backend')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.analytics_db import AnalyticsDB

def prepare_data(db_path="backend/data/analytics.duckdb", output_dir="data/etfs"):
    print(f"📊 Initializing Feature Engineering and Processing Pipeline...")
    db = AnalyticsDB(db_path=db_path)
    
    # 1. Fetch clean data from DuckDB using fetchall to prevent segfault
    query = "SELECT ticker, timestamp, open, high, low, close, volume FROM clean_market_data ORDER BY ticker, timestamp ASC"
    rows = db.execute(query).fetchall()
    
    if not rows:
        print("❌ No clean data found in clean_market_data table.")
        return False
        
    df = pd.DataFrame(rows, columns=['ticker', 'timestamp', 'open', 'high', 'low', 'close', 'volume'])
    print(f"Loaded {len(df)} rows of clean data.")
    
    processed_dfs = []
    tickers = df['ticker'].unique()
    
    for ticker in tickers:
        df_ticker = df[df['ticker'] == ticker].copy()
        df_ticker['timestamp'] = pd.to_datetime(df_ticker['timestamp'])
        df_ticker.set_index('timestamp', inplace=True)
        df_ticker.sort_index(inplace=True)
        
        if len(df_ticker) < 30:
            # Skip tickers with insufficient data points
            continue
            
        # --- Task 3.1: Calculate Indicators ---
        # 1. Rolling Volatility (14 periods standard deviation of log returns)
        df_ticker['log_return'] = np.log(df_ticker['close'] / df_ticker['close'].shift(1)).fillna(0)
        df_ticker['volatility'] = df_ticker['log_return'].rolling(window=14).std().fillna(0)
        
        # 2. RSI (14 periods)
        delta = df_ticker['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / (loss + 1e-9)
        df_ticker['rsi'] = (100 - (100 / (1 + rs))).fillna(50)
        
        # 3. ATR (14 periods)
        hl = df_ticker['high'] - df_ticker['low']
        hc = (df_ticker['high'] - df_ticker['close'].shift(1)).abs()
        lc = (df_ticker['low'] - df_ticker['close'].shift(1)).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        df_ticker['atr'] = tr.rolling(window=14).mean().fillna(0)
        
        # 4. CVD (Cumulative Volume Delta)
        price_diff = df_ticker['close'].diff().fillna(0)
        vol_delta = np.where(price_diff > 0, df_ticker['volume'], np.where(price_diff < 0, -df_ticker['volume'], 0))
        df_ticker['cvd'] = vol_delta.cumsum()
        
        # --- Task 3.2: Forward-Looking Return & Labels ---
        # Predict 6 periods ahead (1 hour ahead at 10-minute bars)
        df_ticker['forward_return'] = (df_ticker['close'].shift(-6) - df_ticker['close']) / df_ticker['close']
        df_ticker['forward_return'] = df_ticker['forward_return'].fillna(0)
        
        # Calculate standard deviation of returns for this ticker to calibrate dynamic thresholds
        ret_std = df_ticker['log_return'].std()
        buy_threshold = max(ret_std * 1.5, 0.002) # 1.5x std deviation or 0.2% return
        sell_threshold = -buy_threshold
        
        # Discrete Labels: 1 (Buy), 0 (Hold), -1 (Sell)
        df_ticker['label'] = 0
        df_ticker.loc[df_ticker['forward_return'] > buy_threshold, 'label'] = 1
        df_ticker.loc[df_ticker['forward_return'] < sell_threshold, 'label'] = -1
        
        # Reset index to include timestamp in df
        df_ticker.reset_index(inplace=True)
        
        # Drop initial rows that don't have full indicators
        df_ticker = df_ticker.iloc[14:]
        
        processed_dfs.append(df_ticker)
        
    if not processed_dfs:
        print("❌ No processed data was generated!")
        return False
        
    df_final = pd.concat(processed_dfs, ignore_index=True)
    
    # Save processed features to CSV
    csv_path = os.path.join(output_dir, "processed_features.csv")
    df_final.to_csv(csv_path, index=False)
    print(f"✅ Exported CSV features to {csv_path} ({len(df_final)} rows)")
    
    # --- Task 3.3 & 3.4: Construct JSONL formatting for SLM tuning ---
    # Prompt format:
    # {"text": "<s>[INST] Market state: Ticker={ticker}, Vol={vol:.4f}, RSI={rsi:.1f}, ATR={atr:.4f}, CVD={cvd:.0f} [/INST] Recommendation: {rec} </s>"}
    jsonl_records = []
    
    label_map = {1: "BUY", 0: "HOLD", -1: "SELL"}
    
    for _, row in df_final.iterrows():
        rec_str = label_map[int(row['label'])]
        prompt = f"<market_state> Ticker={row['ticker']} Vol={row['volatility']:.5f} RSI={row['rsi']:.1f} ATR={row['atr']:.5f} CVD={row['cvd']:.0f} </market_state> Recommendation:"
        jsonl_records.append({"question": prompt, "answer": rec_str})
        
    # Shuffle and split into train (80%) and validation (20%)
    import random
    random.seed(42)
    random.shuffle(jsonl_records)
    
    split_idx = int(len(jsonl_records) * 0.8)
    train_records = jsonl_records[:split_idx]
    valid_records = jsonl_records[split_idx:]
    
    # Save train.jsonl
    train_path = os.path.join(output_dir, "train.jsonl")
    with open(train_path, "w") as f:
        for r in train_records:
            f.write(json.dumps(r) + "\n")
            
    # Save valid.jsonl
    valid_path = os.path.join(output_dir, "valid.jsonl")
    with open(valid_path, "w") as f:
        for r in valid_records:
            f.write(json.dumps(r) + "\n")
            
    print(f"✅ Exported train.jsonl ({len(train_records)} lines) and valid.jsonl ({len(valid_records)} lines) to {output_dir}")
    
    # Show label distribution
    label_dist = df_final['label'].value_counts()
    print("\nLabel distribution:")
    for lbl, cnt in label_dist.items():
        print(f"  - {label_map[lbl]} ({lbl}): {cnt} ({cnt/len(df_final)*100:.1f}%)")
        
    return True

if __name__ == "__main__":
    os.makedirs("data/etfs", exist_ok=True)
    prepare_data()

import sys
import os
import pytest
import pandas as pd
import numpy as np
import tempfile
import json
from datetime import datetime, timedelta, timezone

# Ensure root, backend, and scripts are in sys.path
root_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if root_path not in sys.path:
    sys.path.insert(0, root_path)
backend_path = os.path.join(root_path, "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)
scripts_path = os.path.join(root_path, "scripts")
if scripts_path not in sys.path:
    sys.path.insert(0, scripts_path)

from backend.analytics_db import AnalyticsDB
from clean_etf_data import clean_data
from prepare_training_data import prepare_data

def test_outlier_detection_and_cleaning():
    # Force memory database mode
    db = AnalyticsDB(db_path=":memory:")
    
    # Clean up tables
    db.execute("DELETE FROM raw_market_data")
    db.execute("DELETE FROM clean_market_data")
    
    # Create raw mock ticks with an outlier and a gap
    # Base timestamp
    base_time = datetime(2026, 6, 1, 10, 0, 0)
    ticker = "TEST_ETF"
    
    # We will insert:
    # 10:00 - Close 100.0
    # 10:10 - Close 101.0
    # 10:20 - Close 135.0 (Outlier! +33.7% change)
    # (Gap: 10:30 is missing)
    # 10:40 - Close 102.0
    
    raw_data = [
        {"timestamp": (base_time).isoformat(), "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 100},
        {"timestamp": (base_time + timedelta(minutes=10)).isoformat(), "open": 101.0, "high": 101.0, "low": 101.0, "close": 101.0, "volume": 100},
        {"timestamp": (base_time + timedelta(minutes=20)).isoformat(), "open": 135.0, "high": 135.0, "low": 135.0, "close": 135.0, "volume": 100},
        {"timestamp": (base_time + timedelta(minutes=40)).isoformat(), "open": 102.0, "high": 102.0, "low": 102.0, "close": 102.0, "volume": 100},
    ]
    
    inserted = db.bulk_insert_ohlcv(ticker, raw_data, table_name="raw_market_data")
    assert inserted == len(raw_data)
    
    # Run the cleaning pipeline (it points to :memory: due to PYTEST_CURRENT_TEST and our overrides)
    success = clean_data(db_path=":memory:")
    assert success is True
    
    # Fetch cleaned data
    clean_rows = db.execute("SELECT timestamp, open, high, low, close, volume FROM clean_market_data ORDER BY timestamp ASC").fetchall()
    
    # The clean database should have:
    # 10:00 (100.0)
    # 10:10 (101.0)
    # 10:20 (forward-filled to 101.0 due to outlier correction)
    # 10:30 (forward-filled to 101.0 due to missing period filling)
    # 10:40 (102.0)
    # That is 5 rows in total!
    assert len(clean_rows) == 5, f"Expected 5 cleaned rows, got {len(clean_rows)}"
    
    # Map back to dict
    clean_dict = {row[0].strftime("%Y-%m-%dT%H:%M:%S"): row[4] for row in clean_rows}
    
    # Verify outlier was corrected
    ts_outlier = (base_time + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S")
    assert clean_dict[ts_outlier] == 101.0, f"Expected outlier to be corrected to 101.0, got {clean_dict[ts_outlier]}"
    
    # Verify missing period was filled
    ts_missing = (base_time + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    assert clean_dict[ts_missing] == 101.0, f"Expected missing interval to be forward-filled to 101.0, got {clean_dict[ts_missing]}"
    
    # Verify last price
    ts_last = (base_time + timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%S")
    assert clean_dict[ts_last] == 102.0

def test_indicator_calculations():
    db = AnalyticsDB(db_path=":memory:")
    db.execute("DELETE FROM clean_market_data")
    
    # Prepare clean mock data: we need at least 30 rows to run indicators (14 period window + 6 period forward shifts)
    # Let's generate 40 rows.
    ticker = "TEST_ETF"
    base_time = datetime(2026, 6, 1, 10, 0, 0)
    
    clean_data_list = []
    # Let's generate a pattern where close increases and then drops
    # So we get both positive and negative returns / volume deltas
    for i in range(40):
        # close increases from 100 to 120, then drops to 110, then increases
        if i < 20:
            close = 100.0 + i
        elif i < 30:
            close = 120.0 - (i - 20) * 1.5
        else:
            close = 105.0 + (i - 30)
            
        ts = base_time + timedelta(minutes=10 * i)
        clean_data_list.append({
            "timestamp": ts.isoformat(),
            "open": close - 0.5,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1000 + i * 10
        })
        
    db.bulk_insert_ohlcv(ticker, clean_data_list, table_name="clean_market_data")
    
    # Create temporary directory for output files
    with tempfile.TemporaryDirectory() as temp_dir:
        success = prepare_data(db_path=":memory:", output_dir=temp_dir)
        assert success is True
        
        # Verify output files exist
        csv_file = os.path.join(temp_dir, "processed_features.csv")
        train_file = os.path.join(temp_dir, "train.jsonl")
        valid_file = os.path.join(temp_dir, "valid.jsonl")
        
        assert os.path.exists(csv_file)
        assert os.path.exists(train_file)
        assert os.path.exists(valid_file)
        
        # Verify CSV content
        df_features = pd.read_csv(csv_file)
        
        # 40 total rows, first 14 rows dropped to initialize 14-period indicators -> 26 rows remaining
        assert len(df_features) == 26
        
        # Check that columns are present and contain no nulls
        expected_cols = ["volatility", "rsi", "atr", "cvd", "forward_return", "label"]
        for col in expected_cols:
            assert col in df_features.columns
            assert not df_features[col].isnull().any()
            
        # Verify label values
        assert df_features["label"].isin([-1, 0, 1]).all()
        
        # Verify CVD calculation:
        # CVD is cumulative sum of (volume if return > 0 else -volume if return < 0 else 0)
        # Check first row (since it's at index 14, cumulative sum is computed from the start)
        # Let's make sure cvd is non-zero
        assert (df_features["cvd"] != 0).any()
        
        # Verify JSONL content formatting
        with open(train_file, "r") as f:
            lines = f.readlines()
            assert len(lines) > 0
            
            # Check prompt and label in one of the lines
            first_line = json.loads(lines[0])
            assert "question" in first_line
            assert "answer" in first_line
            assert first_line["answer"] in ["BUY", "HOLD", "SELL"]
            assert first_line["question"].startswith("<market_state>")
            assert first_line["question"].endswith("Recommendation:")

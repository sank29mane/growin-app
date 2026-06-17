import sys
import os
import pytest
import duckdb
from datetime import datetime, timezone

# Ensure root is in sys.path
root_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if root_path not in sys.path:
    sys.path.insert(0, root_path)

from backend.analytics_db import AnalyticsDB

def test_analytics_db_tables_exist():
    # Force memory mode for testing
    db = AnalyticsDB(db_path=":memory:")
    
    # Check if raw_market_data and clean_market_data tables were created
    tables = [t[0] for t in db.execute("show tables").fetchall()]
    assert "raw_market_data" in tables, "raw_market_data table is missing"
    assert "clean_market_data" in tables, "clean_market_data table is missing"

def test_analytics_db_bulk_insert_raw_and_clean():
    db = AnalyticsDB(db_path=":memory:")
    
    # Prepare dummy data
    ticker = "TEST.L"
    timestamp = datetime.now(timezone.utc).isoformat()
    data = [
        {
            'timestamp': timestamp,
            'open': 100.0,
            'high': 105.0,
            'low': 95.0,
            'close': 101.0,
            'volume': 1000
        }
    ]
    
    # Clean tables first
    db.execute("DELETE FROM raw_market_data")
    db.execute("DELETE FROM clean_market_data")
    
    # 1. Insert into raw_market_data
    inserted_raw = db.bulk_insert_ohlcv(ticker, data, table_name='raw_market_data')
    assert inserted_raw == 1, "Failed to insert into raw_market_data"
    
    # Verify raw count
    cnt_raw = db.execute("select count(*) from raw_market_data").fetchone()[0]
    assert cnt_raw == 1
    
    # 2. Insert into clean_market_data
    inserted_clean = db.bulk_insert_ohlcv(ticker, data, table_name='clean_market_data')
    assert inserted_clean == 1, "Failed to insert into clean_market_data"
    
    # Verify clean count
    cnt_clean = db.execute("select count(*) from clean_market_data").fetchone()[0]
    assert cnt_clean == 1
    
    # 3. Test Conflict Handling (Duplicate Insert)
    # The ON CONFLICT clause should update the values rather than raising a primary key constraint error
    updated_data = [
        {
            'timestamp': timestamp,
            'open': 100.0,
            'high': 110.0,  # updated high
            'low': 95.0,
            'close': 102.0,  # updated close
            'volume': 1500  # updated volume
        }
    ]
    
    db.bulk_insert_ohlcv(ticker, updated_data, table_name='raw_market_data')
    
    # Verify it updated in-place (still 1 row)
    cnt_raw_post = db.execute("select count(*) from raw_market_data").fetchone()[0]
    assert cnt_raw_post == 1
    
    # Verify fields were updated
    res = db.execute("select high, close, volume from raw_market_data").fetchone()
    assert res[0] == 110.0
    assert res[1] == 102.0
    assert res[2] == 1500

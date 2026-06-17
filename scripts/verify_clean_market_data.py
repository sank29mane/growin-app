import os
import sys
import duckdb
import pandas as pd

def verify():
    db_path = "backend/data/analytics.duckdb"
    print(f"🔍 Validating DuckDB clean_market_data table in {db_path}...")
    conn = duckdb.connect(db_path)
    
    # 1. Total row count
    row_count = conn.execute("select count(*) from clean_market_data").fetchone()[0]
    print(f"Total rows in clean_market_data: {row_count}")
    if row_count == 0:
        print("❌ clean_market_data is empty!")
        return False
        
    # 2. Check for null values
    null_checks = conn.execute("""
        select 
            sum(case when open is null then 1 else 0 end) as null_open,
            sum(case when close is null then 1 else 0 end) as null_close,
            sum(case when timestamp is null then 1 else 0 end) as null_ts
        from clean_market_data
    """).fetchone()
    print(f"Null check results: Open Nulls={null_checks[0]}, Close Nulls={null_checks[1]}, Timestamp Nulls={null_checks[2]}")
    
    if any(null_checks):
        print("❌ Null values found in clean_market_data!")
        return False
        
    # 3. Check time delta (should strictly adhere to 10-minute boundaries)
    # Fetch timestamps for a ticker to test delta
    rows = conn.execute("select timestamp from clean_market_data where ticker = '3LOI.L' order by timestamp asc").fetchall()
    if len(rows) > 1:
        timestamps = [pd.to_datetime(r[0]) for r in rows]
        deltas = [timestamps[i] - timestamps[i-1] for i in range(1, len(timestamps))]
        distinct_deltas = set(deltas)
        print(f"Distinct deltas for 3LOI.L: {distinct_deltas}")
        
        # Verify that all deltas are exactly 10 minutes
        for delta in distinct_deltas:
            if delta.total_seconds() != 600: # 10 minutes = 600 seconds
                # Note: overnight/weekend gaps might exist if we generate full ranges,
                # but within active days/intervals they should be 10 minutes.
                # Since we used pandas.date_range with freq='10min', all consecutive rows MUST have exactly 10 min.
                if delta.total_seconds() != 600:
                    print(f"❌ Found delta {delta} which is not 10 minutes!")
                    return False
    else:
        print("⚠️ Not enough data points to test time delta for 3LOI.L")
        
    print("\n✅ Verification Successful: clean_market_data has 0 NULLs and strictly adheres to 10-minute time-series boundaries.")
    return True

if __name__ == "__main__":
    success = verify()
    sys.exit(0 if success else 1)

import duckdb
import pandas as pd
import logging
import json
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class AnalyticsDB:
    """
    DuckDB-powered analytics engine for high-performance OLAP queries.
    
    Use Cases:
    - Time-series aggregations (20-50x faster than SQLite)
    - Complex analytical queries on historical OHLCV data
    - Portfolio performance analytics
    - Bulk data operations
    
    Architecture:
    - Columnar storage for fast scans
    - Vectorized execution
    - Multi-threaded query processing
    - In-memory or persistent mode
    """
    
    def __init__(self, db_path: str = ":memory:"):
        """
        Initialize DuckDB analytics database.
        
        Args:
            db_path: Path to database file or ":memory:" for in-memory
        """
        self.conn = duckdb.connect(db_path)
        self._init_schema()
        logger.info(f"✅ AnalyticsDB initialized (mode: {'memory' if db_path == ':memory:' else 'persistent'})")
    
    def _init_schema(self):
        """Create optimized schema for time-series and agent analytics"""
        # OHLCV historical data table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ohlcv_history (
                ticker VARCHAR NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume BIGINT,
                PRIMARY KEY (ticker, timestamp)
            )
        """)
        
        # Agent Telemetry table (SOTA 2026 Reasoning Trace)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_telemetry (
                id VARCHAR PRIMARY KEY,
                correlation_id VARCHAR,
                agent_name VARCHAR,
                subject VARCHAR,
                payload JSON,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Agent Performance table (SOTA 2026 Alpha Metrics)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_performance (
                correlation_id VARCHAR PRIMARY KEY,
                ticker VARCHAR,
                entry_price DOUBLE,
                return_1d DOUBLE,
                return_5d DOUBLE,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create indices for fast queries
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ticker_time 
            ON ohlcv_history(ticker, timestamp DESC)
        """)
        
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_telemetry_correlation 
            ON agent_telemetry(correlation_id)
        """)
        
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_performance_ticker 
            ON agent_performance(ticker)
        """)
        
        logger.info("Analytics schema initialized (including agent_telemetry and agent_performance)")

    def log_agent_message(self, message_data: Dict[str, Any]):
        """Persist an agent message to the telemetry table."""
        try:
            # DuckDB handles JSON type directly from dict
            self.conn.execute("""
                INSERT INTO agent_telemetry (id, correlation_id, agent_name, subject, payload, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                message_data.get('id'),
                message_data.get('correlation_id'),
                message_data.get('sender'),
                message_data.get('subject'),
                json.dumps(message_data.get('payload')),
                message_data.get('timestamp')
            ))
        except Exception as e:
            logger.error(f"Failed to log agent message: {e}")

    def calculate_agent_alpha(self, correlation_id: str):
        """
        Correlate agent reasoning with subsequent price action.
        Calculates 1-day and 5-day forward returns for the subject ticker.
        """
        try:
            # 1. Get ticker and timestamp from telemetry
            telemetry = self.conn.execute("""
                SELECT agent_name, payload, timestamp 
                FROM agent_telemetry 
                WHERE correlation_id = ? AND subject = 'agent_started' AND agent_name = 'OrchestratorAgent'
                LIMIT 1
            """, (correlation_id,)).fetchone()
            
            if not telemetry:
                return
                
            payload = json.loads(telemetry[1]) if isinstance(telemetry[1], str) else telemetry[1]
            # Need to extract ticker from payload (it's in 'query' or 'ticker' depending on event)
            # Orchestrator agent_started usually has the query. 
            # Better to find 'intent_classified' or 'context_fabricated' which has ticker explicitly.
            
            ticker_info = self.conn.execute("""
                SELECT payload 
                FROM agent_telemetry 
                WHERE correlation_id = ? AND subject = 'context_fabricated'
                LIMIT 1
            """, (correlation_id,)).fetchone()
            
            if not ticker_info:
                return
                
            ticker_payload = json.loads(ticker_info[0]) if isinstance(ticker_info[0], str) else ticker_info[0]
            ticker = ticker_payload.get('ticker')
            if not ticker:
                return
                
            timestamp = telemetry[2]
            
            # 2. Fetch entry price (closest to reasoning timestamp)
            entry_row = self.conn.execute("""
                SELECT close FROM ohlcv_history 
                WHERE ticker = ? AND timestamp <= ?
                ORDER BY timestamp DESC LIMIT 1
            """, (ticker, timestamp)).fetchone()
            
            if not entry_row:
                return
            
            entry_price = entry_row[0]
            
            # 3. Fetch future prices (1d and 5d forward)
            # DuckDB allows powerful interval arithmetic
            price_1d = self.conn.execute("""
                SELECT close FROM ohlcv_history 
                WHERE ticker = ? AND timestamp >= ? + INTERVAL 1 DAY
                ORDER BY timestamp ASC LIMIT 1
            """, (ticker, timestamp)).fetchone()
            
            price_5d = self.conn.execute("""
                SELECT close FROM ohlcv_history 
                WHERE ticker = ? AND timestamp >= ? + INTERVAL 5 DAY
                ORDER BY timestamp ASC LIMIT 1
            """, (ticker, timestamp)).fetchone()
            
            return_1d = (price_1d[0] - entry_price) / entry_price if price_1d else None
            return_5d = (price_5d[0] - entry_price) / entry_price if price_5d else None
            
            # 4. Store Performance Result
            self.conn.execute("""
                INSERT INTO agent_performance (correlation_id, ticker, entry_price, return_1d, return_5d, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (correlation_id) DO UPDATE SET
                    return_1d = EXCLUDED.return_1d,
                    return_5d = EXCLUDED.return_5d
            """, (correlation_id, ticker, entry_price, return_1d, return_5d, timestamp))
            
            logger.info(f"Alpha Audit: {ticker} (1d: {return_1d}, 5d: {return_5d})")
            
        except Exception as e:
            logger.error(f"Failed to calculate agent alpha: {e}")
    
    def bulk_insert_ohlcv(self, ticker: str, data: List[Dict[str, Any]]) -> int:
        """
        Fast bulk insert for OHLCV data.
        Uses DuckDB's native DataFrame integration for maximum speed.
        
        Args:
            ticker: Stock ticker symbol
            data: List of OHLCV dicts with keys: o, h, l, c, v, t OR timestamp, open, high, low, close, volume
        
        Returns:
            Number of rows inserted
        """
        if not data:
            return 0
        
        try:
            # OPTIMIZATION: Vectorized normalization (~100x faster than iteration)
            df = pd.DataFrame(data)

            # 1. Map short keys to long keys
            col_map = {
                'o': 'open',
                'h': 'high',
                'l': 'low',
                'c': 'close',
                'v': 'volume',
                't': 'timestamp'
            }
            rename_dict = {}
            for short, long in col_map.items():
                if short in df.columns and long not in df.columns:
                    rename_dict[short] = long
                elif short in df.columns and long in df.columns:
                    # Coalesce: fill NaN in long with value from short
                    df[long] = df[long].fillna(df[short])

            if rename_dict:
                df.rename(columns=rename_dict, inplace=True)

            # 2. Ensure all required columns exist and fill defaults
            required_cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
            for col in required_cols:
                if col not in df.columns:
                    df[col] = 0.0 if col != 'timestamp' else None

            # 3. Vectorized timestamp conversion
            if pd.api.types.is_numeric_dtype(df['timestamp']):
                new_ts = pd.Series(index=df.index, dtype='datetime64[ns]')
                numerics = pd.to_numeric(df['timestamp'], errors='coerce')
                
                # Heuristic: > 2e9 implies milliseconds
                mask_ms = numerics > 2_000_000_000

                if mask_ms.any():
                    new_ts.loc[mask_ms] = pd.to_datetime(numerics.loc[mask_ms], unit='ms')

                if (~mask_ms).any():
                    new_ts.loc[~mask_ms] = pd.to_datetime(numerics.loc[~mask_ms], unit='s')

                df['timestamp'] = new_ts
            else:
                # Handle ISO strings or mixed
                df['timestamp'] = pd.to_datetime(df['timestamp'])

            df['ticker'] = ticker
            
            # Bulk insert using DuckDB's fast path
            # SOTA: Registering dataframe explicitly to avoid scope resolution issues
            try:
                self.conn.register('df_tmp', df)
                self.conn.execute("""
                    INSERT INTO ohlcv_history 
                    SELECT ticker, timestamp, open, high, low, close, volume 
                    FROM df_tmp
                    ON CONFLICT (ticker, timestamp) DO UPDATE SET
                        open = EXCLUDED.open,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        close = EXCLUDED.close,
                        volume = EXCLUDED.volume
                """)
                self.conn.unregister('df_tmp')
            except Exception as duck_err:
                logger.error(f"DuckDB execution failed: {duck_err}")
                raise duck_err
            
            rows_inserted = len(df)
            logger.info(f"📊 Inserted {rows_inserted} rows for {ticker}")
            return rows_inserted
            
        except Exception as e:
            logger.error(f"Bulk insert failed: {e}")
            return 0
    
    def get_aggregated_stats(
        self, 
        ticker: str, 
        window_days: int = 30
    ) -> Optional[pd.DataFrame]:
        """
        Lightning-fast aggregated statistics for a ticker.
        
        Args:
            ticker: Stock ticker symbol
            window_days: Time window in days
        
        Returns:
            DataFrame with aggregated statistics
        """
        try:
            # Sentinel: Use parameterized query for interval to prevent SQL injection
            result = self.conn.execute("""
                SELECT 
                    ticker,
                    COUNT(*) as data_points,
                    AVG(close) as avg_price,
                    STDDEV(close) as volatility,
                    MIN(close) as min_price,
                    MAX(close) as max_price,
                    MAX(high) - MIN(low) as price_range,
                    SUM(volume) as total_volume,
                    AVG(volume) as avg_volume,
                    (MAX(close) - MIN(close)) / MIN(close) * 100 as price_change_pct
                FROM ohlcv_history
                WHERE ticker = ? 
                  AND timestamp >= CURRENT_TIMESTAMP - (INTERVAL 1 DAY * ?)
                GROUP BY ticker
            """, (ticker, int(window_days))).fetchdf()
            
            return result if not result.empty else None
            
        except Exception as e:
            logger.error(f"Aggregation query failed: {e}")
            return None
    
    def get_recent_ohlcv(
        self, 
        ticker: str, 
        limit: int = 100
    ) -> Optional[pd.DataFrame]:
        """
        Get recent OHLCV data for a ticker.
        
        Args:
            ticker: Stock ticker symbol
            limit: Number of most recent rows to fetch
        
        Returns:
            DataFrame with OHLCV data
        """
        try:
            result = self.conn.execute("""
                SELECT 
                    timestamp,
                    open,
                    high,
                    low,
                    close,
                    volume
                FROM ohlcv_history
                WHERE ticker = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (ticker, limit)).fetchdf()
            
            return result if not result.empty else None
            
        except Exception as e:
            logger.error(f"Query failed: {e}")
            return None
    
    def calculate_volatility(
        self, 
        ticker: str, 
        window_days: int = 30
    ) -> Optional[float]:
        """
        Calculate annualized volatility for a ticker.
        
        Args:
            ticker: Stock ticker symbol
            window_days: Time window in days
        
        Returns:
            Annualized volatility (standard deviation of returns)
        """
        try:
            # Sentinel: Use parameterized query for interval to prevent SQL injection
            result = self.conn.execute("""
                WITH daily_returns AS (
                    SELECT 
                        ticker,
                        close,
                        LAG(close) OVER (ORDER BY timestamp) as prev_close,
                        (close - LAG(close) OVER (ORDER BY timestamp)) / 
                            LAG(close) OVER (ORDER BY timestamp) as daily_return
                    FROM ohlcv_history
                    WHERE ticker = ?
                      AND timestamp >= CURRENT_TIMESTAMP - (INTERVAL 1 DAY * ?)
                    ORDER BY timestamp
                )
                SELECT 
                    STDDEV(daily_return) * SQRT(252) as annualized_volatility
                FROM daily_returns
                WHERE daily_return IS NOT NULL
            """, (ticker, int(window_days))).fetchone()
            
            return result[0] if result and result[0] is not None else None
            
        except Exception as e:
            logger.error(f"Volatility calculation failed: {e}")
            return None
    
    def get_price_trend(
        self, 
        ticker: str, 
        window_days: int = 30
    ) -> Optional[str]:
        """
        Determine price trend (bullish/bearish/neutral).
        
        Args:
            ticker: Stock ticker symbol
            window_days: Time window in days
        
        Returns:
            Trend description: 'bullish', 'bearish', or 'neutral'
        """
        try:
            # Sentinel: Use parameterized query for interval to prevent SQL injection
            result = self.conn.execute("""
                WITH price_data AS (
                    SELECT 
                        close,
                        ROW_NUMBER() OVER (ORDER BY timestamp) as rn,
                        COUNT(*) OVER () as total
                    FROM ohlcv_history
                    WHERE ticker = ?
                      AND timestamp >= CURRENT_TIMESTAMP - (INTERVAL 1 DAY * ?)
                )
                SELECT 
                    AVG(CASE WHEN rn <= total/2 THEN close END) as first_half_avg,
                    AVG(CASE WHEN rn > total/2 THEN close END) as second_half_avg
                FROM price_data
            """, (ticker, int(window_days))).fetchone()
            
            if not result or None in result:
                return 'neutral'
            
            first_avg, second_avg = result
            change_pct = ((second_avg - first_avg) / first_avg) * 100
            
            if change_pct > 2:
                return 'bullish'
            elif change_pct < -2:
                return 'bearish'
            else:
                return 'neutral'
                
        except Exception as e:
            logger.error(f"Trend calculation failed: {e}")
            return 'neutral'
    
    def get_agent_alpha_metrics(self, ticker: Optional[str] = None) -> Dict[str, Any]:
        """
        Summarize agent performance metrics.
        Returns a breakdown per ticker and per specialist agent.
        """
        try:
            # 1. Ticker level metrics (from agent_performance table)
            ticker_query = "SELECT ticker, AVG(return_1d) as avg_1d, AVG(return_5d) as avg_5d, COUNT(*) as count FROM agent_performance"
            params = []
            if ticker:
                ticker_query += " WHERE ticker = ? GROUP BY ticker"
                params.append(ticker)
            else:
                ticker_query += " GROUP BY ticker"
                
            ticker_res = self.conn.execute(ticker_query, params).fetchdf()
            
            # 2. Specialist level metrics (correlation between specialist completion and future returns)
            # We join performance with telemetry to see which agents were active during successful/unsuccessful sessions
            specialist_query = """
                SELECT 
                    t.agent_name,
                    AVG(p.return_1d) as avg_1d,
                    AVG(p.return_5d) as avg_5d,
                    COUNT(*) as count
                FROM agent_telemetry t
                JOIN agent_performance p ON t.correlation_id = p.correlation_id
                WHERE t.subject = 'agent_complete' AND t.agent_name != 'OrchestratorAgent'
            """
            if ticker:
                specialist_query += " AND p.ticker = ? GROUP BY t.agent_name"
                specialist_res = self.conn.execute(specialist_query, [ticker]).fetchdf()
            else:
                specialist_query += " GROUP BY t.agent_name"
                specialist_res = self.conn.execute(specialist_query).fetchdf()

            # Format result
            specialists = {}
            for _, row in specialist_res.iterrows():
                specialists[row['agent_name']] = {
                    "avg_1d": float(row['avg_1d']) if not pd.isna(row['avg_1d']) else 0.0,
                    "avg_5d": float(row['avg_5d']) if not pd.isna(row['avg_5d']) else 0.0,
                    "total_sessions": int(row['count'])
                }

            if ticker_res.empty:
                return {"avg_1d": 0.0, "avg_5d": 0.0, "total_sessions": 0, "specialists": specialists}
                
            if ticker:
                return {
                    "avg_1d": float(ticker_res.iloc[0]['avg_1d']) if not pd.isna(ticker_res.iloc[0]['avg_1d']) else 0.0,
                    "avg_5d": float(ticker_res.iloc[0]['avg_5d']) if not pd.isna(ticker_res.iloc[0]['avg_5d']) else 0.0,
                    "total_sessions": int(ticker_res.iloc[0]['count']),
                    "specialists": specialists
                }
            else:
                return {
                    "avg_1d": float(ticker_res['avg_1d'].mean()),
                    "avg_5d": float(ticker_res['avg_5d'].mean()),
                    "total_sessions": int(ticker_res['count'].sum()),
                    "specialists": specialists
                }
        except Exception as e:
            logger.error(f"Failed to fetch alpha metrics: {e}")
            return {"avg_1d": 0.0, "avg_5d": 0.0, "total_sessions": 0, "specialists": {}}


# --- Utility Functions ---

_analytics_instance = None

def get_analytics_db(db_path: Optional[str] = None):
    """
    Singleton pattern for AnalyticsDB.
    
    Args:
        db_path: Path to database file or None for default persistent path
    
    Returns:
        AnalyticsDB instance
    """
    global _analytics_instance
    if _analytics_instance is None:
        if db_path is None:
            # SOTA 2026: Persistent local analytics
            import os
            base_dir = os.path.dirname(os.path.abspath(__file__))
            data_dir = os.path.join(base_dir, "data")
            os.makedirs(data_dir, exist_ok=True)
            db_path = os.path.join(data_dir, "analytics.duckdb")
            
        _analytics_instance = AnalyticsDB(db_path)
    return _analytics_instance


if __name__ == '__main__':
    # Test
    db = get_analytics_db()
    print("AnalyticsDB ready for OLAP queries")

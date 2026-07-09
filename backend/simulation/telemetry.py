import sqlite3
import pickle
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class TelemetryLogger:
    """
    Telemetry logger for trading simulation runs.
    Logs simulation metrics to a local SQLite database and persists tick windows
    selectively based on smart sampling conditions (high slippage deviation or high size reduction).
    """
    def __init__(self, db_path: str = "simulation_telemetry.db"):
        # Strip sqlite:/// prefix if present
        if db_path.startswith("sqlite:///"):
            db_path = db_path[len("sqlite:///"):]
            
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS simulation_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        simulation_latency_ms REAL,
                        slippage_error_bps REAL,
                        gate_blocks_count INTEGER,
                        active_regime_distribution INTEGER,
                        actual_deviation_bps REAL,
                        size_reduction_pct REAL,
                        tick_window BLOB
                    )
                """)
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to initialize telemetry database at {self.db_path}: {e}")

    def log_cycle(
        self,
        simulation_latency_ms: float,
        slippage_error_bps: float,
        gate_blocks_count: int,
        active_regime_distribution: int,
        tick_window: Dict[str, Any],
        actual_deviation_bps: float,
        size_reduction_pct: float
    ):
        """
        Log simulation metrics for a trading cycle.
        Applies smart sampling to persist the tick window as a BLOB if thresholds are exceeded.
        """
        tick_window_blob = None
        # Smart sampling: actual_deviation_bps > 5.0 (5 bps deviation) or size_reduction_pct > 0.5 (50% reduction)
        if actual_deviation_bps > 5.0 or size_reduction_pct > 0.5:
            try:
                tick_window_blob = pickle.dumps(tick_window)
                logger.info(
                    f"Smart sampling triggered (deviation={actual_deviation_bps:.2f} bps, "
                    f"reduction={size_reduction_pct*100:.1f}%). Persisted tick window."
                )
            except Exception as e:
                logger.error(f"Failed to serialize tick window: {e}")

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO simulation_logs (
                        simulation_latency_ms,
                        slippage_error_bps,
                        gate_blocks_count,
                        active_regime_distribution,
                        actual_deviation_bps,
                        size_reduction_pct,
                        tick_window
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    simulation_latency_ms,
                    slippage_error_bps,
                    gate_blocks_count,
                    active_regime_distribution,
                    actual_deviation_bps,
                    size_reduction_pct,
                    sqlite3.Binary(tick_window_blob) if tick_window_blob else None
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to insert simulation telemetry log: {e}")

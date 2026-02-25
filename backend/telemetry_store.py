import sqlite3
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / ".telemetry.db"

def init_db():
    """Initialize the SQLite database for telemetry tracing."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correlation_id TEXT,
                agent_name TEXT,
                model_version TEXT,
                latency_ms REAL,
                timestamp TEXT,
                cached BOOLEAN,
                tokens_used INTEGER,
                metadata TEXT
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_correlation_id ON traces (correlation_id)
        ''')
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to initialize telemetry tracing DB: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

def record_trace(telemetry: Any, metadata: Optional[Dict[str, Any]] = None):
    """ Record agent execution span/telemetry into local store. """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO traces (correlation_id, agent_name, model_version, latency_ms, timestamp, cached, tokens_used, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            telemetry.correlation_id,
            telemetry.agent_name,
            telemetry.model_version,
            telemetry.latency_ms,
            telemetry.timestamp,
            telemetry.cached,
            telemetry.tokens_used,
            json.dumps(metadata) if metadata else None
        ))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to record telemetry trace: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

# Initialize upon import
init_db()

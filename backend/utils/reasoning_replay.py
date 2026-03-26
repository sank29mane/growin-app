import pandas as pd
import json
import logging
from typing import List, Dict, Any, Optional
from analytics_db import get_analytics_db

logger = logging.getLogger(__name__)

def get_reasoning_trace(correlation_id: str) -> List[Dict[str, Any]]:
    """
    Retrieve the full reasoning trace for a specific operation from the analytics database.
    
    Args:
        correlation_id: The unique session identifier
        
    Returns:
        List of agent messages in chronological order
    """
    db = get_analytics_db()
    try:
        # Query DuckDB
        query = """
            SELECT agent_name, subject, payload, timestamp 
            FROM agent_telemetry 
            WHERE correlation_id = ?
            ORDER BY timestamp ASC
        """
        df = db.conn.execute(query, (correlation_id,)).fetchdf()
        
        if df.empty:
            return []
            
        # Convert to list of dicts
        trace = []
        for _, row in df.iterrows():
            trace.append({
                "agent": row["agent_name"],
                "event": row["subject"],
                "data": json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"],
                "time": row["timestamp"].isoformat()
            })
        return trace
    except Exception as e:
        logger.error(f"Failed to retrieve reasoning trace: {e}")
        return []

def list_recent_sessions(limit: int = 10) -> pd.DataFrame:
    """List the most recent orchestrated sessions."""
    db = get_analytics_db()
    query = """
        SELECT DISTINCT correlation_id, MIN(timestamp) as start_time, COUNT(*) as events
        FROM agent_telemetry
        GROUP BY correlation_id
        ORDER BY start_time DESC
        LIMIT ?
    """
    return db.conn.execute(query, (limit,)).fetchdf()

if __name__ == "__main__":
    # Quick test
    print("Recent Orchestrator Sessions:")
    print(list_recent_sessions())

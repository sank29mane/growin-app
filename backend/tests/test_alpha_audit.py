import pytest
import asyncio
import pandas as pd
from datetime import datetime, timedelta
from backend.analytics_db import get_analytics_db
from backend.agents.orchestrator_agent import OrchestratorAgent
from unittest.mock import AsyncMock, patch, MagicMock

@pytest.mark.asyncio
async def test_agent_alpha_audit_flow():
    """Verify that agent runs result in alpha performance tracking with specialist breakdown."""
    # Use in-memory for test
    db = get_analytics_db(":memory:")
    
    ticker = "AAPL"
    correlation_id = "test-alpha-session"
    
    # 1. Seed OHLCV data
    now = datetime.now()
    historical_data = [
        {"t": now - timedelta(minutes=5), "o": 150.0, "h": 151.0, "l": 149.0, "c": 150.0, "v": 1000},
        {"t": now + timedelta(days=1), "o": 160.0, "h": 161.0, "l": 159.0, "c": 160.0, "v": 1000},
        {"t": now + timedelta(days=5), "o": 180.0, "h": 181.0, "l": 179.0, "c": 180.0, "v": 1000}
    ]
    db.bulk_insert_ohlcv(ticker, historical_data)
    
    # 2. Seed Telemetry
    db.log_agent_message({
        "id": "msg1", "correlation_id": correlation_id, "sender": "OrchestratorAgent", 
        "subject": "agent_started", "payload": {"query": "Buy AAPL"}, "timestamp": now
    })
    db.log_agent_message({
        "id": "msg2", "correlation_id": correlation_id, "sender": "OrchestratorAgent", 
        "subject": "context_fabricated", "payload": {"ticker": ticker}, "timestamp": now
    })
    db.log_agent_message({
        "id": "msg3", "correlation_id": correlation_id, "sender": "QuantAgent", 
        "subject": "agent_complete", "payload": {"success": True}, "timestamp": now
    })
    
    # 3. Trigger Audit
    db.calculate_agent_alpha(correlation_id)
    
    # 4. Verify Results
    perf = db.conn.execute("SELECT * FROM agent_performance WHERE correlation_id = ?", (correlation_id,)).fetchdf()
    assert not perf.empty
    
    # 5. Check Summary Metrics with Specialist Breakdown
    metrics = db.get_agent_alpha_metrics(ticker)
    assert metrics['total_sessions'] == 1
    assert 'QuantAgent' in metrics['specialists']
    assert abs(metrics['specialists']['QuantAgent']['avg_1d'] - 0.066) < 0.01
    
    print(f"Alpha Audit Verified: QuantAgent 1d Alpha: {metrics['specialists']['QuantAgent']['avg_1d']:.3f}")

if __name__ == "__main__":
    asyncio.run(test_agent_alpha_audit_flow())

from pydantic import BaseModel, Field
from backend.simulation.engine import PreFlightSimulator
from backend.simulation.models import MarketImpactModel
from backend.simulation.swarm_gate import RiskSwarmGate
from backend.simulation.telemetry import TelemetryLogger

class PreFlightDecision(BaseModel):
    """
    Pydantic schema for pre-flight trading decisions.
    """
    approved: bool = Field(..., description="True if the trade passes risk checks.")
    simulated_fill_price: float = Field(..., description="Estimated fill price including slippage.")
    scaled_size: float = Field(..., description="Final size in shares/units after swarm gate scaling.")
    regime_id: int = Field(..., description="The detected market regime code.")
    latency_ms: float = Field(..., description="Time taken to simulate the trade.")
    simulator_drawdown_pct: float = Field(..., description="Current simulated portfolio drawdown.")

__all__ = [
    "PreFlightDecision",
    "PreFlightSimulator",
    "MarketImpactModel",
    "RiskSwarmGate",
    "TelemetryLogger",
]

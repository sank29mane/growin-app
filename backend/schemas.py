from pydantic import BaseModel, Field, ConfigDict
from typing import List, Dict, Any, Optional
from decimal import Decimal

# --- Goal Planning Models ---

class GoalPlanContext(BaseModel):
    initial_capital: float = Field(..., description="Initial capital amount")
    target_returns_percent: float = Field(..., description="Target annual return percentage")
    duration_years: float = Field(..., description="Investment duration in years")
    risk_profile: str = Field(..., description="Risk profile: LOW, MEDIUM, HIGH, AGGRESSIVE_PLUS")
    
    # Optional fields for future expansion
    purpose: str = Field("General Investment", description="Purpose of the goal")

class InstrumentWeight(BaseModel):
    ticker: str
    weight: float

class GoalExecutionImplementation(BaseModel):
    type: str = Field(..., description="Implementation type, e.g., TRADING212_PIE")
    name: str = Field(..., description="Name of the Pie or portfolio")
    action: Optional[str] = "create"

class GoalExecutionRequest(BaseModel):
    model_config = ConfigDict(extra='ignore')
    
    implementation: GoalExecutionImplementation
    suggested_instruments: List[InstrumentWeight]

# --- Account Models ---

class SetActiveAccountRequest(BaseModel):
    account_type: str = Field(..., description="Account type: invest, isa, cfd")

# --- Search Models ---
# Response models can also be defined here if needed
class SearchResult(BaseModel):
    ticker: str
    name: str

# --- Analysis Models ---
class AnalysisRequest(BaseModel):
    ticker: str
    timeframe: str = "1Day"

# --- Agent Models ---
class MLXDownloadRequest(BaseModel):
    repo_id: str = Field(..., description="HuggingFace repository ID")

# --- Math Delegation Models ---

class MathScriptRequest(BaseModel):
    query: str = Field(..., description="The user's math-related question or simulation request")
    context_data: Dict[str, Any] = Field(default_factory=dict, description="Numerical data, price history, or parameters")
    required_stats: List[str] = Field(default_factory=list, description="List of specific statistical measures needed (e.g., RSI, Sharpe)")

class MathScriptResponse(BaseModel):
    script: str = Field(..., description="The generated Python/MLX script ready for sandbox execution")
    explanation: str = Field(..., description="Human-readable explanation of the math strategy")
    engine_requirement: str = Field("npu", description="Hardware requirement for execution")

# --- SOTA AI & Strategy Models ---

class ReasoningStep(BaseModel):
    agent: str = Field(..., description="Name of the agent performing the step")
    action: str = Field(..., description="Description of the action/thought")
    content: Optional[str] = Field(None, description="Detailed reasoning or data findings")
    timestamp: float = Field(default_factory=lambda: 0.0)

class AgentEvent(BaseModel):
    event_type: str = Field(..., description="Type of event: status_update, reasoning_step, final_result")
    agent: str
    status: str
    step: Optional[ReasoningStep] = None
    timestamp: float = Field(default_factory=lambda: 0.0)

class AIStrategyResponse(BaseModel):
    strategy_id: str
    title: str
    summary: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning_trace: List[ReasoningStep]
    instruments: List[InstrumentWeight]
    risk_assessment: str
    last_updated: float


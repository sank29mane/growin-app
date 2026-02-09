from pydantic import BaseModel, Field, root_validator
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
    implementation: GoalExecutionImplementation
    suggested_instruments: List[InstrumentWeight]
    
    # Allow extra fields for flexibility if agent returns more data
    class Config:
        extra = "ignore" 

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


"""
Agent status and health check endpoints
"""

from fastapi import APIRouter
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/agents/status")
async def get_agents_status():
    """
    Get current status of all specialist agents from the StatusManager.
    """
    from status_manager import status_manager
    live_statuses = status_manager.get_all_statuses()
    
    # Enrich with metadata
    response = {
        "coordinator": live_statuses.get("coordinator", {"status": "offline"}),
        "specialists": {
            "quant_agent": live_statuses.get("quant_agent", {"status": "offline"}),
            "portfolio_agent": live_statuses.get("portfolio_agent", {"status": "offline"}),
            "forecasting_agent": live_statuses.get("forecasting_agent", {"status": "offline"}),
            "research_agent": live_statuses.get("research_agent", {"status": "offline"})
        },
        "decision_agent": live_statuses.get("decision_agent", {"status": "offline"})
    }
    
    # Special check for forecasting model loading
    try:
        from forecaster import get_forecaster
        forecaster = get_forecaster()
        if forecaster.loading:
            response["specialists"]["forecasting_agent"]["status"] = "loading"
            response["specialists"]["forecasting_agent"]["detail"] = "Loading TTM-R2 weights..."
    except:
        pass

    return response


@router.get("/api/models/available")
async def get_available_models():
    """
    Get list of available LLM models for Decision and Coordinator agents.
    
    Returns models from OpenAI, Anthropic, Google, Ollama, and LM Studio
    with their descriptions and API key requirements.
    
    Returns:
        Dict with decision_models and coordinator_models lists
    """
    from model_config import DECISION_MODELS, COORDINATOR_MODELS
    
    return {
        "decision_models": [
            {
                "name": name,
                **info
            }
            for name, info in DECISION_MODELS.items()
        ],
        "coordinator_models": [
            {
                "name": name,
                **info
            }
            for name, info in COORDINATOR_MODELS.items()
        ]
    }

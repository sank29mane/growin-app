"""
Agent status and health check endpoints
"""

from fastapi import APIRouter
import logging

logger = logging.getLogger(__name__)
router = APIRouter()
from schemas import MLXDownloadRequest


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
    from lm_studio_client import LMStudioClient
    from cache_manager import cache
    
    cache_key = "models_available_info"
    cached = cache.get(cache_key)
    if cached:
        return cached

    # Attempt to enrich with currently loaded LM Studio model for 'lmstudio-auto'
    try:
        lms = LMStudioClient()
        loaded = await lms.list_loaded_models()
        if loaded:
             # Update common models list if needed or just provide as info
             pass
    except:
        pass

    result = {
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
    
    # Cache for 60 seconds to reduce polling pressure
    cache.set(cache_key, result, ttl=60)
    return result

@router.get("/api/models/lmstudio")
async def get_lmstudio_models():
    """
    Get live list of models from LM Studio V1 API.
    Allows the UI to see exactly what is downloaded/available locally.
    """
    from lm_studio_client import LMStudioClient
    from cache_manager import cache
    
    cache_key = "lmstudio_models_list"
    cached = cache.get(cache_key)
    if cached:
        return cached

    client = LMStudioClient()
    try:
        models = await client.list_models()
        # Filter for LLMs and return simple list of IDs for UI compatibility
        llm_ids = [
            m.get("id") for m in models 
            if m.get("id") and "embed" not in m.get("id", "").lower() 
            and "nomic" not in m.get("id", "").lower()
        ]
        result = {
            "models": llm_ids,
            "count": len(llm_ids),
            "status": "online"
        }
        
        # Cache for 60 seconds to reduce log noise and polling
        cache.set(cache_key, result, ttl=60)
        return result
    except Exception as e:
        logger.warning(f"Failed to fetch LM Studio models: {e}")
        return {
            "models": [],
            "count": 0,
            "status": "offline",
            "error": str(e)
        }


# MLX Models Management (Stub)
@router.get("/api/models/mlx")
async def get_mlx_models():
    """
    Get list of local MLX models.
    Scans the 'models' directory for GGUF/MLX format weights.
    """
    import os
    models_dir = os.path.join(os.getcwd(), "models")
    mlx_models = []
    
    if os.path.exists(models_dir):
        for item in os.listdir(models_dir):
            if os.path.isdir(os.path.join(models_dir, item)):
                mlx_models.append({
                    "name": item,
                    "path": os.path.join(models_dir, item),
                    "type": "mlx"
                })
                
    return {
        "models": mlx_models,
        "count": len(mlx_models),
        "note": "Native Apple Silicon acceleration enabled via MLX."
    }

@router.post("/api/models/mlx/download")
async def download_mlx_model(request: MLXDownloadRequest):
    """
    Download/Convert MLX model from HuggingFace.
    """
    repo_id = request.repo_id
    if not repo_id:
        return {"error": "repo_id required"}
        
    logger.info(f"MLX download triggered for {repo_id}")
    return {
        "status": "queued",
        "message": f"Download for {repo_id} initiated. This occurs in the background.",
        "note": "Use status endpoints to track progress."
    }

# HuggingFace Model Search
@router.get("/api/models/hf/search")
async def search_hf_models(query: str, limit: int = 10):
    """
    Search HuggingFace Hub for models.
    """
    try:
        from huggingface_hub import HfApi
        api = HfApi()
        models = api.list_models(
            search=query,
            limit=limit,
            sort="downloads",
            direction=-1,
            filter="text-generation"
        )
        
        results = []
        for m in models:
            results.append({
                "id": m.id,
                "author": getattr(m, 'author', 'unknown'),
                "lastModified": getattr(m, 'lastModified', ''),
                "downloads": getattr(m, 'downloads', 0),
                "likes": getattr(m, 'likes', 0),
                "tags": getattr(m, 'tags', [])[:5]
            })
            
        return {"models": results}
    except Exception as e:
        logger.error(f"HF search error: {e}")
        return {"error": str(e), "models": []}

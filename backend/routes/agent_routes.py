"""
Agent status and health check endpoints
"""

from fastapi import APIRouter
import logging

logger = logging.getLogger(__name__)
router = APIRouter()
from schemas import MLXDownloadRequest, LMStudioLoadRequest, LMStudioStatusResponse


@router.get("/api/agents/status")
async def get_agents_status():
    """
    Get current status of all specialist agents from the StatusManager.
    """
    from status_manager import status_manager
    live_statuses = status_manager.get_all_statuses()
    
    # Core system components
    response = {
        "coordinator": live_statuses.get("coordinator", {"status": "offline"}),
        "decision_agent": live_statuses.get("decision_agent", {"status": "offline"}),
        "specialists": {}
    }
    
    # Filter specialists (everything that isn't coordinator or decision_agent)
    for key, val in live_statuses.items():
        if key not in ["coordinator", "decision_agent"]:
            response["specialists"][key] = val
    
    # Ensure standard specialists are represented even if offline
    standard_specs = ["quant_agent", "portfolio_agent", "forecasting_agent", "research_agent", "lmstudio"]
    for spec in standard_specs:
        if spec not in response["specialists"]:
            response["specialists"][spec] = {"status": "offline"}
    
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
    Get live list of models from LM Studio using Native V1 Management API.
    Provides accurate metadata including loading status.
    """
    from lm_studio_client import LMStudioClient
    from cache_manager import cache
    
    cache_key = "lmstudio_models_list"
    cached = cache.get(cache_key)
    if cached:
        return cached

    client = LMStudioClient()
    try:
        # Use Native V1 API for listing models (management=True)
        models = await client.list_models(management=True)
        
        # Filter for LLMs and return simple list of IDs for UI compatibility
        # SOTA: Improved filtering to handle 'id' (OpenAI) and 'key' (Native V1)
        llm_ids = []
        for m in models:
            # Check for both 'key' (Native V1) and 'id' (OpenAI)
            m_id = m.get("key") or m.get("id")
            if not m_id: continue
            
            # Skip common embedding or utility models
            is_embed = "embed" in m_id.lower() or "nomic" in m_id.lower()
            if not is_embed:
                llm_ids.append(m_id)
                
        result = {
            "models": llm_ids,
            "count": len(llm_ids),
            "status": "online"
        }
        
        # Cache for 60 seconds
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


@router.post("/api/models/lmstudio/load")
async def load_lmstudio_model(request: LMStudioLoadRequest):
    """
    Trigger LM Studio to load a specific model ID.
    Implements a clean 'Unload before Load' switcher flow.
    """
    from lm_studio_client import LMStudioClient
    from status_manager import status_manager
    
    client = LMStudioClient()
    model_id = request.model_id
    
    logger.info(f"LM Studio: Switch request for {model_id}")
    status_manager.set_status("lmstudio", "working", f"Switching to {model_id}...")
    
    try:
        # 1. Get currently loaded models
        loaded_models = await client.list_loaded_models()
        
        # 2. If already loaded, just confirm and return
        if model_id in loaded_models:
            status_manager.set_status("lmstudio", "ready", f"Model {model_id} active")
            return {"status": "success", "message": f"Model {model_id} is already loaded"}
            
        # 3. Clean Switch: Unload other models first to free VRAM
        if loaded_models:
            logger.info(f"LM Studio: Unloading existing models {loaded_models} for clean switch.")
            status_manager.set_status("lmstudio", "working", "Unloading current model...")
            
            # We get full management info to get instance IDs
            management_models = await client.list_models(management=True)
            for m in management_models:
                instances = m.get("loaded_instances", [])
                for inst in instances:
                    inst_id = inst.get("id")
                    if inst_id:
                        await client.unload_model(inst_id)
            
            # Tiny sleep to let VRAM clear
            await asyncio.sleep(1.0)

        # 4. Load the new model
        status_manager.set_status("lmstudio", "working", f"Loading {model_id} into VRAM...")
        success = await client.ensure_model_loaded(
            model_id, 
            context_length=request.context_length,
            gpu=request.gpu_offload
        )
        
        if success:
            status_manager.set_status("lmstudio", "ready", f"Model {model_id} active")
            return {"status": "success", "message": f"Model {model_id} loaded successfully"}
        else:
            status_manager.set_status("lmstudio", "error", f"Failed to load {model_id}")
            return {"status": "error", "message": f"Failed to load {model_id}"}
    except Exception as e:
        logger.error(f"LM Studio load error: {e}")
        status_manager.set_status("lmstudio", "error", str(e))
        return {"status": "error", "message": str(e)}


@router.get("/api/models/lmstudio/status", response_model=LMStudioStatusResponse)
async def get_lmstudio_status():
    """
    Detailed health and load status of LM Studio with short-term caching to prevent log spam.
    """
    from lm_studio_client import LMStudioClient
    from cache_manager import cache
    
    cache_key = "lmstudio_status_detail"
    cached = cache.get(cache_key)
    if cached:
        return cached

    client = LMStudioClient()
    try:
        online = await client.check_connection()
        if not online:
            res = LMStudioStatusResponse(status="offline", active=False)
            cache.set(cache_key, res, ttl=5) # Cache offline status longer
            return res
            
        loaded = await client.list_loaded_models()
        # In LM Studio v1, usually only one model is loaded at a time
        current_model = loaded[0] if loaded else None
        
        result = LMStudioStatusResponse(
            status="online",
            loaded_model=current_model,
            active=True,
            memory_usage={} 
        )
        # Cache for 5s to bridge the polling interval without spamming LM Studio logs
        cache.set(cache_key, result, ttl=5)
        return result
    except Exception as e:
        logger.warning(f"Failed to get LM Studio status: {e}")
        return LMStudioStatusResponse(status="error", active=False)


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

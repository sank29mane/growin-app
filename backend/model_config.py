"""
Model Configuration for Decision Agent
Allows easy model switching and supports multiple providers.
Priority is given to Apple Silicon native local models (MLX/CoreML).
Cloud models (Gemini) are used for CLI assistance and testing fallbacks.
"""

import os
from pathlib import Path

# Get the absolute path to the backend directory
BACKEND_DIR = Path(__file__).parent.absolute()

# PRODUCTION PRIORITY: Native Apple Silicon Models
# These are the primary models for the application's core logic.
LOCAL_MLX_MODELS = {
    "native-mlx": {
        "provider": "mlx",
        "model_path": str(BACKEND_DIR / "models/mlx/lmstudio-community--LFM2.5-1.2B-Instruct-MLX-8bit"),
        "description": "Native MLX - Optimized for Apple Silicon (Primary)",
        "requires_key": None
    },
    "granite-small": {
        "provider": "mlx",
        "model_id": "lmstudio-community/granite-4.0-h-small-MLX-8bit",
        "description": "Granite 4.0 Small - Native SOTA coordinator"
    }
}

# ASSISTANCE & TESTING: Cloud Models (Latest Preview Versions)
# These are utilized by the Gemini CLI and for CI/Testing fallbacks.
CLOUD_ASSISTANCE_MODELS = {
    "gemini-1.5-pro-preview": {
        "provider": "google",
        "model_id": "gemini-1.5-pro-002",  # Updated to latest stable preview
        "description": "Gemini 1.5 Pro Preview - Master Planner (CLI/Test Only)",
        "requires_key": "GOOGLE_API_KEY"
    },
    "gemini-1.5-flash-preview": {
        "provider": "google",
        "model_id": "gemini-1.5-flash-002", # Updated to latest stable preview
        "description": "Gemini 1.5 Flash Preview - Structural Executor (CLI/Test Only)",
        "requires_key": "GOOGLE_API_KEY"
    },
    "claude-3.5-sonnet": {
        "provider": "anthropic",
        "description": "Claude 3.5 Sonnet - Secondary Analysis",
        "requires_key": "ANTHROPIC_API_KEY"
    }
}

# LEGACY & OTHER LOCAL PROVIDERS
OTHER_MODELS = {
    "gpt-4o": {
        "provider": "openai",
        "description": "GPT-4o - Fastest OpenAI model",
        "requires_key": "OPENAI_API_KEY"
    },
    "lmstudio-auto": {
        "provider": "lmstudio",
        "description": "LM Studio - Auto-detect loaded model",
        "requires_key": None
    },
    "gemma-2-27b-it": {
        "provider": "ollama",
        "description": "Gemma 2 27B - Local (requires Ollama)",
        "requires_key": None
    }
}

# Merge all models into the DECISION_MODELS registry
DECISION_MODELS = {**LOCAL_MLX_MODELS, **CLOUD_ASSISTANCE_MODELS, **OTHER_MODELS}

COORDINATOR_MODELS = {
    "granite-tiny": {
        "provider": "mlx",
        "model_path": str(BACKEND_DIR / "models/mlx/lmstudio-community--LFM2.5-1.2B-Instruct-MLX-8bit"),
        "description": "Granite 4.0 Tiny - Ultra-lightweight coordinator",
        "temperature": 0,
        "max_tokens": 512,
        "top_p": 1.0
    },
    **LOCAL_MLX_MODELS
}


def get_available_models():
    """Return list of available decision models"""
    return list(DECISION_MODELS.keys())


def get_model_info(model_name: str):
    """Get information about a specific model from either Decision or Coordinator registries"""
    # Detect CI environment to potentially force cloud fallback if local hardware is missing
    is_ci = os.getenv("CI", "false").lower() == "true"
    
    info = DECISION_MODELS.get(model_name) or COORDINATOR_MODELS.get(model_name) or {}
    
    if is_ci and info.get("provider") == "mlx":
        # Log or flag that local model is being requested in CI without hardware acceleration
        pass
        
    return info

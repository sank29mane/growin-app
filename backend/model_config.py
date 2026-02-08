"""
Model Configuration for Decision Agent
Allows easy model switching and supports multiple providers
"""

from pathlib import Path

# Get the absolute path to the backend directory
BACKEND_DIR = Path(__file__).parent.absolute()

DECISION_MODELS = {
    "gpt-4o": {
        "provider": "openai",
        "description": "GPT-4o - Fastest OpenAI model",
        "requires_key": "OPENAI_API_KEY"
    },
    "gpt-4-turbo": {
        "provider": "openai",
        "description": "GPT-4 Turbo - High reasoning",
        "requires_key": "OPENAI_API_KEY"
    },
    "claude-3.5-sonnet": {
        "provider": "anthropic",
        "description": "Claude 3.5 Sonnet - Best for analysis",
        "requires_key": "ANTHROPIC_API_KEY"
    },
    "gemini-1.5-pro": {
        "provider": "google",
        "description": "Gemini 1.5 Pro - Google's best",
        "requires_key": "GOOGLE_API_KEY"
    },
    "nemotron-3-nano": {
        "provider": "lmstudio",
        "model_id": "nvidia/nemotron-3-nano",
        "description": "Nvidia Nemotron 3 Nano - User User Preference",
        "requires_key": None
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
    },
    "mistral-large": {
        "provider": "ollama",
        "description": "Mistral Large - Local (requires Ollama)",
        "requires_key": None
    },
    "native-mlx": {
        "provider": "mlx",
        "model_path": str(BACKEND_DIR / "models/mlx/lmstudio-community--LFM2.5-1.2B-Instruct-MLX-8bit"),
        "description": "Native MLX - Optimized for Apple Silicon",
        "requires_key": None
    },
    "gpt-oss-20b": {
        "provider": "lmstudio",
        "model_id": "gpt-oss-20b",
        "description": "GPT-OSS 20B - Local high-performance model",
        "requires_key": None
    }
}

COORDINATOR_MODELS = {
    "granite-tiny": {
        "provider": "mlx",
        "model_path": str(BACKEND_DIR / "models/mlx/granite-4.0-h-tiny-MLX-8bit"),
        "description": "Granite 4.0 Tiny - Ultra-lightweight coordinator",
        "temperature": 0,
        "max_tokens": 512,
        "top_p": 1.0
    },
    "granite-small": {
        "provider": "mlx",
        "model_id": "lmstudio-community/granite-4.0-h-small-MLX-8bit",
        "description": "Granite 4.0 Small - Native SOTA coordinator"
    },
    "mistral": {
        "provider": "ollama",
        "description": "Mistral 7B - Fast coordinator (Ollama fallback)"
    }
}


def get_available_models():
    """Return list of available decision models"""
    return list(DECISION_MODELS.keys())


def get_model_info(model_name: str):
    """Get information about a specific model from either Decision or Coordinator registries"""
    if model_name in DECISION_MODELS:
        return DECISION_MODELS[model_name]
    if model_name in COORDINATOR_MODELS:
        return COORDINATOR_MODELS[model_name]
    return {}

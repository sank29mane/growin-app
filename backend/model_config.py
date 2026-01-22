"""
Model Configuration for Decision Agent
Allows easy model switching and supports multiple providers
"""

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
    "lmstudio:auto": {
        "provider": "lmstudio",
        "description": "LM Studio - Whatever model you have loaded",
        "requires_key": None
    },
    "native-mlx": {
        "provider": "mlx",
        "model_path": "models/mlx/lmstudio-community--LFM2.5-1.2B-Instruct-MLX-8bit",
        "description": "Native MLX - Optimized for Apple Silicon",
        "requires_key": None
    }
}

COORDINATOR_MODELS = {
    "granite-tiny": {
        "provider": "mlx",
        "model_path": "models/mlx/granite-4.0-h-tiny-MLX-8bit",
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
    """Get information about a specific model"""
    return DECISION_MODELS.get(model_name, {})

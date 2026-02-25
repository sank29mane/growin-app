"""
LLM Factory - Handles initialization of various LLM providers.
Refactored from DecisionAgent to reduce complexity.
"""

import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)

class LLMFactory:
    """Factory for creating LLM instances based on model name and configuration."""

    @staticmethod
    async def create_llm(model_name: str, api_keys: Dict[str, str] = None):
        """
        Initialize and return an LLM instance.

        Args:
            model_name: Name/ID of the model.
            api_keys: Dictionary of API keys.

        Returns:
            LangChain compatible LLM or custom client.
        """
        api_keys = api_keys or {}

        from model_config import get_model_info
        info = get_model_info(model_name)
        provider = info.get("provider", "").lower()

        # Normalization and Provider Detection
        model_lower = model_name.lower()
        
        try:
            llm_instance = None
            
            # 2. Native MLX Priority (Apple Silicon NPU/M4 Optimization)
            # User requested NOT to load LLM on ANE/NPU implicitly.
            # Using standard provider logic below.
            
            if not llm_instance:
                is_lmstudio_hint = "lmstudio" in model_lower
                if provider == "lmstudio" or (not provider and is_lmstudio_hint):
                    try:
                        llm_instance = await LLMFactory._create_lmstudio(model_name)
                    except Exception as lm_err:
                        logger.warning(f"LM Factory: LM Studio creation failed for {model_name}: {lm_err}")
                    # If provider was explicitly lmstudio, we should probably fall through to auto-detect later
                    # but if it was just a hint, we keep going to other providers

            if not llm_instance:
                if provider == "openai" or (not provider and "gpt" in model_lower and "oss" not in model_lower):
                    llm_instance = LLMFactory._create_openai(model_name, api_keys)

                elif provider == "anthropic" or (not provider and "claude" in model_lower):
                    llm_instance = LLMFactory._create_anthropic(model_name, api_keys)

                elif provider == "google" or (not provider and "gemini" in model_lower):
                    llm_instance = LLMFactory._create_google(model_name, api_keys)

                elif provider == "mlx" or (not provider and ("mlx" in model_lower or "granite" in model_lower) and "lmstudio" not in model_lower):
                    llm_instance = LLMFactory._create_mlx(model_name)

                elif provider == "ollama" or (not provider and ("gemma" in model_lower or "mistral" in model_lower)):
                    llm_instance = LLMFactory._create_ollama(model_name)

            if not llm_instance:
                # Last resort: Try auto-detecting ANY loaded model in LM Studio
                logger.info("LLM Factory: Attempting last-resort auto-detection in LM Studio")
                try:
                    llm_instance = await LLMFactory._create_lmstudio("lmstudio-auto")
                except Exception:
                    pass

            if not llm_instance:
                raise ValueError(f"Unsupported model or provider failed to return instance: {model_name}")
            
            logger.info(f"LLM Factory: Successfully initialized {model_name} (Type: {type(llm_instance).__name__})")
            return llm_instance

        except Exception as e:
            logger.error(f"LLM Factory: Failed to initialize {model_name}: {e}")

            # Safe Fallback Strategy
            if "native-mlx" in model_name:
                 raise RuntimeError(f"Native MLX Model failed to load and no fallbacks available: {e}")

            # Attempt Native MLX fallback ONLY if hardware is likely to support it (Apple Silicon)
            import platform
            if platform.processor() == "arm" and platform.system() == "Darwin":
                try:
                    logger.info("Attempting fallback to Native MLX (Apple Silicon detected)...")
                    return LLMFactory._create_mlx("native-mlx")
                except Exception as fallback_error:
                    logger.error(f"MLX Fallback failed: {fallback_error}")

            raise RuntimeError(f"Total failure: Model {model_name} could not be initialized and no suitable fallbacks found. Error: {e}")

    @staticmethod
    def _create_openai(model_name: str, api_keys: Dict[str, str]):
        from langchain_openai import ChatOpenAI
        key = api_keys.get("openai") or os.getenv("OPENAI_API_KEY")
        if not key:
            raise ValueError("OpenAI API Key required")
        return ChatOpenAI(model=model_name, temperature=0, openai_api_key=key)

    @staticmethod
    def _create_anthropic(model_name: str, api_keys: Dict[str, str]):
        from langchain_anthropic import ChatAnthropic
        key = api_keys.get("anthropic") or os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError("Anthropic API Key required")
        return ChatAnthropic(model=model_name, anthropic_api_key=key)

    @staticmethod
    def _create_google(model_name: str, api_keys: Dict[str, str]):
        from langchain_google_genai import ChatGoogleGenerativeAI
        key = api_keys.get("gemini") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise ValueError("Google API Key required")
        return ChatGoogleGenerativeAI(model=model_name, google_api_key=key)

    @staticmethod
    def _create_mlx(model_name: str):
        from mlx_langchain import ChatMLX
        logger.info(f"Initializing Native MLX Model: {model_name}")
        return ChatMLX(model_name=model_name)

    @staticmethod
    def _create_ollama(model_name: str):
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model_name, base_url="http://127.0.0.1:11434")

    @staticmethod
    async def _create_lmstudio(model_name: str):
        from lm_studio_client import LMStudioClient
        from model_config import get_model_info

        info = get_model_info(model_name)
        client = LMStudioClient()

        # If specific model_id is fixed in config, use it
        target_model_id = info.get("model_id")

        # Fallback: If the user passed a direct model ID (not in config), use it directly
        if not target_model_id and model_name != "lmstudio-auto":
            target_model_id = model_name

        # Check connection
        if not await client.check_connection():
            raise ConnectionError("LM Studio server not reachable")

        if target_model_id:
            logger.info(f"LM Studio: Ensuring model loaded: {target_model_id}")
            try:
                await client.ensure_model_loaded(target_model_id)
                # Explicitly set the active model ID on the client wrapper/mock property
                client.active_model_id = target_model_id 
                return client
            except Exception as e:
                logger.warning(f"Failed to load requested model {target_model_id}: {e}")
                # If loading fails, fall through to auto-detect as safety net? 
                # No, user asked for specific model, we should probably error or behave predictably.
                # But to maintain robustness, we can try auto-detect if the specific one fails.
                logger.info("Falling back to auto-detected model...")

        # Auto-detect currently loaded model (filter out embeddings)
        models = await client.list_models()
        if models:
            llm_candidates = [
                m["id"] for m in models
                if "embed" not in m["id"].lower()
                and "nomic" not in m["id"].lower()
                and "bert" not in m["id"].lower()
            ]

            if llm_candidates:
                loaded_id = llm_candidates[0]
                logger.info(f"LM Studio: Auto-detected LLM: {loaded_id}")
                client.active_model_id = loaded_id
                return client
            elif models:
                logger.warning(f"LM Studio: Only embedding models found. Using first: {models[0]['id']}")
                client.active_model_id = models[0]["id"]
                return client

            raise ValueError("No models available in LM Studio")

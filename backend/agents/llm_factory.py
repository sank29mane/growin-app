"""
LLM Factory - Handles initialization of various LLM providers.
Refactored from DecisionAgent to reduce complexity.
"""

import logging
import os
from typing import Optional, Dict, Any

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
        
        try:
            if "gpt" in model_name.lower():
                return LLMFactory._create_openai(model_name, api_keys)
            
            elif "claude" in model_name.lower():
                return LLMFactory._create_anthropic(model_name, api_keys)
            
            elif "gemini" in model_name.lower():
                return LLMFactory._create_google(model_name, api_keys)
            
            elif "mlx" in model_name.lower() or "granite" in model_name.lower():
                return LLMFactory._create_mlx(model_name)

            elif "lmstudio" in model_name.lower() or "nemotron" in model_name.lower():
                return await LLMFactory._create_lmstudio(model_name)
            
            elif "gemma" in model_name.lower() or "mistral" in model_name.lower():
                return LLMFactory._create_ollama(model_name)

            else:
                raise ValueError(f"Unsupported model: {model_name}")

        except Exception as e:
            logger.error(f"LLM Factory failed to initialize {model_name}: {e}")
            # Fallback handling
            if "native-mlx" in model_name:
                 raise RuntimeError(f"Native MLX Model failed to load: {e}")
            
            # Attempt Native MLX fallback for critical failures
            try:
                logger.info("Attempting fallback to Native MLX...")
                return LLMFactory._create_mlx("native-mlx")
            except Exception as fallback_error:
                raise RuntimeError(f"Total failure (Original: {e}, Fallback: {fallback_error})")

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
        
        # Check connection
        if not await client.check_connection():
            raise ConnectionError("LM Studio server not reachable")
        
        if target_model_id:
            logger.info(f"LM Studio: Ensuring model loaded: {target_model_id}")
            await client.ensure_model_loaded(target_model_id)
            return client
        else:
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
                    # Note: We return the client, but the caller needs to know the model ID
                    # The client handles 'chat' with model_id. 
                    # Wrapper needed? 
                    # DecisionAgent expects an object with 'ainvoke' or 'chat'.
                    # LMStudioClient has 'chat'.
                    # But DecisionAgent stores self.model_name.
                    # We might need to return (client, resolved_model_name) or
                    # attach the model name to the client instance?
                    client.active_model_id = loaded_id # Hacky attachment?
                    return client
                elif models:
                    logger.warning(f"LM Studio: Only embedding models found. Using first: {models[0]['id']}")
                    client.active_model_id = models[0]["id"]
                    return client
            
            raise ValueError("No models available in LM Studio")

"""
LangChain Wrapper for MLX Inference Engine
Allows MLX models to be used as LangChain BaseChatModel
"""
from typing import Any, List, Optional, Dict
import logging
import asyncio

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult

from mlx_engine import get_mlx_engine
from model_config import DECISION_MODELS, COORDINATOR_MODELS

logger = logging.getLogger(__name__)

class ChatMLX(BaseChatModel):
    """
    MLX Chat Model Wrapper for LangChain.
    Targeting specific interaction with the specialized MLXInferenceEngine.
    """
    
    model_name: str = "mlx-model"
    temperature: float = 0.0  # Default to deterministic
    max_tokens: int = 2048
    top_p: float = 1.0

    @property
    def _llm_type(self) -> str:
        return "mlx-chat"

    def _resolve_model_path(self, target_model: str) -> str:
        """Resolve friendly model name to actual path."""
        if target_model == "mlx-model":
            return "mlx-community/Mistral-7B-Instruct-v0.3-4bit"

        for config in [DECISION_MODELS, COORDINATOR_MODELS]:
            if target_model in config:
                info = config[target_model]
                return info.get("model_path") or info.get("model_id", target_model)
        
        return target_model

    def _build_chatml_prompt(self, messages: List[BaseMessage]) -> str:
        """Construct ChatML formatted prompt from messages."""
        role_map = {
            SystemMessage: "system",
            HumanMessage: "user",
            AIMessage: "assistant",
        }
        
        prompt_parts = []
        for msg in messages:
            role = role_map.get(type(msg), "user")
            prompt_parts.append(f"<|im_start|>{role}\n{msg.content}<|im_end|>\n")
        
        prompt_parts.append("<|im_start|>assistant\n")
        return "".join(prompt_parts)

    def _ensure_model(self, engine: Any) -> Optional[str]:
        """
        Ensure the correct model is loaded. 
        Returns an error message if loading fails, otherwise None.
        """
        target_path = self._resolve_model_path(self.model_name)
        current_path = getattr(engine, 'current_model_path', None)
        
        if not engine.is_loaded() or current_path != target_path:
            logger.info(f"Switching MLX model to {target_path}...")
            # load_model is sync and IO bound
            if not engine.load_model(target_path):
                return f"Error: Failed to load MLX model {target_path}."
        
        return None

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Generate response using MLX Engine (Sync)."""
        engine = get_mlx_engine()
        
        if error_msg := self._ensure_model(engine):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=error_msg))])
            
        prompt = self._build_chatml_prompt(messages)
        
        try:
            from mlx_lm.sample_utils import make_sampler
            sampler = make_sampler(temp=self.temperature)
            
            # Run async generate in event loop
            response_text = asyncio.run(engine.generate(
                prompt=prompt,
                max_tokens=self.max_tokens,
                sampler=sampler
            ))
            
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=response_text))])
            
        except Exception as e:
            logger.error(f"MLX generation failed: {e}")
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"Error generating response: {e}"))])

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Async Generate response using MLX Engine."""
        engine = get_mlx_engine()
        
        if error_msg := self._ensure_model(engine):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=error_msg))])

        prompt = self._build_chatml_prompt(messages)

        try:
            from mlx_lm.sample_utils import make_sampler
            sampler = make_sampler(temp=self.temperature)

            response_text = await engine.generate(
                prompt=prompt,
                max_tokens=self.max_tokens,
                sampler=sampler,
            )
            
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=response_text))])
            
        except Exception as e:
            logger.error(f"MLX async generation failed: {e}")
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"Error generating response: {e}"))])

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        return {"model_name": self.model_name}

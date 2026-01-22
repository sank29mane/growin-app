"""
LangChain Wrapper for MLX Inference Engine
Allows MLX models to be used as LangChain BaseChatModel
"""
from typing import Any, List, Optional, Dict

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
import logging

from mlx_engine import get_mlx_engine
from model_config import DECISION_MODELS, COORDINATOR_MODELS

logger = logging.getLogger(__name__)

class ChatMLX(BaseChatModel):
    """
    MLX Chat Model Wrapper for LangChain
    Targeting specific interaction with the specialized MLXInferenceEngine
    """
    
    model_name: str = "mlx-model"
    temperature: float = 0.0 # Default to deterministic
    max_tokens: int = 2048
    top_p: float = 1.0
    # Optional: stop_sequences: List[str] = None

    @property
    def _llm_type(self) -> str:
        return "mlx-chat"

    def _resolve_model_path(self, target_model: str) -> str:
        """Resolve friendly model name to actual path"""
        if target_model in DECISION_MODELS:
             return DECISION_MODELS[target_model].get("model_path", target_model)
        elif target_model in COORDINATOR_MODELS:
             # Check for model_path first (local), then model_id (HuggingFace)
             return COORDINATOR_MODELS[target_model].get("model_path") or COORDINATOR_MODELS[target_model].get("model_id", target_model)
        elif target_model == "mlx-model":
            # Default fallback
            return "mlx-community/Mistral-7B-Instruct-v0.3-4bit"
        return target_model

    def _build_chatml_prompt(self, messages: List[BaseMessage]) -> str:
        """Construct ChatML formatted prompt from messages"""
        prompt = ""
        for msg in messages:
            if isinstance(msg, SystemMessage):
                prompt += f"<|im_start|>system\n{msg.content}<|im_end|>\n"
            elif isinstance(msg, HumanMessage):
                prompt += f"<|im_start|>user\n{msg.content}<|im_end|>\n"
            elif isinstance(msg, AIMessage):
                prompt += f"<|im_start|>assistant\n{msg.content}<|im_end|>\n"
            else:
                prompt += f"<|im_start|>user\n{msg.content}<|im_end|>\n"
        
        prompt += "<|im_start|>assistant\n"
        return prompt

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """
        Generate response using MLX Engine (Sync)
        """
        engine = get_mlx_engine()
        
        # Determine target model
        target_model_path = self._resolve_model_path(self.model_name)
            
        # Check if we need to load or switch models
        # Note: getattr handles case where current_model_path is missing on engine (safety)
        if not engine.is_loaded() or getattr(engine, 'current_model_path', None) != target_model_path:
            logger.info(f"Switching MLX model to {target_model_path}...")
            success = engine.load_model(target_model_path)
            if not success:
                 return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"Error: Failed to load MLX model {target_model_path}."))])

        # Construct prompt using ChatML standard
        prompt = self._build_chatml_prompt(messages)
        
        try:
            from mlx_lm.sample_utils import make_sampler
            import asyncio
            
            sampler = make_sampler(temp=self.temperature)
            
            # Run async generate in event loop
            response_text = asyncio.run(engine.generate(
                prompt=prompt,
                max_tokens=self.max_tokens,
                sampler=sampler
            ))
            
            message = AIMessage(content=response_text)
            return ChatResult(generations=[ChatGeneration(message=message)])
            
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
        """
        Async Generate response using MLX Engine
        """
        engine = get_mlx_engine()
        
        # Determine target model
        target_model_path = self._resolve_model_path(self.model_name)
        
        if not engine.is_loaded() or getattr(engine, 'current_model_path', None) != target_model_path:
            logger.info(f"Switching MLX model to {target_model_path}...")
            # Note: load_model is sync, so we can call it directly even in async method, 
            # though it might block the loop briefly content loading is IO bound.
            success = engine.load_model(target_model_path)
            if not success:
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"Error: Failed to load MLX model {target_model_path}."))])

        # Construct prompt using ChatML standard
        prompt = self._build_chatml_prompt(messages)

        try:
            from mlx_lm.sample_utils import make_sampler
            sampler = make_sampler(temp=self.temperature)

            response_text = await engine.generate(
                prompt=prompt,
                max_tokens=self.max_tokens,
                sampler=sampler, 
                # stop=stop # If engine supports it
            )
            
            message = AIMessage(content=response_text)
            return ChatResult(generations=[ChatGeneration(message=message)])
            
        except Exception as e:
            logger.error(f"MLX async generation failed: {e}")
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"Error generating response: {e}"))])

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        return {"model_name": self.model_name}

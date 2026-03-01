import logging
import json
import time
from typing import Dict, Any, List, Optional
from langchain_core.messages import SystemMessage, HumanMessage

from .base_agent import BaseAgent, AgentConfig, AgentResponse
from ..schemas import MathScriptRequest, MathScriptResponse
from ..mlx_langchain import ChatMLX

logger = logging.getLogger(__name__)

class MathGeneratorAgent(BaseAgent):
    """
    Specialized agent for generating NPU-optimized math scripts using MLX.
    Uses the local Granite-4.0-Tiny model for ultra-fast script generation.
    """

    def __init__(self, config: Optional[AgentConfig] = None):
        if config is None:
            config = AgentConfig(
                name="MathGeneratorAgent",
                enabled=True,
                timeout=30.0,
                cache_ttl=600
            )
        super().__init__(config)
        
        # Use a reliable model - fallback to LFM 2.5B if granite is missing, but config is fixed now.
        # Still, robust fallback is good practice.
        self.model_name = "granite-tiny"
        
        # Initialize ChatMLX with low temperature for code generation consistency
        self.llm = ChatMLX(
            model_name=self.model_name,
            temperature=0.1,
            max_tokens=2048
        )

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Processes a MathScriptRequest and returns a MathScriptResponse.
        
        Args:
            context: Input data dictionary matching MathScriptRequest schema
            
        Returns:
            AgentResponse containing the generated MathScriptResponse
        """
        try:
            # Validate input context using Pydantic model
            request = MathScriptRequest(**context)
            
            # Generate the script and explanation
            response_data = await self.generate_math_script(request)
            
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=response_data.model_dump(),
                latency_ms=0  # BaseAgent.execute will overwrite this
            )
        except Exception as e:
            self.logger.error(f"MathGeneratorAgent analysis failed: {e}", exc_info=True)
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error=str(e),
                latency_ms=0
            )

    async def generate_math_script(self, request: MathScriptRequest) -> MathScriptResponse:
        """
        Uses LLM to generate an MLX-optimized Python script and explanation.
        """
        system_prompt = (
            "You are an expert quantitative developer specializing in Apple Silicon NPU acceleration using MLX.\n"
            "Your goal is to generate high-performance Python scripts that leverage `mlx.core` for financial math.\n\n"
            "CRITICAL INSTRUCTIONS:\n"
            "1. Use provided MLX injection functions for heavy lifting. Do not reinvent low-level MLX code.\n"
            "2. Available injections include: `monte_carlo_sim(S0, mu, sigma, T, dt, num_sims)`, "
            "`black_scholes_tensor(S, K, T, r, sigma, option_type)`, `rsi_mlx(prices, period)`, `sma_mlx(prices, period)`.\n"
            "3. Always import `mlx.core as mx`.\n"
            "4. The output MUST be a valid JSON object with two fields:\n"
            "   - 'script': The Python code as a string. Ensure it uses the available injections and imports mx.\n"
            "   - 'explanation': A brief summary of the mathematical strategy used.\n"
            "5. The script will be executed in a sandbox where these injections are already available.\n"
            "6. Data passed in `context_data` will be available in the sandbox global scope.\n"
            "7. Return ONLY the JSON object, no other text."
        )
        
        # Prepare context data keys for the prompt
        context_keys = list(request.context_data.keys())
        
        user_prompt = (
            f"Query: {request.query}\n"
            f"Required Stats: {', '.join(request.required_stats)}\n"
            f"Context Data Keys Available: {', '.join(context_keys)}\n\n"
            "Generate the MLX script and explanation in JSON format."
        )
        
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt)
        ]
        
        try:
            # Generate response using ChatMLX (which handles its own internal async/sync bridging)
            # Note: ChatMLX inherited from BaseChatModel, we use ainvoke for async
            result = await self.llm.ainvoke(messages)
            content = result.content

            # Parse JSON response
            # Clean content in case of markdown wrapping
            cleaned_content = content.strip()
            if cleaned_content.startswith("```json"):
                cleaned_content = cleaned_content[7:]
            if cleaned_content.endswith("```"):
                cleaned_content = cleaned_content[:-3]
            cleaned_content = cleaned_content.strip()
            
            # Simple JSON extraction if model added prefix/suffix
            if "{" in cleaned_content and "}" in cleaned_content:
                start = cleaned_content.find("{")
                end = cleaned_content.rfind("}") + 1
                cleaned_content = cleaned_content[start:end]
                
            parsed = json.loads(cleaned_content)
            
            return MathScriptResponse(
                script=parsed.get("script", "# Error: No script generated"),
                explanation=parsed.get("explanation", "No explanation provided."),
                engine_requirement="npu"
            )
        except Exception as e:
            self.logger.error(f"Failed to parse MathGeneratorAgent output as JSON: {e}")

            # Use Fallback Agent/Model if granite fails
            if self.model_name == "granite-tiny":
                 self.logger.info("Retrying with fallback model (LFM)...")
                 self.llm.model_name = "native-mlx" # Assuming this maps to LFM in ChatMLX
                 try:
                     result = await self.llm.ainvoke(messages)
                     content = result.content
                     # ... parsing logic (simplified duplication for safety) ...
                     cleaned_content = content.strip()
                     if cleaned_content.startswith("```json"): cleaned_content = cleaned_content[7:]
                     if cleaned_content.endswith("```"): cleaned_content = cleaned_content[:-3]
                     cleaned_content = cleaned_content.strip()
                     start = cleaned_content.find("{")
                     end = cleaned_content.rfind("}") + 1
                     if start != -1 and end != -1:
                         cleaned_content = cleaned_content[start:end]

                     parsed = json.loads(cleaned_content)
                     return MathScriptResponse(
                        script=parsed.get("script", "# Error: No script generated"),
                        explanation=parsed.get("explanation", "No explanation provided."),
                        engine_requirement="npu"
                     )
                 except Exception as fallback_e:
                     self.logger.error(f"Fallback model failed: {fallback_e}")

            # Fallback behavior
            return MathScriptResponse(
                script=f"# Fallback: Failed to parse JSON\n# Original output:\n'''\n{content}\n'''",
                explanation="Error: The model output was not valid JSON.",
                engine_requirement="npu"
            )

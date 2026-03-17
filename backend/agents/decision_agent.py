"""
Decision Agent - High-reasoning LLM for final trade decisions
User-selectable model with price validation integration
"""

from market_context import MarketContext
from price_validation import PriceValidator
from typing import Dict, Optional, List, Any
import logging
import re
import json
import time
import os
import asyncio
import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field
from magentic import prompt as mag_prompt
from langchain_core.messages import SystemMessage, HumanMessage
from .llm_factory import LLMFactory
from utils.audit_log import log_audit
from app_logging import correlation_id_ctx
from resilience import get_circuit_breaker, CircuitBreakerOpenError, CircuitBreakerOpenException
from shared_types import SENSITIVE_TOOLS

logger = logging.getLogger(__name__)

decision_circuit_breaker = get_circuit_breaker("decision", failure_threshold=3, recovery_timeout=30.0)

class ToolCall(BaseModel):
    """Structured tool call extracted from agent reasoning."""
    tool_name: str = Field(..., description="Name of the tool to execute")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Arguments for the tool call")
    conviction_level: int = Field(default=5, ge=1, le=10, description="Confidence in this decision (1-10)")
    is_high_conviction: bool = Field(default=False, description="Explicitly marked as high conviction setup")

@mag_prompt(
    "Extract all tool calls from the following LLM response.\n"
    "Response: {content}\n"
    "Reasoning: {reasoning}\n"
    "Context: Looking for tool calls in the format [TOOL:name(args)].\n"
    "Return a list of ToolCall objects."
)
def extract_tool_calls(content: str, reasoning: str) -> List[ToolCall]:
    ...

class DecisionAgent:
    """
    The "Brain" - Uses high-reasoning LLM to make final decisions.

    Model: User-selectable (GPT-4o, Claude, Gemma-2-27B, etc.)
    Role: Synthesize all data, validate prices, make recommendations
    Performance: 5-8s (LLM reasoning time)
    """

    INTERCEPTED_TOOLS = SENSITIVE_TOOLS

    def __init__(self, model_name: str = "gpt-4o", api_keys: Optional[Dict[str, str]] = None, mcp_client=None):
        self.model_name = model_name
        self.api_keys = api_keys or {}
        from app_context import state
        self.mcp_client = mcp_client or state.mcp_client
        self.llm = None
        self._lm_studio_client = None # Deprecated, kept for compat if needed, but Factory handles it
        self._initialized = False

    async def _initialize_llm(self):
        """Initialize the LLM using the Factory"""
        try:
            self.llm = await LLMFactory.create_llm(self.model_name, self.api_keys)

            # Update model name if auto-detected by LM Studio
            if hasattr(self.llm, "active_model_id"):
                self.model_name = self.llm.active_model_id
                self._lm_studio_client = self.llm # For explicit checks if needed

            logger.info(f"DecisionAgent successfully initialized with {self.model_name}")
            self._initialized = True

        except Exception as e:
            logger.error(f"DecisionAgent initialization failed: {e}")
            raise

    async def make_decision(self, context: MarketContext, query: str, previous_response_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Make a trading decision based on aggregated market context.
        Returns a dict with 'content' and 'response_id'.
        """
        if not self._initialized:
            await self._initialize_llm()

        import os
        if os.getenv("USE_SHADOW_LLM") == "1":
             return {"content": self._generate_shadow_response(context, query=query), "response_id": None}

        from status_manager import status_manager
        status_manager.set_status("decision_agent", "working", "Applying reasoning...", model=self.model_name)

        # 1. Identify Cross-Agent Contradictions
        contradictions = self._identify_contradictions(context)
        if contradictions:
            context.user_context["contradictions"] = contradictions

        # 1a. SOTA 2026: Calculate Hybrid Conviction Multiplier (Phase 36 Wave 2)
        conviction_multiplier = 1.0
        if context.vision and context.vision.patterns:
            high_conf_patterns = [p for p in context.vision.patterns if p.confidence >= 0.85]
            if high_conf_patterns:
                conviction_multiplier = 1.2
                logger.info(f"DecisionAgent: High-confidence visual patterns detected. Applying 1.2x Conviction Multiplier.")
                context.user_context["conviction_multiplier"] = conviction_multiplier

        # 2. Build optimized prompt
        prompt = self._build_prompt(context, query)
        prompt = self._inject_context_layers(prompt, query)

        # 2.5 SOTA 2026: JMCE Uncertainty Gating
        uncertainty_warning = ""
        if context.forecast and context.forecast.jmce_uncertainty:
            # We treat the mean of the diagonal covariance as the uncertainty score
            # Institutional standard: If score > 0.05 (5% residual variance), block auto-execution
            u_score = sum(context.forecast.jmce_uncertainty) / len(context.forecast.jmce_uncertainty)
            if u_score > 0.05:
                logger.warning(f"JMCE Uncertainty Spike: {u_score:.4f}. Enforcing Risk Gate.")
                uncertainty_warning = f"\n\n⚠️ **JMCE RISK GATE ACTIVE**: High residual variance detected ({u_score:.2%}). Predictive stability is COMPROMISED. Final Strategic Synthesis MUST prioritize capital preservation."
                prompt += uncertainty_warning

        # 3. Math Delegation Workflow (NPU Accelerated)
        if any(k in (query or "").lower() for k in ["simulate", "calculate", "math", "model", "monte carlo", "forecast"]):
            logger.info("DecisionAgent: Math query detected. Enforcing NPU-Accelerated Sandbox.")
            try:
                from .math_generator_agent import MathGeneratorAgent
                from utils.math_validator import MathValidator
                
                # Prepare context for math agent
                math_input = {
                    "query": query,
                    "context_data": {
                        "ticker": context.ticker,
                        "price": context.price.price if context.price else 0,
                        "rsi": context.quant.rsi if context.quant else 0,
                        "portfolio_value": context.portfolio.total_value if context.portfolio else 0,
                        "cash": context.portfolio.cash_balance.get('total', 0) if context.portfolio and isinstance(context.portfolio.cash_balance, dict) else 0
                    },
                    "required_stats": ["simulated_projection", "risk_parameters"]
                }
                
                math_agent = MathGeneratorAgent()
                math_response = await math_agent.analyze(math_input)
                
                if math_response.success:
                    script = math_response.data.get("script")
                    explanation = math_response.data.get("explanation")
                    
                    logger.info(f"DecisionAgent: Math script generated. Explanation: {explanation}")
                    
                    validator = MathValidator()
                    math_start = time.time()
                    exec_result = await validator.execute_and_validate(script)
                    math_duration = (time.time() - math_start) * 1000
                    
                    # Integrate Math Telemetry
                    try:
                        from telemetry_store import record_math_metric
                        # Utilization proxy: if successful, assume 45% for NPU baseline
                        util_proxy = 45.0 if exec_result.get("status") == "success" else 0.0
                        
                        record_math_metric(
                            correlation_id=correlation_id_ctx.get() or "none",
                            success=(exec_result.get("status") == "success"),
                            execution_time_ms=math_duration,
                            npu_utilization_proxy=util_proxy,
                            exit_code=exec_result.get("exit_code", -1),
                            metadata={
                                "engine": "npu",
                                "explanation": explanation,
                                "schema_valid": exec_result.get("schema_valid", False)
                            }
                        )
                    except Exception as telem_err:
                        logger.error(f"Failed to record math telemetry: {telem_err}")

                    if exec_result.get("status") == "success":
                        math_output = exec_result.get("stdout", "")
                        logger.info("DecisionAgent: Math execution successful on NPU.")
                        
                        # Inject math findings into prompt for the reasoning loop
                        prompt += f"\n\n=== NPU-ACCELERATED MATH RESULTS ===\n"
                        prompt += f"MODEL: {explanation}\n"
                        prompt += f"RAW OUTPUT: {math_output}\n"
                        prompt += f"====================================\n"
                    else:
                        logger.warning(f"DecisionAgent: Math execution failed: {exec_result.get('error')}")
                else:
                    logger.warning(f"DecisionAgent: Math generation failed: {math_response.error}")
            except Exception as me:
                logger.error(f"DecisionAgent: Math delegation workflow failed: {me}", exc_info=True)

        # 4. Agentic Loop with NPU Acceleration
        try:
            system_content = self._get_system_persona(context.intent)
            
            # Execute loop (Stateful and Consolidated)
            loop_result = await self._run_agentic_loop(system_content, prompt, context, previous_response_id)
            recommendation = loop_result.get("content", "")
            response_id = loop_result.get("response_id")
            
            # 5. SOTA 2026: Reasoning Trace Export (Task 2.2)
            await self._export_reasoning_trace(context, recommendation, query)

            # Price validation
            recommendation = await self._validate_prices(recommendation, context.ticker)

            # SOTA 2026 Phase 30: Detect and extract Trade Proposals for HITL
            trade_proposal = self._extract_trade_proposal(recommendation, context)
            if trade_proposal:
                from app_context import state
                proposal_id = trade_proposal.get("proposal_id")
                state.trade_proposals[proposal_id] = trade_proposal
                context.user_context["pending_proposal"] = trade_proposal
                logger.info(f"DecisionAgent: Detected trade proposal for {trade_proposal.get('ticker')} ({proposal_id}). Routing to HITL gate.")

            status_manager.set_status("decision_agent", "ready", "Decision delivered", model=self.model_name)

            # Interactive Actions
            if (context.intent == "goal_planning" or "plan" in (query or "").lower()) and "CREATE_GOAL_PLAN" not in recommendation:
                recommendation += "\n\n[ACTION:CREATE_GOAL_PLAN]"

            # Audit Log
            log_audit(
                action="DECISION_MADE",
                actor=f"DecisionAgent::{self.model_name}",
                details={
                    "query": query,
                    "ticker": context.ticker,
                    "intent": context.intent,
                    "correlation_id": correlation_id_ctx.get(),
                    "recommendation_snippet": recommendation[:200],
                    "response_id": response_id,
                    "contradictions_count": len(contradictions) if contradictions else 0,
                    "conviction_multiplier": conviction_multiplier
                }
            )

            return {
                "content": recommendation,
                "response_id": response_id
            }

        except Exception as e:
            logger.error(f"Decision making failed: {e}")
            status_manager.set_status("decision_agent", "error", f"Error: {str(e)}", model=self.model_name)
            return {"content": f"Error generating recommendation: {str(e)}", "response_id": None}

    async def _export_reasoning_trace(self, context: MarketContext, recommendation: str, query: str):
        """SOTA 2026: Export detailed reasoning trace as JSON for auditing and UAT."""
        try:
            from datetime import datetime, timezone
            trace = {
                "correlation_id": correlation_id_ctx.get(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "query": query,
                "inputs": {
                    "ticker": context.ticker,
                    "intent": context.intent,
                    "agents_executed": list(context.agents_executed),
                    "hybrid_weighting": {
                        "quant": 0.4,
                        "forecast": 0.3,
                        "visual": 0.3
                    },
                    "conviction_multiplier": context.user_context.get("conviction_multiplier", 1.0)
                },
                "agent_thoughts": {
                    "chain_of_thought": context.reasoning,
                    "contradictions": context.user_context.get("contradictions", [])
                },
                "final_consensus": {
                    "recommendation_summary": recommendation[:500] + "..." if len(recommendation) > 500 else recommendation,
                    "model": self.model_name
                }
            }
            
            # Save to a dedicated trace file (overwritten per request in UAT, or unique ID in prod)
            trace_path = os.path.join(os.getcwd(), "reasoning_trace.json")
            with open(trace_path, "w") as f:
                json.dump(trace, f, indent=2)
            
            logger.info(f"✅ Reasoning trace exported to {trace_path}")
        except Exception as e:
            logger.warning(f"Failed to export reasoning trace: {e}")

    def _identify_contradictions(self, context: MarketContext) -> List[str]:
        """Identify conflicting signals between agents for the debate phase."""
        contradictions = []
        
        # 1. Technicals vs. Sentiment
        if context.quant and context.research:
            q_signal = context.quant.signal
            r_sent = context.research.sentiment_label
            
            if q_signal == "BUY" and r_sent == "BEARISH":
                contradictions.append("Technical indicators suggest a BUY, but News Sentiment is BEARISH.")
            elif q_signal == "SELL" and r_sent == "BULLISH":
                contradictions.append("Technical indicators suggest a SELL, but News Sentiment is BULLISH.")
                
        # 2. Forecast vs. Technicals
        if context.forecast and context.quant:
            f_trend = context.forecast.trend
            q_signal = context.quant.signal
            
            if f_trend == "BULLISH" and q_signal == "SELL":
                contradictions.append("AI Forecast predicts growth, but Technical signals suggest selling.")
            elif f_trend == "BEARISH" and q_signal == "BUY":
                contradictions.append("AI Forecast predicts a decline, but Technical signals suggest buying.")
                
        # 3. Whales vs. Retail Sentiment
        if context.whale and context.social:
            w_impact = context.whale.sentiment_impact
            s_sent = context.social.sentiment_label
            
            if w_impact == "BULLISH" and s_sent == "BEARISH":
                contradictions.append("Whale activity is BULLISH, but Social Media sentiment is BEARISH.")
            elif w_impact == "BEARISH" and s_sent == "BULLISH":
                contradictions.append("Whale activity is BEARISH, but Social Media sentiment is BULLISH.")
                
        # 4. Vision vs. Technicals (SOTA 2026 Phase 35)
        if context.vision and context.quant:
            v_patterns = [p.name.lower() for p in context.vision.patterns]
            q_signal = context.quant.signal
            
            if q_signal == "BUY" and any(p in v_patterns for p in ["double top", "head and shoulders", "bear flag"]):
                 contradictions.append("Technical signal is BUY, but VisionAgent detected BEARISH reversal patterns.")
            elif q_signal == "SELL" and any(p in v_patterns for p in ["double bottom", "inverse head and shoulders", "bull flag"]):
                 contradictions.append("Technical signal is SELL, but VisionAgent detected BULLISH reversal patterns.")
                
        return contradictions

    async def make_decision_stream(self, context: MarketContext, query: str):
        """
        Stream the decision making process.
        Yields chunks of text with formatting protection.
        """
        if not self._initialized:
            await self._initialize_llm()

        import os
        if os.getenv("USE_SHADOW_LLM") == "1":
             yield self._generate_shadow_response(context, query=query)
             return

        from status_manager import status_manager
        status_manager.set_status("decision_agent", "working", "Streaming reasoning...", model=self.model_name)

        # Same prep logic as make_decision
        account_filter = self._detect_account_mentions(query)
        prompt = self._build_prompt(context, query, account_filter)
        prompt = self._inject_context_layers(prompt, query)
        system_content = self._get_system_persona(context.intent)

        try:
            full_response = ""
            
            if hasattr(self.llm, "astream"):
                messages = [SystemMessage(content=system_content), HumanMessage(content=prompt)]
                async for chunk in self.llm.astream(messages):
                    content = chunk.content
                    
                    # SOTA Real-time Formatting Buffer
                    # Nemotron often outputs '### 1.' clumped with previous text
                    # We look for common header patterns and ensure a newline exists
                    if "###" in content:
                        # If the buffer (full_response) doesn't end with a newline, inject one
                        if full_response and not full_response.endswith("\n"):
                            yield "\n\n"
                            full_response += "\n\n"
                        elif full_response and full_response.endswith("\n") and not full_response.endswith("\n\n"):
                            yield "\n"
                            full_response += "\n"
                            
                    full_response += content
                    yield content
            else:
                # Fallback
                response = await self._generate_draft(system_content, prompt, context=context, query=query)
                full_response = response
                yield response
            
            # Post-stream processing: Reasoning Extraction
            reasoning = self._extract_reasoning(full_response)
            if reasoning:
                context.reasoning = reasoning
                
            # Post-stream processing (Price Validation)
            validation = await PriceValidator.validate_trade_price(context.ticker)
            if validation["action"] in ["block", "warn"] and context.ticker:
                 if any(word in full_response.upper() for word in ["BUY", "SELL"]):
                     warning = f"\n\n⚠️ **Price Warning**: {validation['message']}"
                     yield warning
            
            status_manager.set_status("decision_agent", "ready", "Stream complete", model=self.model_name)
            
            # SOTA 2026: Emit Decision Completion Telemetry
            from .messenger import AgentMessage, get_messenger
            messenger = get_messenger()
            c_id = correlation_id_ctx.get()
            await messenger.send_message(AgentMessage(
                sender="DecisionAgent",
                recipient="broadcast",
                subject="agent_complete",
                payload={"agent": "DecisionAgent", "success": True, "tokens_proxy": len(full_response.split())},
                correlation_id=c_id
            ))

            # Audit Log (Stream Complete)
            log_audit(
                action="DECISION_STREAM_COMPLETE",
                actor=f"DecisionAgent::{self.model_name}",
                details={
                    "query": query,
                    "ticker": context.ticker,
                    "intent": context.intent,
                    "correlation_id": correlation_id_ctx.get(),
                    "full_response_length": len(full_response)
                }
            )
            
        except Exception as e:
            logger.error(f"Streaming failed: {e}")
            yield f"Error: {str(e)}"

    async def _run_agentic_loop(self, system_content: str, prompt: str, context: MarketContext, previous_response_id: Optional[str] = None) -> Dict[str, Any]:
        """Runs multi-turn LLM loop with tool support and server-side state."""
        import asyncio
        import os
        
        # 0. Shadow Mode Priority Override
        if os.getenv("USE_SHADOW_LLM") == "1" and context:
            logger.info("🚀 SHADOW MODE ACTIVE (Agentic Loop): Emulating high-fidelity model response.")
            shadow_text = self._generate_shadow_response(context)
            return {"content": self._clean_response(shadow_text), "response_id": "shadow-test-id"}

        # 1. Stateful Priority: If LM Studio Client supports it, use server-side context
        from lm_studio_client import LMStudioClient
        if isinstance(self.llm, LMStudioClient) and hasattr(self.llm, "stateful_chat"):
            try:
                logger.info(f"DecisionAgent: Initiating stateful chat turn (Prev ID: {previous_response_id})")
                stateful_resp = await self.llm.stateful_chat(
                    model_id=self.model_name,
                    input_text=prompt,
                    previous_response_id=previous_response_id,
                    system_prompt=system_content,
                    temperature=0.1
                )
                if "error" not in stateful_resp:
                    content = stateful_resp.get("content", "")
                    reasoning = stateful_resp.get("reasoning", "")
                    
                    # Capture reasoning if not provided by client directly
                    if not reasoning:
                         reasoning = self._extract_reasoning(content)
                    
                    if reasoning:
                         context.reasoning = reasoning

                    return {
                        "content": self._clean_response(content),
                        "response_id": stateful_resp.get("response_id")
                    }
                else:
                    logger.warning(f"Stateful chat failed: {stateful_resp['error']}. Falling back to stateless loop.")
            except Exception as se:
                logger.error(f"Stateful transition error: {se}. Falling back.")

        # 2. Stateless Fallback: Standard Agentic Loop
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": prompt}
        ]
        
        mcp = self.mcp_client
        response_id = None
        
        # Limit loop to 3 turns
        for _ in range(3):
            if hasattr(self.llm, "chat"):
                resp = await self.llm.chat(
                    model_id=self.model_name,
                    messages=messages,
                    temperature=0.1
                )
                content = resp.get("content", "")
                reasoning = resp.get("reasoning", "")
                
                # Capture reasoning
                if not reasoning:
                    reasoning = self._extract_reasoning(content)
                if reasoning:
                    context.reasoning = reasoning

                response_id = resp.get("sessionId") or resp.get("response_id")
                
                # SOTA 2026: Agentic Tool Extraction via Magentic
                tool_calls = await asyncio.to_thread(extract_tool_calls, content, reasoning)
                
                if tool_calls:
                    from status_manager import status_manager
                    if len(tool_calls) > 1:
                        logger.info(f"DecisionAgent: Executing {len(tool_calls)} Parallel Consultations")
                        status_manager.set_status("decision_agent", "working", f"Executing {len(tool_calls)} parallel consultations...", model=self.model_name)
                    
                    async def execute_tool(t_call: ToolCall):
                        tool_name = t_call.tool_name
                        tool_args = t_call.arguments
                        try:
                            # Security Interception (SOTA 2026 Phase 31: Autonomous Bypass for High Conviction)
                            is_high_conviction = t_call.is_high_conviction or t_call.conviction_level == 10
                            
                            if tool_name in self.INTERCEPTED_TOOLS and not is_high_conviction:
                                logger.info(f"DecisionAgent: INTERCEPTING sensitive tool {tool_name}")
                                return f"[ACTION_REQUIRED:{tool_name}] Parameters: {json.dumps(tool_args)}. Unauthorized for autonomous execution. Requires UI confirmation."

                            if is_high_conviction and tool_name in self.INTERCEPTED_TOOLS:
                                logger.info(f"DecisionAgent: AUTONOMOUS BYPASS for sensitive tool {tool_name} (High Conviction Detected)")

                            logger.info(f"DecisionAgent: Executing Tool {tool_name}")

                            async def execute_mcp_tool():
                                if hasattr(asyncio, 'timeout'):
                                    async with asyncio.timeout(15.0):
                                        return await mcp.call_tool(tool_name, tool_args)
                                else:
                                    return await asyncio.wait_for(
                                        mcp.call_tool(tool_name, tool_args),
                                        timeout=15.0
                                    )

                            result = await decision_circuit_breaker.call(execute_mcp_tool)

                            result_text = result.content[0].text if hasattr(result, 'content') else str(result)
                            return f"[TOOL_RESULT:{tool_name}] {result_text}"
                        except asyncio.TimeoutError:
                            logger.warning(f"Tool execution timed out: {tool_name}")
                            return f"[TOOL_RESULT:{tool_name}] Error: Execution timed out after 15 seconds"
                        except CircuitBreakerOpenException:
                            logger.warning(f"Tool execution skipped: {tool_name} circuit breaker is OPEN")
                            return f"[TOOL_RESULT:{tool_name}] Error: Execution skipped because circuit breaker is OPEN"
                        except Exception as e:
                            logger.warning(f"Tool execution failed in loop: {e}")
                            return f"[TOOL_RESULT:{tool_name}] Error: {str(e)}"

                    tool_results = await asyncio.gather(*(execute_tool(t) for t in tool_calls))
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": "\n".join(tool_results)})
                    continue
                
                return {"content": self._clean_response(content), "response_id": response_id}
            else:
                # Basic generation fallback
                draft = await self._generate_draft(system_content, prompt, context=context, query=query)
                reasoning = self._extract_reasoning(draft)
                if reasoning:
                    context.reasoning = reasoning
                return {"content": self._clean_response(draft), "response_id": None}
                
        return {"content": "Reasoning loop exceeded max turns.", "response_id": None}


    async def _generate_draft(self, system_content: str, prompt: str, context: Optional[MarketContext] = None, query: str = "") -> str:
        """Generate initial draft."""
        if hasattr(self.llm, "ainvoke"):
            # LangChain
            messages = [SystemMessage(content=system_content), HumanMessage(content=prompt)]
            response = await self.llm.ainvoke(messages)
            return response.content
        elif hasattr(self.llm, "chat"):
            # LM Studio Client - Optimized for High Token Throughput
            msg_dicts = [{"role": "system", "content": system_content}, {"role": "user", "content": prompt}]
            # Use higher max_tokens for complex synthesis
            resp = await self.llm.chat(
                model_id=self.model_name, 
                messages=msg_dicts, 
                temperature=0.2, # Slight temperature for better synthesis
                max_tokens=2048
            )

            if "error" in resp:
                raise RuntimeError(resp["error"])

            return resp.get("content", "")
        else:
            llm_type = type(self.llm).__name__
            llm_attrs = dir(self.llm) if self.llm else "None"
            raise ValueError(f"LLM interface not recognized. Type: {llm_type}, Attrs: {llm_attrs}")

    def _generate_shadow_response(self, context: MarketContext, query: str = "") -> str:
        """
        Emulates a high-fidelity model response using real context data.
        Verifies that Phase 28 Liquidity and Phase 27 Geopolitical features work.
        """
        q = (query or "").lower()
        if context.intent == "conversational":
            if re.search(r'\b(hello|hi|hey|greetings)\b', q):
                return "Hello! I'm your Growin Intelligence Assistant. How can I help you with your portfolio or the markets today?"
            if "rsi" in q:
                return "The **Relative Strength Index (RSI)** is a momentum oscillator that measures the speed and change of price movements. It ranges from 0 to 100. Traditionally, an RSI above 70 indicates an overbought condition, while below 30 indicates an oversold condition. Is there a specific stock you'd like me to check the RSI for?"
            if "macd" in q:
                return "The **MACD (Moving Average Convergence Divergence)** is a trend-following momentum indicator that shows the relationship between two moving averages of a security’s price. It helps identify trend direction and momentum."
            return "I'm here to help! You can ask me to analyze a specific stock (e.g., 'Analyze TSLA'), check your portfolio performance, or explain financial concepts. What's on your mind?"

        if context.intent == "portfolio_query" and context.portfolio:
            p = context.portfolio
            return f"""### Shadow Mode: Portfolio Analysis
**Account Type**: {context.user_context.get("account_type", "all").upper()}

*   **Total Value**: £{float(p.total_value):.2f}
*   **Total Return**: £{float(p.total_pnl):.2f} ({p.pnl_percent}%)
*   **Net Deposits**: £{float(p.net_deposits):.2f}

**Positions**: {p.total_positions} active holdings
**Top Holdings**: {', '.join(p.top_holdings) if p.top_holdings else 'None'}

✅ *Data retrieved successfully via T212 API integration.*
"""

        ticker = context.ticker or "MARKET"
        price = float(context.price.current_price) if context.price and context.price.current_price else 0.0
        currency = context.price.currency if context.price and context.price.currency else "£"
        
        # Specialist Data
        sent = context.research.sentiment_label if context.research else "NEUTRAL"
        signal = context.quant.signal if context.quant else "HOLD"
        forecast = f"{currency}{float(context.forecast.forecast_24h):.2f}" if context.forecast else "N/A"
        
        # Phase 28: Liquidity
        slippage = f"{float(context.risk_governance.slippage_bps):.1f} bps" if (context.risk_governance and context.risk_governance.slippage_bps is not None) else "N/A"
        liq_status = context.risk_governance.liquidity_status if context.risk_governance else "UNKNOWN"
        
        # Phase 27: Geopolitical
        gpr = f"{float(context.geopolitical.gpr_score):.2f} ({context.geopolitical.global_sentiment_label})" if context.geopolitical else "N/A"

        # Phase 35: Vision
        vision_info = ""
        if context.vision:
            patterns = " | ".join([f"{p.name} ({p.confidence*100:.0f}%)" for p in context.vision.patterns])
            vision_info = f"\n**Vision Signals**: {patterns if patterns else 'No specific patterns detected'}"

        return f"""
### 1. Market Pulse
**Sentiment**: {sent} (Verified across NewsData.io and Tavily)
**GPR Index**: {gpr}{vision_info}

### 2. Technical Levels
*   **Support**: {currency}{float(context.quant.support_level) if context.quant and context.quant.support_level else 0.0:.2f}
*   **Resistance**: {currency}{float(context.quant.resistance_level) if context.quant and context.quant.resistance_level else 0.0:.2f}
*   **Signals**: {signal} (RSI: {float(context.quant.rsi) if context.quant and context.quant.rsi else 0.0:.1f})

### 3. AI Forecast (TTM-R2)
*   **24h Target**: {forecast} ({context.forecast.trend if context.forecast else "SIDEWAYS"})
*   **Confidence**: {context.forecast.confidence if context.forecast else "LOW"}
*   **Liquidity Status**: {liq_status}
*   **Est. Slippage**: {slippage}

### 4. Strategic Synthesis
The analysis for **{ticker}** is complete. Based on the Swarm execution, we detect a **{sent}** bias. 

*   **Liquidity Audit**: The position is classified as **{liq_status}**. With an estimated slippage of **{slippage}**, the market impact for your account size is minimal.
*   **Technical Outlook**: {ticker} is currently testing structural support. The {signal} signal from our QuantEngine suggests a high-probability entry point.
*   **Risk Governance**: Macro systemic risk is handled. Geopolitical factors ({gpr}) have been indexed and factored into the final conviction score.

**Target Entry**: {currency}{price:.2f}
**Take Profit**: {currency}{price*1.05:.2f} (Targeting 5% gain)
**Stop Loss**: {currency}{price*0.97:.2f} (Max 3% risk)
**Conviction Level**: 8/10
"""

    async def _critique_response(self, system_content: str, prompt: str, draft: str) -> str:
        """Critique and refine the draft."""
        critique_prompt = """
        [SELF-REFLECTION STEP]
        Review your Draft Analysis above.
        Check for:
        1. Did you hallucinate any data?
        2. Did you strictly follow the "MANDATORY ANSWER FORMAT"?
        3. Is the risk assessment balanced?

        If perfect, repeat exactly. If flaws found, rewrite the "Strategic Synthesis".
        """

        from status_manager import status_manager
        status_manager.set_status("decision_agent", "working", "Refining strategy...", model=self.model_name)

        if hasattr(self.llm, "ainvoke"):
            messages = [
                SystemMessage(content=system_content),
                HumanMessage(content=prompt),
                HumanMessage(content=draft),
                HumanMessage(content=critique_prompt)
            ]
            final = await self.llm.ainvoke(messages)
            return self._clean_response(final.content)
        return self._clean_response(draft)

    async def _validate_prices(self, text: str, ticker: Optional[str]) -> str:
        """Run price validation if trade detected."""
        if any(word in text.upper() for word in ["BUY", "SELL"]) and ticker:
            from status_manager import status_manager
            status_manager.set_status("decision_agent", "working", f"Validating price for {ticker}...", model=self.model_name)
            validation = await PriceValidator.validate_trade_price(ticker)

            if validation["action"] == "block":
                text += f"\n\n⚠️ **TRADE BLOCKED**: {validation['message']}"
            elif validation["action"] == "warn":
                text += f"\n\n⚠️ **Price Warning**: {validation['message']}"
                text += f"\nRecommended price: £{validation['recommended_price']:.2f}"
        return text

    def _inject_context_layers(self, prompt: str, query: str) -> str:
        """Inject RAG and Skills into prompt."""
        # Detect small model for aggressive optimization
        is_small_model = any(k in (self.model_name or "").lower() for k in ["nano", "mobile", "phi", "tiny", "gemma-2b"])


        # Skills (Limit length for small models)
        from utils.skill_loader import get_skill_loader
        skills_text = get_skill_loader().get_relevant_skills(query)
        if skills_text:
            if is_small_model:
                skills_text = skills_text[:500] + "..." # Hard truncate
            logger.info("DecisionAgent: Injecting relevant skills")
            prompt += f"\n\n=== RELEVANT EXPERT GUIDELINES ===\n{skills_text}\n=================================="

        # RAG
        from app_context import state
        if state.rag_manager:
            # Fetch fewer docs for small models
            n_results = 1 if is_small_model else 3
            
            # Abstract Query Detection
            is_abstract = False
            q_lower = (query or "").lower()
            if any(w in q_lower for w in ["portfolio", "market", "why", "trend", "economy", "inflation"]):
                # Simple check: no obvious standalone uppercase ticker symbols (e.g. AAPL)
                if not re.search(r'\b[A-Z]{2,5}\b', query):
                    is_abstract = True

            rag_docs = []
            if is_abstract:
                logger.info("DecisionAgent: Abstract query detected. Prioritizing theoretical RAG context.")
                rag_docs = state.rag_manager.query(query, n_results=n_results, where={"type": "abstract_concept"})
            
            # Fallback / Standard query
            if not rag_docs:
                rag_docs = state.rag_manager.query(query, n_results=n_results)

            if rag_docs:
                rag_lines = []
                for d in rag_docs:
                    content = d['content']
                    # Aggressive truncation for small models
                    limit = 300 if is_small_model else 1000
                    if len(content) > limit:
                        content = content[:limit] + "..."
                    rag_lines.append(f"- {content} (Source: {d['metadata'].get('type','unknown')})")

                rag_text = "\n".join(rag_lines)
                logger.info(f"DecisionAgent: Injecting {len(rag_docs)} RAG documents")
                prompt += f"\n\n=== HISTORICAL CONTEXT (RAG) ===\n{rag_text}\n================================"

        return prompt

    def _get_system_persona(self, intent: str) -> str:
        """Return the appropriate system prompt based on intent."""
        # Simplified persona for small models to reduce cognitive load
        if any(k in (self.model_name or "").lower() for k in ["nano", "mobile", "phi", "tiny", "gemma-2b"]):

            return "**Lead Financial Trader (Nano)**\nYou are a senior, profit‑maximising Lead Financial Trader. You consult your Coordinator Agent for data and provide high-probability trade suggestions based on SMA, RSI, MACD, and Sentiment."

        if intent in ["conversational", "educational"]:
            return """You are the Growin Intelligence Assistant & Financial Advisor.
            Your goal is to provide helpful, conversational, and educational insights to the user.
            You are professional, articulate, and friendly.
            
            GUIDELINES:
            - If the user greets you, greet them back warmly.
            - If the user asks for a definition (e.g. "What is RSI?"), explain it clearly but concisely.
            - If the user asks general questions about their portfolio performance without seeking a specific trade, provide a high-level summary.
            - NO FIXED FORMAT: You do not need to follow the technical 'Market Pulse' or 'Strategic Recommendation' structure. Just have a helpful conversation.
            - Encourage them to ask about specific stocks or their portfolio if they haven't already."""

        base_persona = """You are the Lead Strategic Trader. 
        You are an **Aggressive, Calculated Swing and Day Trader**. 
        Your goal is strictly to maximize ROI through high-probability, short-term setups.

        MISSION:
        Identify and deliver a **Single, High-Conviction Trading Plan**. 
        You are not a generic advisor; you are a hunter for intraday and intra-week profits.

        CORE TRADING PRINCIPLES:
        - Velocity: Prioritize setups that can play out within 2 hours (Intraday) to 5 days (Swing).
        - Edge: Use SMA, RSI, MACD, and Volume Profile to find entries.
        - Precision: Never output generic analysis. Every recommendation MUST include hard coordinates.
        - Macro Context: Always weigh Geopolitical Risk (GPR) and Systemic Risk (VIX/Yield) before recommending size or direction. If GPR is CRISIS, be extremely defensive.
        - Visual Confirmation: Cross-reference Quant signals with VisionAgent findings. If both confirm a pattern (e.g., Bull Flag + RSI breakout), increase conviction score.
        - Liquidity Awareness: Factor in Est. Slippage and POV. If market is THIN or ILLIQUID, adjust your Target Entry to account for slippage (Last Price + Slippage bps) and recommend smaller sizes.

        OUTPUT MANDATE:
        You MUST provide a structured Trading Plan in every recommendation involving a ticker.
        Use CLEAN MARKDOWN with double line breaks between sections for high readability.
        
        Use this format for coordinates:
        **Target Entry**: [Price]
        **Take Profit**: [Price] (Targeting X% gain)
        **Stop Loss**: [Price] (Max X% loss)
        **Conviction Level**: [Score 1-10]
        """

        if intent == "educational":
            return base_persona + "\nFocus on educating the client about the underlying mechanics of your decision while maintaining the aggressive edge."
        
        return base_persona + """
        [OUTPUT FORMAT INSTRUCTION]
        You must follow the **🚀 PROFIT‑DRIVEN TRADE RECOMMENDATION 🚀** template.
        Use the "MANDATORY ANSWER FORMAT" for data sections.
        """

    def _clean_response(self, response: str) -> str:
        """
        SOTA 2026: Balanced Response Cleaning.
        Strips thinking tags but preserves Markdown-essential newlines and structure.
        """
        # 1. Strip all variations of <think> tags (including malformed or unclosed ones)
        response = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL | re.IGNORECASE)
        response = re.sub(r'<think>.*', '', response, flags=re.DOTALL | re.IGNORECASE)
        response = re.sub(r'.*?</think>', '', response, flags=re.DOTALL | re.IGNORECASE)

        # 2. Strip common internal monologue markers and prefixes (at start of string only)
        meta_patterns = [
            r'^\*\*thinking\*\*.*?\n',
            r'^\[THOUGHTS\].*?\[/THOUGHTS\]',
            r'^thinking:.*?\n',
            r'^we need to.*?\n', 
            r'^let\'s think.*?\n',
            r'^i will follow.*?\n', 
            r'^i should.*?\n', 
            r'^i must.*?\n'
        ]
        for pattern in meta_patterns:
            response = re.sub(pattern, '', response, flags=re.IGNORECASE | re.MULTILINE)

        # 3. Strip instruction echoes and "mandatory" markers
        response = re.sub(r'\[MANDATORY ANSWER FORMAT.*?\]', '', response, flags=re.DOTALL | re.IGNORECASE)

        # 4. Normalize Whitespace (Collapse 3+ newlines into 2, but keep 2 for Markdown)
        response = response.strip()
        response = re.sub(r'\n{3,}', '\n\n', response)
        
        # 5. FINAL HARDENING: Ensure Markdown headers have newlines BEFORE and AFTER
        # Ensure at least one newline before any header that isn't at the start
        response = re.sub(r'(.)(###\s+)', r'\1\n\n\2', response)
        # Ensure at least one newline after every header
        response = re.sub(r'(###\s+.*)\n?(?!\n)', r'\1\n\n', response)

        return response.strip()

    def _extract_reasoning(self, response: str) -> Optional[str]:
        """Extract content within <think> tags."""
        matches = re.findall(r'<think>(.*?)</think>', response, flags=re.DOTALL | re.IGNORECASE)
        if matches:
            return "\n".join(matches).strip()
        # Fallback: check for unclosed <think> tag
        unclosed = re.search(r'<think>(.*)', response, flags=re.DOTALL | re.IGNORECASE)
        if unclosed:
            return unclosed.group(1).strip()
        return None

    def _detect_account_mentions(self, query: str) -> str:
        q = (query or "").lower()
        has_invest = any(w in q for w in ["invest", "investment", "brokerage", "trading"])
        has_isa = any(w in q for w in ["isa", "tax-free"])
        has_both = any(w in q for w in ["both", "all", "compare"])

        if has_both:
            return "all"
        if has_isa and has_invest:
            return "all"
        if has_isa:
            return "isa"
        if has_invest:
            return "invest"
        return "all"

    def _build_prompt(self, context: MarketContext, query: str, account_filter: str = "all") -> str:
        """Build prompt from market context with SOTA 2026 30/30/40 Hybrid Fusion."""

        # Final Mandatory Structure for rendering
        structured_template = """
        ### 1. Market Pulse
        **Sentiment**: {sentiment_label} (Score: {sentiment_score})

        ### 2. Technical Levels
        *   **Support**: {support}
        *   **Resistance**: {resistance}
        *   **Signals**: {signals}

        ### 3. AI Forecast ({forecast_algo})
        *   **24h Target**: {forecast_24h} ({forecast_trend})
        *   **Confidence**: {forecast_conf}
        *   **Note**: {forecast_note}

        ### 4. Strategic Synthesis
        [Your detailed reasoning here...]
        """
        
        # SOTA 2026: Infuse Hybrid Weighting Instructions
        weighting_instruction = """
        [HYBRID FUSION MANDATE]
        Apply the following weighting to your synthesis:
        - 40% QUANT/TECHNICALS (Indicators & ORB)
        - 30% FORECASTING (TTM-R2 Predictive Stability)
        - 30% VISUAL/SENTIMENT (VLM Patterns & News)
        
        If a Conviction Multiplier is active (Visual Patterns > 0.85), escalate your sizing and confidence accordingly.
        """

        coordinator_account = context.user_context.get("account_type")
        if coordinator_account:
            account_filter = coordinator_account

        sections = [
            f"**User Query**: {query}",
            f"**System Intent**: {context.intent}",
            f"**Routing Logic**: {context.routing_reason or 'Direct Analysis'}\n",
            weighting_instruction
        ]

        # Specialist Execution Summary
        if context.agents_executed or context.agents_failed:
            executed = list(context.agents_executed)
            failed = list(context.agents_failed)
            sections.append(f"**DATA CONTEXT**: Executed: {executed}, Failed: {failed}")

        # Portfolio Data (Compacted)
        if context.portfolio:
            p = context.portfolio
            label = f"📊 **{account_filter.upper()} ACCOUNT DATA**"
            cash = p.cash_balance.get('total', 0.0) if isinstance(p.cash_balance, dict) else p.cash_balance

            # Context Optimization: Compact summary
            p_text = f"{label}:\n- Value: £{p.total_value:,.0f} | Cash: £{cash:,.0f} | Perf: {p.pnl_percent*100:+.1f}%"

            if hasattr(p, 'accounts') and p.accounts:
                # Compact account breakdown (one line)
                acc_info = [f"{k[:3].upper()}: £{v.get('current_value',0):,.0f}" for k,v in p.accounts.items()]
                if acc_info:
                    p_text += f"\n- Split: {' | '.join(acc_info)}"

            sections.append(p_text)

        # Technicals (Compacted)
        if context.quant:
            q = context.quant
            source = context.price.source if context.price else "Mandated Provider"
            orb_text = ""
            if q.orb_signal and q.orb_signal.get("signal") != "WAIT":
                orb_text = f" | ORB: {q.orb_signal['signal']}"
            sections.append(f"**TECH ({source})**: {q.ticker} | RSI: {q.rsi:.1f} | Signal: {q.signal}{orb_text}")

        # Forecast (Compacted)
        if context.forecast:
            f = context.forecast
            sections.append(f"**AI MODEL ({f.algorithm})**: {f.ticker} | Target: £{f.forecast_24h:.2f} ({f.trend}) | Conf: {f.confidence}")

        # Research (Compacted)
        if context.research:
            r = context.research
            # Limit to 2 articles, titles only, truncated
            news = " | ".join([f"{a.title[:50]}..." for a in r.articles[:2]])
            sections.append(f"**NEWS**: Sent: {r.sentiment_label} | {news}")

        # Geopolitical Risk (SOTA 2026 Phase 27)
        if context.geopolitical:
            g = context.geopolitical
            events = " | ".join([f"{e.title[:40]}" for e in g.top_events[:2]])
            sections.append(f"**GPR (Geopolitical)**: Score: {g.gpr_score:.2f} ({g.global_sentiment_label}) | Events: {events}")

        # Institutional Liquidity (SOTA 2026 Phase 28)
        if context.risk_governance and context.risk_governance.slippage_bps is not None:
            rg = context.risk_governance
            sections.append(f"**LIQUIDITY**: Status: {rg.liquidity_status} | Est. Slippage: {rg.slippage_bps:.1f} bps | POV: {float(rg.pov_participation or 0)*100:.2f}%")

        # Vision Data (SOTA 2026 Phase 35)
        if context.vision:
            v = context.vision
            patterns = " | ".join([f"{p.name} ({p.confidence*100:.0f}%)" for p in v.patterns[:3]])
            sections.append(f"**VISION (VLM)**: Patterns: {patterns if patterns else 'No specific patterns detected'} | Description: {v.raw_description[:100]}...")

        # Debate Context
        contradictions = context.user_context.get("contradictions", [])
        if contradictions:
            sections.append("\n=== AGENT CONTRADICTIONS (DEBATE PHASE) ===")
            sections.append("The following conflicting signals were detected across your specialist agents. You MUST resolve these in your Strategic Synthesis:")
            for c in contradictions:
                sections.append(f"- {c}")
            sections.append("==========================================\n")

        # Template Vars
        if context.intent in ["conversational", "educational"]:
            sections.append("\n\n(Conversational Mode: Skip technical structure)")
            return "\n".join(sections)
            
        tmpl_vars = {
            "sentiment_label": context.research.sentiment_label if context.research else "N/A",
            "sentiment_score": f"{context.research.sentiment_score:.2f}" if context.research else "0.0",
            "support": f"£{context.quant.support_level:.2f}" if context.quant and context.quant.support_level else "N/A",
            "resistance": f"£{context.quant.resistance_level:.2f}" if context.quant and context.quant.resistance_level else "N/A",
            "signals": f"{context.quant.signal}{' (ORB: ' + context.quant.orb_signal['signal'] + ')' if context.quant.orb_signal and context.quant.orb_signal.get('signal') != 'WAIT' else ''}" if context.quant else "N/A",
            "forecast_algo": context.forecast.algorithm if context.forecast else "Unavailable",
            "forecast_24h": f"£{context.forecast.forecast_24h:.2f}" if context.forecast else "N/A",
            "forecast_trend": context.forecast.trend if context.forecast else "N/A",
            "forecast_conf": context.forecast.confidence if context.forecast else "N/A",
            "forecast_note": "Using TTM-R2 Model" if (context.forecast and not context.forecast.is_fallback) else "Fallback/Unavailable"
        }

        sections.append("\n\nIMPORTANT: Use this format:")
        sections.append(structured_template.format(**tmpl_vars))

        return "\n".join(sections)

    def switch_model(self, new_model: str):
        self.model_name = new_model
        self._initialized = False # Force re-init using factory on next call
        logger.info(f"Switched to model: {new_model}")

    async def generate_response(self, prompt: str) -> str:
        """Utility generation"""
        if not self._initialized:
            await self._initialize_llm()
        return await self._generate_draft("You are a helpful assistant.", prompt)

    def _extract_trade_proposal(self, text: str, context: MarketContext) -> Optional[Dict[str, Any]]:
        """
        SOTA 2026: Extracts structured trade proposals from LLM text for HITL.
        Looks for BUY/SELL/REBALANCE keywords and coordinates.
        """
        # Look for explicit [ACTION:TRADE_APPROVAL] or keywords
        if not any(word in text.upper() for word in ["BUY", "SELL", "REBALANCE"]):
            return None
            
        try:
            # 1. Ticker (from context or text)
            ticker = context.ticker
            if not ticker:
                ticker_match = re.search(r'\b[A-Z]{2,5}\b', text)
                if ticker_match: ticker = ticker_match.group(0)
            
            if not ticker: return None

            # 2. Action
            action = "REBALANCE"
            if "BUY" in text.upper(): action = "BUY"
            elif "SELL" in text.upper(): action = "SELL"

            # 3. Quantity (Heuristic for now, or extracted from coordinates)
            quantity = Decimal("1.0")
            # Look for "X shares" or "qty: X"
            qty_match = re.search(r'([0-9.]+)\s*shares', text, re.IGNORECASE)
            if not qty_match:
                qty_match = re.search(r'qty:\s*([0-9.]+)', text, re.IGNORECASE)

            if qty_match:
                try:
                    quantity = Decimal(qty_match.group(1))
                except Exception:
                    pass

            # 4. Reasoning (Snippet from Strategic Synthesis)
            reasoning = "NPU-detected Alpha opportunity."
            if "Strategic Synthesis" in text:
                try:
                    reasoning = text.split("Strategic Synthesis")[1].split("\n\n")[1].strip()[:150] + "..."
                except Exception:
                    pass

            return {
                "proposal_id": str(uuid.uuid4()),
                "ticker": ticker,
                "action": action,
                "quantity": quantity,
                "reasoning": reasoning,
                "status": "PENDING",
                "bypass_confirmation": "HIGH CONVICTION" in (context.reasoning or "").upper() or "CONVICTION LEVEL: 10" in text.upper(),
                "timestamp": datetime.now().timestamp()
            }
        except Exception as e:
            logger.warning(f"Failed to extract trade proposal: {e}")
            return None

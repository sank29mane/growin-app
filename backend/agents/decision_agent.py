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
from langchain_core.messages import SystemMessage, HumanMessage
from .llm_factory import LLMFactory
from utils.audit_log import log_audit
from app_logging import correlation_id_ctx

logger = logging.getLogger(__name__)


class DecisionAgent:
    """
    The "Brain" - Uses high-reasoning LLM to make final decisions.

    Model: User-selectable (GPT-4o, Claude, Gemma-2-27B, etc.)
    Role: Synthesize all data, validate prices, make recommendations
    Performance: 5-8s (LLM reasoning time)
    """

    INTERCEPTED_TOOLS = [
        "place_market_order",
        "place_limit_order",
        "place_stop_order",
        "place_stop_limit_order",
        "cancel_order",
        "create_investment_pie",
        "update_investment_pie",
        "delete_investment_pie"
    ]

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

        from status_manager import status_manager
        status_manager.set_status("decision_agent", "working", "Applying reasoning...", model=self.model_name)

        # 1. Identify Cross-Agent Contradictions
        contradictions = self._identify_contradictions(context)
        if contradictions:
            context.user_context["contradictions"] = contradictions

        # 2. Build optimized prompt
        prompt = self._build_prompt(context, query)
        prompt = self._inject_context_layers(prompt, query)

        # 3. Math Delegation Workflow (NPU Accelerated)
        if any(k in query.lower() for k in ["simulate", "calculate", "math", "model", "monte carlo", "forecast"]):
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
            
            # Price validation
            recommendation = await self._validate_prices(recommendation, context.ticker)

            status_manager.set_status("decision_agent", "ready", "Decision delivered", model=self.model_name)

            # Interactive Actions
            if (context.intent == "goal_planning" or "plan" in query.lower()) and "CREATE_GOAL_PLAN" not in recommendation:
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
                    "contradictions_count": len(contradictions) if contradictions else 0
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
                
        return contradictions

    async def make_decision_stream(self, context: MarketContext, query: str):
        """
        Stream the decision making process.
        Yields chunks of text.
        """
        if not self._initialized:
            await self._initialize_llm()

        from status_manager import status_manager
        status_manager.set_status("decision_agent", "working", "Streaming reasoning...", model=self.model_name)

        # SOTA 2026: Emit Decision Start Telemetry
        from .messenger import AgentMessage, get_messenger
        messenger = get_messenger()
        c_id = correlation_id_ctx.get()
        await messenger.send_message(AgentMessage(
            sender="DecisionAgent",
            recipient="broadcast",
            subject="agent_started",
            payload={"agent": "DecisionAgent", "model": self.model_name},
            correlation_id=c_id
        ))

        # Same prep logic as make_decision
        account_filter = self._detect_account_mentions(query)
        prompt = self._build_prompt(context, query, account_filter)
        prompt = self._inject_context_layers(prompt, query)
        system_content = self._get_system_persona(context.intent)

        try:
            # Streaming logic
            full_response = ""
            
            if hasattr(self.llm, "astream"):
                messages = [SystemMessage(content=system_content), HumanMessage(content=prompt)]
                async for chunk in self.llm.astream(messages):
                    content = chunk.content
                    full_response += content
                    yield content
            else:
                # Fallback
                response = await self._generate_draft(system_content, prompt)
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
                
                tool_matches = list(re.finditer(r'\[TOOL:(\w+)\((.*?)\)\]', content, re.DOTALL))
                
                if tool_matches:
                    from status_manager import status_manager
                    if len(tool_matches) > 1:
                        logger.info(f"DecisionAgent: Executing {len(tool_matches)} Parallel Consultations")
                        status_manager.set_status("decision_agent", "working", f"Executing {len(tool_matches)} parallel consultations...", model=self.model_name)
                    
                    async def execute_tool(match):
                        tool_name = match.group(1)
                        tool_args_str = match.group(2)
                        try:
                            tool_args = json.loads(tool_args_str) if tool_args_str.strip() else {}
                            
                            # Security Interception
                            if tool_name in self.INTERCEPTED_TOOLS:
                                logger.info(f"DecisionAgent: INTERCEPTING sensitive tool {tool_name}")
                                return f"[ACTION_REQUIRED:{tool_name}] Parameters: {tool_args_str}. Unauthorized for autonomous execution. Requires UI confirmation."

                            logger.info(f"DecisionAgent: Executing Tool {tool_name}")
                            result = await mcp.call_tool(tool_name, tool_args)
                            result_text = result.content[0].text if hasattr(result, 'content') else str(result)
                            return f"[TOOL_RESULT:{tool_name}] {result_text}"
                        except Exception as e:
                            logger.warning(f"Tool execution failed in loop: {e}")
                            return f"[TOOL_RESULT:{tool_name}] Error: {str(e)}"

                    tool_results = await asyncio.gather(*(execute_tool(m) for m in tool_matches))
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": "\n".join(tool_results)})
                    continue
                
                return {"content": self._clean_response(content), "response_id": response_id}
            else:
                # Basic generation fallback
                draft = await self._generate_draft(system_content, prompt)
                reasoning = self._extract_reasoning(draft)
                if reasoning:
                    context.reasoning = reasoning
                return {"content": self._clean_response(draft), "response_id": None}
                
        return {"content": "Reasoning loop exceeded max turns.", "response_id": None}


    async def _generate_draft(self, system_content: str, prompt: str) -> str:
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
        is_small_model = any(k in self.model_name.lower() for k in ["nano", "mobile", "phi", "tiny", "gemma-2b"])

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
            q_lower = query.lower()
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
        if any(k in self.model_name.lower() for k in ["nano", "mobile", "phi", "tiny", "gemma-2b"]):
            return "**Lead Financial Trader (Nano)**\nYou are a senior, profit‑maximising Lead Financial Trader. You consult your Coordinator Agent for data and provide high-probability trade suggestions based on SMA, RSI, MACD, and Sentiment."

        base_persona = """You are the Lead Financial Trader and the primary interface for the client. 
        You are a **senior, profit‑maximising financial advisor and discretionary trader**.

        MISSION:
        Identify, validate, and execute the **single highest‑probability, highest‑expected‑return trade(s)** while preserving capital through strict risk controls.

        CORE TRADING PRINCIPLES:
        - Technical Edge: Always compute SMA-50, SMA-200, RSI, MACD, and Bollinger Bands.
        - Risk-Reward: Target minimum 1:3 RR.
        - Position Sizing: Sugested Capital = min(FreeCash, 2% * PortfolioValue).

        DATA PROVENANCE:
        - Portfolio data: Trading 212 (Sole Source for holdings/invested amounts).
        - US Stocks: Alpaca (Primary) with yfinance fallback.
        - UK/LSE Stocks: Finnhub (Primary) with yfinance fallback.
        
        KNOWLEDGE MANDATE:
        - You possess deep general financial knowledge. Correlate specific T212 holdings with broad market trends (inflation, interest rates, sector rotation) to provide friendly, educational explanations.
        - KNOWLEDGE TIMELINE & MEMORY: You have semantic access to past chat history and a time-stamped news insights timeline. Use these to:
            * Recall past reasoning and advice given to the client.
            * Identify sentiment trends over the last 7 days for specific holdings.
            * Correlate historical news events with portfolio performance.
        
        TOOL: DOCKER SANDBOX (Code Execution)
        - For complex mathematical modeling, Monte Carlo simulations, or custom technical analysis, you can use the `docker_run_python` tool.
        - Syntax: [TOOL:docker_run_python({"script": "your_python_code", "engine": "standard"})]
        - Use `engine="npu"` for compute-intensive tasks on Apple Silicon.
        - The sandbox is isolated and has no network access. Output your results via `print()`.
        """

        if intent == "educational":
            return base_persona + "\nFocus on educating the client about the underlying mechanics of your decision."
        elif intent == "hybrid":
            return base_persona + "\nProvide a high-reasoning synthesis across multiple asset classes and regions."

        return base_persona + """
        [OUTPUT FORMAT INSTRUCTION]
        You must follow the **🚀 PROFIT‑DRIVEN TRADE RECOMMENDATION 🚀** template for trade ideas.
        Use the "MANDATORY ANSWER FORMAT" for data sections.

        CONVERSATIONAL STYLE:
        - Friendly, executive, and transparent.
        - NO internal monologue tags (<think>).
        """

    def _clean_response(self, response: str) -> str:
        """
        SOTA 2026: Ultra-Aggressive Response Cleaning.
        Strips thinking tags, internal monologues, and prompt instruction echoes.
        """
        # 1. Strip all variations of <think> tags (including malformed or unclosed ones)
        response = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL | re.IGNORECASE)
        response = re.sub(r'<think>.*', '', response, flags=re.DOTALL | re.IGNORECASE)
        response = re.sub(r'.*?</think>', '', response, flags=re.DOTALL | re.IGNORECASE)

        # 2. Strip common internal monologue markers and prefixes
        meta_patterns = [
            r'\*\*thinking\*\*.*?\n',
            r'\[THOUGHTS\].*?\[/THOUGHTS\]',
            r'^thinking:.*?\n',
            r'^we need to.*?\n', 
            r'^let\'s think.*?\n',
            r'^i will follow.*?\n', 
            r'^i should.*?\n', 
            r'^i must.*?\n',
            r'^based on.*?\n',
            r'^analyzing portfolio.*?\n',
            r'^style guidelines:.*?\n', 
            r'^as requested,.*?\n',
            r'^certainly!.*?\n',
            r'^here is.*?\n'
        ]
        for pattern in meta_patterns:
            response = re.sub(pattern, '', response, flags=re.IGNORECASE | re.MULTILINE)

        # 3. Strip instruction echoes and "mandatory" markers
        response = re.sub(r'Must include sections.*?\n', '', response, flags=re.IGNORECASE)
        response = re.sub(r'Align trade ideas.*?\n', '', response, flags=re.IGNORECASE)
        response = re.sub(r'\[MANDATORY ANSWER FORMAT.*?\]', '', response, flags=re.DOTALL | re.IGNORECASE)

        # 4. Collapse excessive whitespace and redundant newlines
        response = response.strip()
        response = re.sub(r'\n{3,}', '\n\n', response)
        
        # 5. Header Deduplication: Models sometimes echo headers they just wrote
        lines = response.split('\n')
        deduped_lines = []
        last_line = ""
        for line in lines:
            trimmed = line.strip()
            if trimmed and trimmed == last_line:
                continue
            deduped_lines.append(line)
            last_line = trimmed
        response = '\n'.join(deduped_lines)

        # 6. Final fallback: if the model echoed the entire template, try to find the actual content
        if "Strategic Synthesis" in response and response.count("Strategic Synthesis") > 1:
             # Take the last occurrence which is usually the actual answer
             parts = response.split("Strategic Synthesis")
             response = "4. **Strategic Synthesis" + parts[-1]

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
        q = query.lower()
        has_invest = any(w in q for w in ["invest", "investment", "brokerage", "trading"])
        has_isa = any(w in q for w in ["isa", "tax-free"])
        has_both = any(w in q for w in ["both", "all", "compare", "portfolio"])

        if has_both or (has_invest and has_isa):
            return "all"
        if has_isa:
            return "isa"
        if has_invest:
            return "invest"
        return "all"

    def _build_prompt(self, context: MarketContext, query: str, account_filter: str = "all") -> str:
        """Build prompt from market context"""

        # Template
        structured_template = """
        [MANDATORY ANSWER FORMAT - COPY EXACTLY]
        1. **Market Pulse**: {sentiment_label} (Score: {sentiment_score})
        2. **Technical Levels**:
           - Structural Support: {support}
           - Structural Resistance: {resistance}
           - Breakout Signals: {signals}
        3. **AI Forecast ({forecast_algo})**:
           - 24h Target: {forecast_24h} ({forecast_trend})
           - Confidence: {forecast_conf}
           - Note: {forecast_note}
        4. **Strategic Synthesis**: [Your detailed reasoning here...]
        """

        coordinator_account = context.user_context.get("account_type")
        if coordinator_account:
            account_filter = coordinator_account

        sections = [
            f"**User Query**: {query}",
            f"**System Intent**: {context.intent}",
            f"**Routing Logic**: {context.routing_reason or 'Direct Analysis'}\n"
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
            sections.append(f"**TECH ({source})**: {q.ticker} | RSI: {q.rsi:.1f} | Signal: {q.signal}")

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

        # Debate Context
        contradictions = context.user_context.get("contradictions", [])
        if contradictions:
            sections.append("\n=== AGENT CONTRADICTIONS (DEBATE PHASE) ===")
            sections.append("The following conflicting signals were detected across your specialist agents. You MUST resolve these in your Strategic Synthesis:")
            for c in contradictions:
                sections.append(f"- {c}")
            sections.append("==========================================\n")

        # Template Vars
        tmpl_vars = {
            "sentiment_label": context.research.sentiment_label if context.research else "N/A",
            "sentiment_score": f"{context.research.sentiment_score:.2f}" if context.research else "0.0",
            "support": f"£{context.quant.support_level:.2f}" if context.quant and context.quant.support_level else "N/A",
            "resistance": f"£{context.quant.resistance_level:.2f}" if context.quant and context.quant.resistance_level else "N/A",
            "signals": context.quant.signal if context.quant else "N/A",
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
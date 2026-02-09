"""
Decision Agent - High-reasoning LLM for final trade decisions
User-selectable model with price validation integration
"""

from market_context import MarketContext
from price_validation import PriceValidator
from typing import Dict, Optional
import logging
import re
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

    def __init__(self, model_name: str = "gpt-4o", api_keys: Optional[Dict[str, str]] = None):
        self.model_name = model_name
        self.api_keys = api_keys or {}
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

    async def make_decision(self, context: MarketContext, query: str) -> str:
        """
        Make a trading decision based on aggregated market context.
        """
        if not self._initialized:
            await self._initialize_llm()

        from status_manager import status_manager
        status_manager.set_status("decision_agent", "working", "Applying reasoning...", model=self.model_name)

        # Analyze query for account mentions
        account_filter = self._detect_account_mentions(query)

        # Build prompt
        prompt = self._build_prompt(context, query, account_filter)

        # Inject Skills & RAG
        prompt = self._inject_context_layers(prompt, query)

        # Invoke LLM
        try:
            system_content = self._get_system_persona(context.intent)

            # --- RECURSIVE THINKING LOOP ---
            draft_content = await self._generate_draft(system_content, prompt)

            # Critique & Refine (Only for cloud models to save local resources)
            is_local = "mlx" in self.model_name.lower() or hasattr(self.llm, "chat")

            if not is_local and context.intent in ["hybrid", "analytical"] and "gpt" in self.model_name:
                recommendation = await self._critique_response(system_content, prompt, draft_content)
            else:
                recommendation = self._clean_response(draft_content)

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
                    "recommendation_snippet": recommendation[:200]
                }
            )

            return recommendation

        except Exception as e:
            logger.error(f"Decision making failed: {e}")
            status_manager.set_status("decision_agent", "error", f"Error: {str(e)}", model=self.model_name)
            return f"Error generating recommendation: {str(e)}"

    async def make_decision_stream(self, context: MarketContext, query: str):
        """
        Stream the decision making process.
        Yields chunks of text.
        """
        if not self._initialized:
            await self._initialize_llm()

        from status_manager import status_manager
        status_manager.set_status("decision_agent", "working", "Streaming reasoning...", model=self.model_name)

        # Same prep logic as make_decision
        account_filter = self._detect_account_mentions(query)
        prompt = self._build_prompt(context, query, account_filter)
        prompt = self._inject_context_layers(prompt, query)
        system_content = self._get_system_persona(context.intent)

        try:
            # Check if validation logic needs to run *before* streaming?
            # Validation runs on the output. If we stream, we can't block it easily.
            # We will validate *after* collecting, or concurrently?
            # For streaming, we might skip strict price validation interception
            # OR we stream the raw LLM output and if it violates, we append a warning.
            
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
                
            # Post-stream processing (Price Validation)
            # We can yield an extra chunk if validation fails
            validation = await PriceValidator.validate_trade_price(context.ticker)
            if validation["action"] in ["block", "warn"] and context.ticker:
                 if any(word in full_response.upper() for word in ["BUY", "SELL"]):
                     warning = f"\n\n⚠️ **Price Warning**: {validation['message']}"
                     yield warning
            
            status_manager.set_status("decision_agent", "ready", "Stream complete", model=self.model_name)
            
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
        if any(k in self.model_name.lower() for k in ["nano", "mobile", "phi", "tiny"]):
            return "You are a financial advisor. Answer the user's query clearly and concisely using the provided context."

        if intent == "educational":
            return """You are an expert financial educator. Explain concepts (RSI, MACD) in simple terms. Focus on 'why' and 'how'."""
        elif intent == "hybrid":
            return """You are a senior financial analyst. Synthesize data from multiple sources (Quant, Portfolio, Research). Cite your sources."""

        return """You are a highly qualified Financial & Trading Advisor specializing in global markets (US, LSE, India).

        CORE PRINCIPLES:
        1. DEEP SYNTHESIS: Connect Portfolio data, Technicals, and Broader Insights.
        2. ACCURACY & DATA INTEGRITY:
           - Evaluate missing sources. If a source is "UNAVAILABLE", explicitly state it limits your decision.
           - Cite Forecast Algorithms (e.g. "IBM Granite TTM-R2" or "Statistical Fallback").
        3. GLOBAL CONTEXT: Prioritize LSE (GBP). Label US stocks clearly (USD).

        [OUTPUT FORMAT INSTRUCTION]
        You must COPY the "MANDATORY ANSWER FORMAT" exactly for the data sections.
        Your creative synthesis goes in "Strategic Synthesis".

        CONVERSATIONAL STYLE:
        - Professional, transparent.
        - NO INTERNAL MONOLOGUE tags (<think>).
        """

    def _clean_response(self, response: str) -> str:
        """Clean LLM response to remove thinking artifacts and meta-commentary."""
        response = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL | re.IGNORECASE)
        response = re.sub(r'<think>.*', '', response, flags=re.DOTALL | re.IGNORECASE)

        meta_patterns = [
            r'\*\*thinking\*\*.*?\n',
            r'^we need to', r'^let\'s', r'^based on my instructions',
            r'^i will follow', r'^i should', r'^i must',
            r'style guidelines:', r'as requested,'
        ]
        for pattern in meta_patterns:
            response = re.sub(pattern, '', response, flags=re.IGNORECASE | re.MULTILINE)

        return response.strip().replace('\n{3,}', '\n\n')

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
            sections.append(f"**TECH**: {q.ticker} | RSI: {q.rsi:.1f} | Signal: {q.signal}")

        # Forecast (Compacted)
        if context.forecast:
            f = context.forecast
            sections.append(f"**AI MODEL**: {f.ticker} | Target: £{f.forecast_24h:.2f} ({f.trend}) | Conf: {f.confidence}")

        # Research (Compacted)
        if context.research:
            r = context.research
            # Limit to 2 articles, titles only, truncated
            news = " | ".join([f"{a.title[:50]}..." for a in r.articles[:2]])
            sections.append(f"**NEWS**: Sent: {r.sentiment_label} | {news}")

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
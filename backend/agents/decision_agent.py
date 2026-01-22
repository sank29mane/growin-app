"""
Decision Agent - High-reasoning LLM for final trade decisions
User-selectable model with price validation integration
"""

from market_context import MarketContext
from price_validation import PriceValidator
from typing import Dict, Optional
import logging
import re

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
        self._initialize_llm()
    
    def _initialize_llm(self):
        """Initialize the LLM based on model name"""
        import os
        
        try:
            if "gpt" in self.model_name.lower():
                from langchain_openai import ChatOpenAI
                key = self.api_keys.get("openai") or os.getenv("OPENAI_API_KEY")
                if not key:
                    raise ValueError("OpenAI API Key required for GPT models")
                self.llm = ChatOpenAI(
                    model=self.model_name,
                    temperature=0,
                    openai_api_key=key
                )
            
            elif "claude" in self.model_name.lower():
                from langchain_anthropic import ChatAnthropic
                key = self.api_keys.get("anthropic") or os.getenv("ANTHROPIC_API_KEY")
                if not key:
                    raise ValueError("Anthropic API Key required for Claude models")
                self.llm = ChatAnthropic(
                    model=self.model_name,
                    anthropic_api_key=key
                )
            
            elif "gemini" in self.model_name.lower():
                from langchain_google_genai import ChatGoogleGenerativeAI
                key = self.api_keys.get("gemini") or os.getenv("GOOGLE_API_KEY")
                if not key:
                    raise ValueError("Google API Key required for Gemini models")
                self.llm = ChatGoogleGenerativeAI(
                    model=self.model_name,
                    google_api_key=key
                )
            
            elif "gemma" in self.model_name.lower() or "mistral" in self.model_name.lower():
                from langchain_ollama import ChatOllama
                self.llm = ChatOllama(
                    model=self.model_name,
                    base_url="http://127.0.0.1:11434"
                )
            
            elif "lmstudio" in self.model_name.lower() or "auto-detect" in self.model_name.lower():
                from langchain_openai import ChatOpenAI
                import httpx
                
                # Detect loaded model
                response = httpx.get("http://127.0.0.1:1234/v1/models", timeout=5.0)
                if response.status_code == 200:
                    models_data = response.json()
                    if models_data.get("data") and len(models_data["data"]) > 0:
                        # Filter out embedding models (SOTA: Ensures we don't try to use an embedding model as an LLM)
                        chat_models = [m for m in models_data["data"] if "embedding" not in m["id"].lower()]
                        
                        if chat_models:
                            loaded_model = chat_models[0]["id"]
                            logger.info(f"LM Studio: Using loaded chat model: {loaded_model}")
                            self.llm = ChatOpenAI(
                                model=loaded_model,
                                temperature=0,
                                openai_api_base="http://127.0.0.1:1234/v1",
                                openai_api_key="lm-studio"
                            )
                        else:
                            raise ValueError("No chat models loaded in LM Studio (only embedding models found)")
                    else:
                        raise ValueError("No model loaded in LM Studio")
                        
            elif "mlx" in self.model_name.lower():
                from mlx_langchain import ChatMLX
                logger.info(f"Initializing MLX Model: {self.model_name}")
                self.llm = ChatMLX(model_name=self.model_name)

            else:
                raise ValueError(f"Unsupported model: {self.model_name}")
            
            logger.info(f"DecisionAgent initialized with {self.model_name}")
            
        except Exception as e:
            logger.error(f"Failed to initialize LLM: {e}")
            # Fallback to a safe default if initialization fails, or re-raise
            if "mlx" in self.model_name.lower():
                 logger.warning("MLX initialization failed (model might not be loaded). Continuing...")
                 from mlx_langchain import ChatMLX
                 self.llm = ChatMLX(model_name=self.model_name)
            else:
                raise
    
    async def make_decision(self, context: MarketContext, query: str) -> str:
        """
        Make a trading decision based on aggregated market context.
        """
        from status_manager import status_manager
        status_manager.set_status("decision_agent", "working", "Applying reasoning...", model=self.model_name)
        
        # Analyze query for account mentions to determine what data to include
        account_filter = self._detect_account_mentions(query)

        # Build comprehensive prompt with account-aware context
        prompt = self._build_prompt(context, query, account_filter)
        
        # Invoke LLM
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            
            # Determine Persona based on Intent
            if context.intent == "educational":
                system_content = """You are an expert financial educator and mentor. 
                Your goal is to explain complex trading concepts (RSI, MACD, etc.) in simple, actionable terms.
                Focus on the 'why' and 'how' behind technical analysis.
                Be interactive: ask the user if they'd like a deeper dive or a practical example based on their current holdings."""
            elif context.intent == "hybrid":
                system_content = """You are a senior financial analyst and transparent trading advisor. 
                Synthesize data from multiple specialist sources (Quant, Portfolio, Research) into a cohesive strategy.
                Explicitly cite your sources (e.g., 'QuantAgent reports a bullish divergence...').
                Show your reasoning clearly so the user understands the logical path to your recommendation."""
            else:
                system_content = """You are a highly qualified Financial & Trading Advisor specializing in global markets (US, LSE, and India).
                
                CORE PRINCIPLES:
                1. DEEP SYNTHESIS: You must connect the dots between Portfolio data, Technicals, and Broader Market Insights. If NewsData.io indicates a business shift, analyze its impact on the technical trend.
                2. ACCURACY IS PARAMOUNT: Base all responses ONLY on the provided Market Context. 
                3. STRICT NO FABRICATION (RULE 3): If any specialist agent is listed as "MISSING (FAILED)" or "NO DATA", you MUST explicitly state that you do not have that specific data. NEVER invent numbers, sentiment, or outlooks for missing fields. Ensure your final advice is tempered by the lack of data.
                4. GLOBAL CONTEXT: When providing market outlooks, prioritize insights for US (S&P 500), UK (LSE/FTSE), and Indian (NSE/NIFTY) markets.
                5. PORTFOLIO ALIGNMENT & LISTING PREFERENCE: 
                   - The user's portfolio is mainly LSE-based. PRIORITIZE SUGGESTING LSE (London Stock Exchange) listings for trades. 
                   - You MAY suggest US stocks, but you MUST explicitly label them as "US Stock" or "Nasdaq/NYSE" and use USD ($).
                   - For US listings, ALWAYS show the price/value in USD ($) and provide an estimated conversion to GBP (£) in parentheses (e.g., "$150 (~£120)").
                   - For LSE stocks, ALWAYS use GBP (£). If quoting a price in pence (e.g. 150p), convert to pounds (£1.50) or clearly state "pence".
                6. TRADING EXPERTISE: Provide actionable insights for intraday, swing, and long-term horizons based on reality.
                7. NO HALLUCINATION: If a user asks about a stock outside the provided context, offer to research it rather than guessing.
                
                CONVERSATIONAL STYLE & FORMATTING:
                - Use a friendly, professional, and lively tone.
                - NO THINKING OUT LOUD: Never include tags like <think>, **Thinking**, or phrases like "We need to...", "Let's analyze...".
                - RESPONSE STRUCTURE: 
                  - Start with a direct, conversational answer.
                  - If data is missing (see "MISSING/FAILED DATA SOURCES"), acknowledge it clearly in the first paragraph.
                  - Use bolding for key figures and tickers.
                  - Use bullet points or tables ONLY for complex data comparisons.
                  - Keep it concise but insightful.
                
                STRICT RULE: NO INTERNAL MONOLOGUE. NO META-COMMENTARY. JUST THE ANSWER. EXACTLY FOLLOW RULE 3.
"""
            
            system_message = SystemMessage(content=system_content)
            human_message = HumanMessage(content=prompt)
            
            response = await self.llm.ainvoke([system_message, human_message])
            
            # Clean up the response to remove thinking artifacts
            recommendation = self._clean_response(response.content)
            
            # Price validation if trade recommended
            if any(word in recommendation.upper() for word in ["BUY", "SELL"]):
                ticker = context.ticker
                if ticker:
                    status_manager.set_status("decision_agent", "working", f"Validating price for {ticker}...", model=self.model_name)
                    validation = await PriceValidator.validate_trade_price(ticker)
                    
                    if validation["action"] == "block":
                        recommendation += f"\n\n⚠️ **TRADE BLOCKED**: {validation['message']}"
                    elif validation["action"] == "warn":
                        recommendation += f"\n\n⚠️ **Price Warning**: {validation['message']}"
                        recommendation += f"\nRecommended price: £{validation['recommended_price']:.2f}"
            
            status_manager.set_status("decision_agent", "ready", "Decision delivered", model=self.model_name)
            return recommendation
            
        except Exception as e:
            logger.error(f"Decision making failed: {e}")
            status_manager.set_status("decision_agent", "error", f"Error: {str(e)}", model=self.model_name)
            return f"Error generating recommendation: {str(e)}"

    def _clean_response(self, response: str) -> str:
        """Clean LLM response to remove thinking artifacts and meta-commentary."""
        
        # Remove <think>...</think> blocks and partial tags
        response = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL | re.IGNORECASE)
        response = re.sub(r'<think>.*', '', response, flags=re.DOTALL | re.IGNORECASE)
        response = re.sub(r'.*?</think>', '', response, flags=re.DOTALL | re.IGNORECASE)
        
        # Remove common thinking artifacts and meta-phrases
        meta_patterns = [
            r'\*\*thinking\*\*.*?\n',
            r'^we need to',
            r'^let\'s',
            r'^based on my instructions',
            r'^i will follow',
            r'^i should',
            r'^i must',
            r'style guidelines:',
            r'as requested,'
        ]
        
        for pattern in meta_patterns:
            response = re.sub(pattern, '', response, flags=re.IGNORECASE | re.MULTILINE)
        
        # Clean up double newlines and leading/trailing whitespace
        response = re.sub(r'\n{3,}', '\n\n', response)
        response = response.strip()
        
        # Quick actions are now handled by the UI - no markdown needed
        
        return response

    def _detect_account_mentions(self, query: str) -> str:
        """
        Analyze user query to detect which account(s) they want information about.

        Returns:
            "invest" - invest account only
            "isa" - ISA account only
            "all" - both accounts
        """
        query_lower = query.lower()

        # Check for explicit account mentions
        has_invest = any(word in query_lower for word in ["invest", "investment", "brokerage", "trading"])
        has_isa = any(word in query_lower for word in ["isa", "tax-free", "isa account"])

        # Check for general/both mentions
        has_both = any(word in query_lower for word in ["both", "all", "compare", "comparison", "total", "portfolio"])

        if has_both or (has_invest and has_isa):
            return "all"
        elif has_isa:
            return "isa"
        elif has_invest:
            return "invest"
        else:
            # Default to all accounts for general queries
            return "all"

    def _build_prompt(self, context: MarketContext, query: str, account_filter: str = "all") -> str:
        """Build comprehensive prompt from market context"""
        
        # PRIORITY: Use coordinator's detected account_type from context if available
        coordinator_account = context.user_context.get("account_type")
        if coordinator_account:
            account_filter = coordinator_account
        
        sections = [
            f"**User Query**: {query}",
            f"**System Intent**: {context.intent}",
            f"**Routing Logic**: {context.routing_reason or 'Direct Analysis'}\n"
        ]
        
        # Specialist Execution Summary & Data Gaps
        if context.agents_executed or context.agents_failed:
            executed = [a for a in context.agents_executed]
            failed = [a for a in context.agents_failed]
            
            # Detect missing but potentially relevant agents based on intent
            # Detect missing but potentially relevant agents based on intent
            # e.g. if analytical, we expect quant/forecast/research
            expected = []
            has_ticker = context.ticker and context.ticker != "MARKET"
            
            if context.intent == "analytical": 
                expected = ["ResearchAgent"]
                if has_ticker: expected.extend(["QuantAgent", "ForecastingAgent"])
            elif context.intent == "market_analysis": 
                expected = ["ResearchAgent", "SocialAgent"]
                if has_ticker: expected.extend(["QuantAgent", "ForecastingAgent"])
            
            missing = [a for a in expected if a not in executed and a not in failed]
            
            summary = []
            if executed: summary.append(f"AVAILABLE SOURCES: {', '.join(executed)}")
            if failed: summary.append(f"⚠️ MISSING (FAILED) SOURCES: {', '.join(failed)}")
            if missing: summary.append(f"❓ UNAVAILABLE SOURCES (NO DATA): {', '.join(missing)}")
            
            sections.append(f"**DATA CONTEXT INTEGRITY SCAN**:\n{chr(10).join(['- ' + s for s in summary])}")
        
        # Portfolio data with clear account labeling
        if context.portfolio:
            p = context.portfolio
            cash_total = p.cash_balance.get('total', 0.0) if isinstance(p.cash_balance, dict) else p.cash_balance

            # Build portfolio overview with explicit account label
            if account_filter == "isa":
                label = "📊 **ISA ACCOUNT DATA**"
            elif account_filter == "invest":
                label = "📊 **INVEST ACCOUNT DATA**"
            else:
                label = "📊 **COMBINED PORTFOLIO DATA**"
            
            portfolio_text = f"{label}:\n"
            portfolio_text += f"- Total Value: £{p.total_value:,.2f}\n"
            portfolio_text += f"- Cash Balance: £{cash_total:,.2f}\n"
            portfolio_text += f"- Positions: {len(p.positions)} active\n"
            portfolio_text += f"- Performance: {p.pnl_percent*100:+.2f}%\n"

            # Add account-specific details if available
            if hasattr(p, 'accounts') and p.accounts:
                accounts_info = []
                for acc_id, acc_data in p.accounts.items():
                    val = acc_data.get('current_value', 0)
                    pnl = acc_data.get('total_pnl_percent', 0)
                    accounts_info.append(f"{acc_id.upper()}: £{val:,.0f} ({pnl*100:+.1f}%)")
                
                if accounts_info:
                    portfolio_text += f"- Account Breakdown: {' | '.join(accounts_info)}\n"

            # Add History Context
            if p.portfolio_history and len(p.portfolio_history) > 1:
                start_val = p.portfolio_history[0].get('total_value', 0)
                end_val = p.portfolio_history[-1].get('total_value', 0)
                if start_val > 0:
                    hist_change = ((end_val - start_val) / start_val) * 100
                    portfolio_text += f"- 30D Performance: {hist_change:+.2f}% (from £{start_val:,.0f} to £{end_val:,.0f})\n"

            sections.append(portfolio_text)
        
        # Technical indicators
        if context.quant:
            q = context.quant
            sections.append(f"""
**TECHNICAL ANALYSIS** ({q.ticker}):
- RSI: {f'{q.rsi:.1f}' if q.rsi is not None else 'N/A'}
- Signal: {q.signal}
- Support/Resistance: £{f'{q.support_level:.2f}' if q.support_level else 'N/A'} / £{f'{q.resistance_level:.2f}' if q.resistance_level else 'N/A'}
""")
        
        # Forecast
        if context.forecast:
            f = context.forecast
            sections.append(f"""
**AI PROJECTIONS** ({f.ticker}):
- 24h Prediction: £{f.forecast_24h:.2f}
- Confidence: {f.confidence}
- Predicted Trend: {f.trend}
""")
        
        # Research/Sentiment (Deep Integration)
        if context.research:
            r = context.research
            news_items = []
            for art in r.articles[:4]:  # Show top 4 articles for context
                sentiment_icon = "🟢" if art.sentiment and art.sentiment > 0.1 else "🔴" if art.sentiment and art.sentiment < -0.1 else "⚪"
                item = f"{sentiment_icon} **{art.title}** ({art.source})\n   > {art.description}"
                news_items.append(item)
            
            news_text = "\n".join(news_items)
            sections.append(f"""
**BROADER MARKET INSIGHTS & SENTIMENT**:
- Overall Sentiment: {r.sentiment_label} (Confidence Score: {r.sentiment_score:.2f})
- Key News & Business Factors:
{news_text}
""")

        # Goal Planner Data
        if context.goal:
            g = context.goal
            impl = g.implementation or {}
            
            # Helper to format weights
            allocations = "\n".join([f"   - {inst['ticker']}: {inst['weight']*100:.1f}% ({inst['category']})" for inst in g.suggested_instruments])
            
            execution_text = "This strategy is optimized for a Trading 212 Pie."
            if impl and impl.get("action") == "CREATE_OR_UPDATE":
                 execution_text = f"**Execution**: Create '{impl.get('name')}' Pie to automate this strategy."

            sections.append(f"""
**🎯 GOAL INVESTMENT PLAN**:
- **Strategy**: {g.risk_profile} Risk Profile
- **Target Return**: {g.target_returns_percent:.1f}% Annually
- **Expected Return**: {g.expected_annual_return*100:.1f}% (Volatility: {g.expected_volatility*100:.1f}%)
- **Feasibility**: {g.probability_of_success*100:.0f}% chance of hitting target
- **Recommended Allocation**:
{allocations}
- {execution_text}
""")
        
        # Metadata
        sections.append(f"\n*Analysis completed in {context.total_latency_ms:.0f}ms*")
        
        return "\n".join(sections)

    def switch_model(self, new_model: str):
        """Switch to a different model"""
        self.model_name = new_model
        self._initialize_llm()
        logger.info(f"Switched to model: {new_model}")

    async def generate_response(self, prompt: str) -> str:
        """Generate a generic response for utility tasks (e.g. titles)"""
        from status_manager import status_manager
        status_manager.set_status("decision_agent", "working", "Generating utility text...", model=self.model_name)
        try:
            # Handle list of messages or single string
            if hasattr(self.llm, "ainvoke"):
                response = await self.llm.ainvoke(prompt)
                # Handle different response types (str vs AIMessage)
                content = ""
                if hasattr(response, 'content'):
                    content = response.content
                else:
                    content = str(response)
            else:
                # Fallback for models that don't support ainvoke or simple string
                content = str(await self.llm(prompt))
            
            status_manager.set_status("decision_agent", "ready", "Idle", model=self.model_name)
            return content
        except Exception as e:
            logger.error(f"Generation failed: {e}")
            status_manager.set_status("decision_agent", "error", f"Error: {str(e)}", model=self.model_name)
            return "Untitled Conversation"

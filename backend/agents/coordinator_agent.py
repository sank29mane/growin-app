"""
Coordinator Agent - Orchestrates all specialist agents
Static SOTA model that routes queries and aggregates results
"""

from .base_agent import BaseAgent, AgentResponse
from market_context import MarketContext
from . import QuantAgent, PortfolioAgent, ForecastingAgent, ResearchAgent, SocialAgent, WhaleAgent, GoalPlannerAgent
from typing import Dict, Any, List, Optional
import asyncio
import logging
import re
import json
import difflib
from utils import run_safe_python

logger = logging.getLogger(__name__)

COORDINATOR_SYSTEM_PROMPT = """
You are the Coordinator Agent. Your job is to classify user queries and route them to specialist agents.
Analyze the user's request and return a JSON object with the intent and required agents.

Specialist Agents:
- QuantAgent: Technical analysis, RSI, MACD, Support/Resistance ("quant")
- ForecastingAgent: AI price predictions ("forecast")
- ResearchAgent: News, sentiment, company info ("research")
- SocialAgent: Social media sentiment ("social" - often paired with research)
- PortfolioAgent: Portfolio data, holdings, P&L ("portfolio")
- WhaleAgent: Large holder activity ("whale")
- GoalPlannerAgent: Create/plan investment goals ("goal_planner")

Intents:
1. "price_check": Simple price lookup (needs "quant" for simple data)
2. "market_analysis": Deep dive (needs "quant", "forecast", "research", "social")
3. "portfolio_query": User asking about their holdings (needs "portfolio")
4. "forecast_request": Specific prediction request (needs "forecast", "quant")
5. "educational": General questions not requiring live data (needs [])
6. "goal_planning": Creating a new investment plan (needs "goal_planner")

Goal Planning Extraction:
If intent is "goal_planning", extract:
- "capital": Amount to invest (default 1000)
- "risk": Risk profile (LOW, MEDIUM, HIGH, AGGRESSIVE_PLUS, GROWTH)
- "years": Duration in years (default 5)

Output Format (JSON ONLY):
{
  "type": "exact_intent_string",
  "needs": ["list", "of", "agents"],
  "reason": "Brief explanation",
  "params": { ...extracted params... }
}
"""

class CoordinatorAgent:
    """
    Coordinator Agent orchestrates specialist agents in parallel.
    
    Model: Static SOTA (Mistral-7B or Gemma-2-9B)
    Role: Parse query, route to specialists, aggregate results
    Performance: 2-3s for routing + specialist execution
    """
    
    def __init__(self, mcp_client, model_name: str = "granite-tiny"):
        self.model_name = model_name
        self.mcp_client = mcp_client
        
        # Initialize specialist agents
        self.quant_agent = QuantAgent()
        self.portfolio_agent = PortfolioAgent()
        self.forecasting_agent = ForecastingAgent()
        self.research_agent = ResearchAgent()
        self.social_agent = SocialAgent()
        self.whale_agent = WhaleAgent()
        self.goal_planner_agent = GoalPlannerAgent()
        
        self.llm = None
        self._initialize_llm()
        
        # Initialize Safe Python Executor for data cleaning/fixes
        from utils import get_safe_executor
        self.python_executor = get_safe_executor()
        
        if "mlx" in self.model_name.lower():
            logger.info(f"Initializing MLX Model: {self.model_name}")
        
        logger.info(f"CoordinatorAgent initialized with model: {model_name}")
    
    def _initialize_llm(self):
        """Initialize the routing LLM"""
        import os
        try:
            # Using simple routing model (Mistral/Ollama) for speed
            if "gpt" in self.model_name.lower():
                from langchain_openai import ChatOpenAI
                self.llm = ChatOpenAI(model=self.model_name, temperature=0)
            elif "mlx" in self.model_name.lower() or "granite" in self.model_name.lower():
                from mlx_langchain import ChatMLX
                # GUARDRAIL: Enforce deterministic output
                self.llm = ChatMLX(model_name=self.model_name, temperature=0, top_p=1.0)
            else:
                from langchain_ollama import ChatOllama
                self.llm = ChatOllama(model=self.model_name, base_url="http://127.0.0.1:11434")
        except Exception as e:
            logger.warning(f"Failed to initialize Coordinator LLM: {e}. Routing will be static.")
    
    async def _classify_intent(self, query: str) -> Dict[str, Any]:
        """Classify user intent to determine routing path"""
        if not self.llm:
            return {"type": "analytical", "needs": ["portfolio", "quant", "forecast"]}
            
        # Input guardrail: Basic sanitization
        clean_query = query.strip()[:500] # Limit length to prevent context overflow attacks

        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            
            # GUARDRAIL: Use System Prompt for strict instruction
            messages = [
                SystemMessage(content=COORDINATOR_SYSTEM_PROMPT),
                HumanMessage(content=f'QUERY: "{clean_query}"')
            ]
            
            response = await self.llm.ainvoke(messages)
            import json
            
            # Extract JSON from response
            content = response.content if hasattr(response, 'content') else str(response)
            
            # GUARDRAIL: JSON extraction with regex fallback
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                return json.loads(match.group())
            else:
                # Last resort: Try parsing raw string if it looks like JSON
                return json.loads(content)
                
        except json.JSONDecodeError:
            logger.error(f"Intent classification failed to parse JSON: {content[:100]}...")
        except Exception as e:
            logger.error(f"Intent classification failed: {e}")
            
        # Default safety fallback
        return {"type": "analytical", "needs": ["portfolio", "quant", "forecast"]}

    async def process_query(self, query: str, ticker: Optional[str] = None, account_type: Optional[str] = None, history: List[Dict] = []) -> MarketContext:
        """
        Process user query and coordinate specialist agents based on intent.
        
        Args:
            query: User's question
            ticker: Optional stock ticker
            account_type: Optional account filter ("invest", "isa", "all")
            history: Conversation history for context resolution
        """
        from status_manager import status_manager
        
        # 0. Resolve Context (Ticker) from History if missing
        if not ticker and history:
            ticker = self._resolve_ticker_from_history(history)
            if ticker:
                logger.info(f"Resolved ticker from history: {ticker}")

        status_manager.set_status("coordinator", "online", f"Analyzing: '{query}'", model=self.model_name)
        
        # 1. Classify Intent
        intent = await self._classify_intent(query)
        logger.info(f"Coordinator Intent: {intent['type']} - Needs: {intent.get('needs', [])}")
        
        # Create context
        context = MarketContext(
            query=query, 
            ticker=ticker, 
            intent=intent.get("type", "analytical"),
            routing_reason=intent.get("reason", "")
        )
        
        # Store account context for agents
        # 1b. Enhance account detection - if None, try to detect from query
        detected_account = account_type
        if not detected_account:
            query_lower = query.lower()
            if "isa" in query_lower:
                detected_account = "isa"
                logger.info("Auto-detected ISA account from query text")
            elif any(w in query_lower for w in ["invest", "investment"]):
                detected_account = "invest"
                logger.info("Auto-detected Invest account from query text")
        
        context.user_context["account_type"] = detected_account
        account_type = detected_account # Update for use in task preparation below
        
        # 2. Determine which specialists to invoke
        needs_quant = "quant" in intent.get("needs", [])
        needs_portfolio = "portfolio" in intent.get("needs", [])
        needs_forecast = "forecast" in intent.get("needs", [])
        needs_research = "research" in intent.get("needs", [])
        needs_social = "social" in intent.get("needs", []) or needs_research
        needs_whale = "whale" in intent.get("needs", []) or needs_forecast # Implicitly check whales if forecasting
        needs_goal = "goal_planner" in intent.get("needs", [])
        
        # Early exit for purely conversational queries if no specialists are needed
        if not intent.get("needs") and intent["type"] == "educational":
             status_manager.set_status("coordinator", "ready", "Educational query - Passing to Decision Core")
             return context

        # COORDINATOR FIX: Robust normalization via T212 rules (fast & deterministic)
        if ticker:
            try:
                from trading212_mcp_server import normalize_ticker
                original_ticker = ticker
                ticker = normalize_ticker(ticker)
                if ticker != original_ticker:
                     logger.info(f"Ticker normalized: {original_ticker} -> {ticker}")
                     context.ticker = ticker
            except ImportError:
                logger.warning("Could not import normalize_ticker from trading212_mcp_server")

        # Fetch OHLCV data if we need it
        ohlcv_data = []
        if (needs_quant or needs_forecast or ticker) and ticker and not needs_goal:
            status_manager.set_status("coordinator", "online", f"Fetching data for {ticker}...")
            # ... (fetch OHLCV code) ...
            ohlcv_data = await self._fetch_ohlcv(ticker)
            
            # Populate PriceData with history for charts
            if ohlcv_data:
                from market_context import PriceData, TimeSeriesItem
                # ... (populating price data) ...
                last_bar = ohlcv_data[-1]
                history_series = [
                    TimeSeriesItem(
                        timestamp=int(d['t']),
                        open=float(d['o']),
                        high=float(d['h']),
                        low=float(d['l']),
                        close=float(d['c']),
                        volume=float(d['v'])
                    ) for d in ohlcv_data
                ]
                context.price = PriceData(
                    ticker=ticker,
                    current_price=float(last_bar['c']),
                    history_series=history_series
                )
        
        # Prepare specialist contexts
        specialist_tasks = []
        
        if needs_portfolio:
            # Pass account_type to portfolio agent
            portfolio_context = {}
            if account_type:
                portfolio_context["account_type"] = account_type
            
            specialist_tasks.append(
                self._run_specialist(self.portfolio_agent, portfolio_context)
            )
        
        if needs_goal:
            # Extract params from intent or use defaults
            params = intent.get("params") or {}
            
            # Default fallbacks if params missing
            goal_context = {
                "initial_capital": params.get("capital", 1000.0),
                "risk_profile": params.get("risk", "MEDIUM"),
                "duration_years": params.get("years", 5.0),
                "target_returns_percent": 10.0 # Default assumption
            }
            
            # Map risk to target return heuristic if not provided
            risk = goal_context["risk_profile"]
            if risk == "LOW": goal_context["target_returns_percent"] = 5.0
            elif risk == "HIGH": goal_context["target_returns_percent"] = 15.0
            elif risk == "AGGRESSIVE_PLUS": goal_context["target_returns_percent"] = 25.0
            
            specialist_tasks.append(
                self._run_specialist(self.goal_planner_agent, goal_context)
            )

        # ... (rest of the conditions for quant, forecast, etc.) ...
        if needs_quant and ohlcv_data:
            specialist_tasks.append(
                self._run_specialist(self.quant_agent, {
                    "ticker": ticker,
                    "ohlcv_data": ohlcv_data
                })
            )
        
        if needs_forecast and ohlcv_data:
            specialist_tasks.append(
                self._run_specialist(self.forecasting_agent, {
                    "ticker": ticker,
                    "ohlcv_data": ohlcv_data,
                    "days": 5
                })
            )
            
        if needs_research:
            # SOTA: Always run research for analytical intents even without ticker
            specialist_tasks.append(
                self._run_specialist(self.research_agent, {
                    "ticker": ticker or "MARKET",
                    "company_name": ticker or "stock market"
                })
            )
            
        if (needs_social or context.intent == "market_analysis") and not any(t[0] == "SocialAgent" for t in specialist_tasks if isinstance(t, tuple)):
            specialist_tasks.append(
                self._run_specialist(self.social_agent, {
                    "ticker": ticker or "MARKET"
                })
            )
            
        if (needs_whale or context.intent == "market_analysis") and not any(t[0] == "WhaleAgent" for t in specialist_tasks if isinstance(t, tuple)):
            specialist_tasks.append(
                self._run_specialist(self.whale_agent, {
                    "ticker": ticker or "SPY" # Default to S&P 500 for general whale flow
                })
            )
        
        # Execute all specialists in parallel with timeouts
        if specialist_tasks:
            status_manager.set_status("coordinator", "online", f"Coordinating {len(specialist_tasks)} specialists...")
            
            # Run tasks with individual timeouts
            results = await asyncio.gather(*specialist_tasks, return_exceptions=True)
            
            # Aggregate results into context
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Specialist failed: {result}")
                    continue
                
                if not isinstance(result, AgentResponse):
                    continue
                
                # Add to context based on agent name
                context.add_agent_result(
                    result.agent_name,
                    result.success,
                    result.latency_ms
                )
                
                if result.success:
                    await self._merge_result_into_context(context, result)
        
        status_manager.set_status("coordinator", "ready", "Task complete")
        logger.info(f"Coordinator: Processed {intent['type']} query in {context.total_latency_ms:.1f}ms")
        
        return context
    
    async def _run_specialist(self, agent: BaseAgent, context: Dict[str, Any]) -> AgentResponse:
        """Run a specialist agent with error handling and timeout"""
        from status_manager import status_manager
        agent_key = agent.config.name.lower().replace("agent", "_agent")
        
        status_manager.set_status(agent_key, "working", "Executing task...")
        
        try:
            # 15s timeout per specialist to prevent hanging
            async with asyncio.timeout(15.0):
                result = await agent.execute(context)
                
                # COORDINATOR SELF-CORRECTION: Try to fix if it's a known data issue
                if not result.success and result.error:
                    error_msg = result.error.lower()
                    
                    # Trigger resolution if ticker not found or delisted (Tier 2 Escalation)
                    if any(x in error_msg for x in ["not found", "ticker", "delisted", "no data", "404"]):
                        logger.info(f"Coordinator: Escalating Ticker Resolution to Tier 2 (Search) for {agent.config.name}: {result.error}")
                        status_manager.set_status(agent_key, "working", "Escalating Ticker Resolution (Tier 2)...")
                        
                        if "ticker" in context:
                            ticker = context["ticker"]
                            status_manager.set_status(agent_key, "working", f"Searching for correct ticker for '{ticker}'...")
                            
                            new_ticker = await self._resolve_ticker_via_search(ticker)
                            
                            if new_ticker and new_ticker != ticker:
                                logger.info(f"Coordinator Tier 2: Success! Resolved {ticker} -> {new_ticker}")
                                status_manager.set_status(agent_key, "working", f"Retrying with resolved ticker: {new_ticker}")
                                
                                new_context = context.copy()
                                new_context["ticker"] = new_ticker
                                # Retry with new ticker
                                retry_result = await agent.execute(new_context)
                                if retry_result.success:
                                    status_manager.set_status(agent_key, "ready", f"Resolved via Tier 2")
                                    return retry_result
                                else:
                                    result = retry_result # Update original result for Tier 3 fallthrough

                        # Tier 3: LLM Self-Correction fallback (Reasoning)
                        logger.info(f"Coordinator: Escalating Ticker Resolution to Tier 3 (LLM) for {agent.config.name}")
                        status_manager.set_status(agent_key, "working", "Attempting LLM self-healing (Tier 3)...")
                        
                        fixed_result = await self._handle_specialist_error(agent, context, result.error)
                        if fixed_result:
                            logger.info(f"Coordinator Tier 3: Successfully healed error for {agent.config.name}")
                            status_manager.set_status(agent_key, "ready", "Fixed via Coordinator Self-Healing")
                            return fixed_result
                
                status_manager.set_status(agent_key, "ready", "Task complete")
                return result
        except asyncio.TimeoutError:
            error_msg = f"Timout after 15s"
            logger.warning(f"Specialist {agent.config.name} timed out")
            status_manager.set_status(agent_key, "error", "Timed out")
            return AgentResponse(
                agent_name=agent.config.name,
                success=False,
                data={},
                error=error_msg,
                latency_ms=15000
            )
        except Exception as e:
            logger.error(f"Specialist {agent.config.name} failed: {e}")
            
            # COORDINATOR SELF-CORRECTION: Try to fix if it's a known data issue
            if any(x in str(e).lower() for x in ["not found", "ticker", "delisted"]):
                status_manager.set_status(agent_key, "working", "Attempting self-correction...")
                fixed_result = await self._handle_specialist_error(agent, context, str(e))
                if fixed_result:
                    status_manager.set_status(agent_key, "ready", "Fixed via Coordinator")
                    return fixed_result

            status_manager.set_status(agent_key, "error", f"Failed: {str(e)}")
            return AgentResponse(
                agent_name=agent.config.name,
                success=False,
                data={},
                error=str(e),
                latency_ms=0
            )

    async def _resolve_ticker_via_search(self, term: str) -> Optional[str]:
        """
        Tier 2: Search-Augmented Discovery.
        Uses Trading212 search tool and string similarity (Levenshtein/Difflib)
        to verify the best match for ambiguous symbols or names.
        """
        try:
            logger.info(f"Coordinator Tier 2: Searching for correct ticker matching '{term}'")
            
            # Call search_instruments tool
            search_result = await self.mcp_client.call_tool("search_instruments", {"query": term})
            
            # Check if search_result is wrapped in TextContent or JSON string
            if hasattr(search_result, 'content'):
                content = search_result.content
                if isinstance(content, list) and len(content) > 0:
                    text = content[0].text if hasattr(content[0], 'text') else str(content[0])
                    search_result = json.loads(text)
            elif isinstance(search_result, str):
                search_result = json.loads(search_result)

            if search_result and isinstance(search_result, list) and len(search_result) > 0:
                # 1. Normalize candidates and prepare for comparison
                candidates = []
                for res in search_result:
                    ticker = res.get("ticker", "")
                    name = res.get("name", "")
                    if ticker:
                        candidates.append({
                            "ticker": ticker,
                            "name": name,
                            "search_score": difflib.SequenceMatcher(None, term.upper(), ticker.upper()).ratio(),
                            "name_score": difflib.SequenceMatcher(None, term.lower(), name.lower()).ratio()
                        })
                
                # 2. Find best match based on similarity
                # Priority: Exact Ticker Match > Ticker Similarity > Name Similarity
                best_match = None
                highest_score = 0.0
                
                for cand in candidates:
                    # Multi-factor score
                    score = max(cand["search_score"], cand["name_score"])
                    
                    # Exact ticker match boost
                    if cand["ticker"].upper() == term.upper():
                        score = 1.0
                    
                    if score > highest_score:
                        highest_score = score
                        best_match = cand
                
                # 3. Commit match if it meets threshold (e.g., > 0.6 similarity)
                if best_match and highest_score > 0.6:
                    found_ticker = best_match["ticker"]
                    
                    # Normalize the found ticker (SOTA rule path)
                    from trading212_mcp_server import normalize_ticker
                    normalized = normalize_ticker(found_ticker)
                    
                    logger.info(f"Coordinator Tier 2: Found best match '{best_match['name']}' ({found_ticker}) score={highest_score:.2f} -> {normalized}")
                    return normalized
                else:
                    logger.warning(f"Coordinator Tier 2: No match exceeded similarity threshold (max={highest_score:.2f})")
                    
        except Exception as e:
            logger.warning(f"Ticker search discovery failed for {term}: {e}")
        
        return None

    async def _handle_specialist_error(self, agent: BaseAgent, context: Dict[str, Any], error: str) -> Optional[AgentResponse]:
        """Tier 3: Attempt to fix a specialist error using LLM reasoning (Self-Healing)"""
        if not self.llm:
            return None

        prompt = f"""
        Agent {agent.config.name} failed with error: "{error}" while processing {context}.
        The error suggests a ticker or data mapping issue. 
        Can we fix this by modifying the input context? 
        If it's a Ticker Not Found/Delisted error, try adding/removing .L or correcting the symbol.
        Leveraged stocks often have digits (MAG5, GLD3) - ensure they are preserved.
        
        Write a Python script to return a modified 'new_context' dict.
        Return ONLY a JSON block:
        {{
          "reasoning": "Explain the fix",
          "code": "new_context = context.copy()\\nif 'ticker' in new_context: new_context['ticker'] = new_context['ticker'] + '.L'"
        }}
        """
        try:
            from langchain_core.messages import HumanMessage
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])
            content = response.content if hasattr(response, 'content') else str(response)
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                try:
                    fix_data = json.loads(match.group(), strict=False)
                    python_code = fix_data.get("code", "")
                except json.JSONDecodeError as je:
                    logger.warning(f"Coordinator: JSON decode failed for fix: {je}")
                    return None
                if python_code:
                    logger.debug(f"Coordinator: Executing fix script:\n{python_code}")
                    exec_res = self.python_executor.execute(python_code, {"context": context})
                    if exec_res["success"] and exec_res["result"]:
                        new_context = exec_res["result"]
                        logger.info(f"Coordinator retry for {agent.config.name} with fixed context: {new_context}")
                        # Retry once with fixed context
                        return await agent.execute(new_context)
                    else:
                        logger.warning(f"Coordinator: Fix script failed: {exec_res.get('error') or 'No result'}")
        except Exception as e:
            logger.warning(f"Self-correction failed: {e}")
            
        return None
    
    async def _merge_result_into_context(self, context: MarketContext, result: AgentResponse):
        """Merge specialist result into market context"""
        from market_context import ForecastData, QuantData, PortfolioData, ResearchData, SocialData, WhaleData
        
        data = result.data
        agent_name = result.agent_name
        
        if agent_name == "ForecastingAgent":
            context.forecast = ForecastData(**data)
        elif agent_name == "QuantAgent":
            context.quant = QuantData(**data)
        elif agent_name == "PortfolioAgent":
            context.portfolio = PortfolioData(**data)
        elif agent_name == "ResearchAgent":
            context.research = ResearchData(**data)
        elif agent_name == "SocialAgent":
            context.social = SocialData(**data)
        elif agent_name == "WhaleAgent":
            context.whale = WhaleData(**data)
        elif agent_name == "GoalPlannerAgent":
            # Note: GoalData must be imported if not already in context, but dynamic import ok
            from market_context import GoalData
            context.goal = GoalData(**data)

    async def _prepare_market_data(self, ticker: str, needs_quant: bool, needs_forecast: bool) -> List[Dict]:
        """Prepare market data including OHLCV fetching and PriceData population."""
        from status_manager import status_manager
        ohlcv_data = []
        if (needs_quant or needs_forecast or ticker) and ticker:
            status_manager.set_status("coordinator", "online", f"Fetching data for {ticker}...")
            ohlcv_data = await self._fetch_ohlcv(ticker)

            # Populate PriceData with history for charts
            if ohlcv_data:
                from market_context import PriceData, TimeSeriesItem
                last_bar = ohlcv_data[-1]
                history_series = [
                    TimeSeriesItem(
                        timestamp=int(d['t']),
                        open=float(d['o']),
                        high=float(d['h']),
                        low=float(d['l']),
                        close=float(d['c']),
                        volume=int(d['v'])
                    ) for d in ohlcv_data[-50:]  # Last 50 bars for chart
                ]
                # Add to context (assuming context is passed or use self.context)
                # For now, just return ohlcv_data
        return ohlcv_data

    async def _execute_specialists(self, context: MarketContext, ohlcv_data: List[Dict], needs_quant: bool, needs_portfolio: bool, needs_forecast: bool, needs_research: bool, needs_social: bool, needs_whale: bool, account_type: str):
        """Execute specialist agents in parallel."""
        from status_manager import status_manager
        tasks = []
        if needs_quant and ohlcv_data:
            tasks.append(("QuantAgent", QuantAgent().execute({"ticker": context.ticker, "ohlcv_data": ohlcv_data})))
        if needs_portfolio:
            tasks.append(("PortfolioAgent", PortfolioAgent().execute({"account_type": account_type})))
        if needs_forecast and ohlcv_data:
            tasks.append(("ForecastingAgent", ForecastingAgent().execute({"ticker": context.ticker, "ohlcv_data": ohlcv_data, "days": 5})))
        if needs_research:
            tasks.append(("ResearchAgent", ResearchAgent().execute({"ticker": context.ticker, "company_name": None})))
        if needs_social:
            tasks.append(("SocialAgent", SocialAgent().execute({"ticker": context.ticker})))
        if needs_whale:
            tasks.append(("WhaleAgent", WhaleAgent().execute({"ticker": context.ticker})))

        # Execute in parallel
        results = await asyncio.gather(*[task[1] for task in tasks], return_exceptions=True)
        for (agent_name, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                logger.error(f"{agent_name} failed: {result}")
                continue
            if result.success:
                self._populate_context(context, agent_name, result.data)
            else:
                logger.warning(f"{agent_name} error: {result.error}")

        status_manager.set_status("coordinator", "ready", f"Completed analysis for {context.ticker or 'general'}")

    async def _fetch_ohlcv(self, ticker: str) -> List[Dict]:
        """Fetch OHLCV data for technical analysis"""
        try:
            from data_engine import get_alpaca_client
            alpaca = get_alpaca_client()
            
            result = await alpaca.get_historical_bars(ticker, limit=200)
            
            if result and "bars" in result:
                return result["bars"]
            
            return []
        except Exception as e:
            logger.error(f"Failed to fetch OHLCV for {ticker}: {e}")
            return []


    def _resolve_ticker_from_history(self, history: List[Dict]) -> Optional[str]:
        """Attempt to find last discussed ticker in history"""
        stop_words = {
            "WHAT", "WHEN", "WHERE", "THAT", "THIS", "HAVE", "BEEN", "WILL", "FROM", "WITH", 
            "YOUR", "THEY", "DOES", "WANT", "NEED", "LIKE", "LOOK", "TELL", "SHOW", "GIVE", 
            "FIND", "ANAL", "BEST", "GOOD", "LONG", "TERM", "COST", "HIGH", "TIME",
            "DATA", "REAL", "USER", "SURE", "HERE", "CHAT", "MODE", "HELP", "MAKE", "LIST",
            "TYPE", "CODE", "READ", "FILE", "VIEW", "EDIT", "TOOL", "CALL", "NAME", "ARGS"
        }
        # Refine stop words to be safe
        safe_stop_words = {
            "WHAT", "WHEN", "WHERE", "THAT", "THIS", "HAVE", "BEEN", "WILL", "FROM", "WITH", 
            "YOUR", "THEY", "DOES", "WANT", "NEED", "LIKE", "LOOK", "TELL", "SHOW", "GIVE", 
            "FIND", "BEST", "GOOD", "TIME", "DATA", "REAL", "USER", "SURE", "HERE", "HELP", 
            "MAKE", "LIST", "MANY", "MUCH", "SOME", "VERY", "ALSO", "INTO", "ONTO",
            "IS", "ARE", "WAS", "WERE", "CAN", "COULD", "SHOULD", "WOULD", "PLEASE", "THANKS",
            "THANK", "YOU", "ABOUT", "FOR", "AND", "BUT", "OR", "NOT", "YES", "NO", "OKAY", "OK"
        }

        # Look at last 5 messages 
        # (history is passed in chronological order from chat_manager usually, verify this)
        # chat_routes calls chat_manager.load_history which returns reversed(messages) -> chronological.
        # So history[-1] is the most recent.
        
        for msg in reversed(history):
            content = msg.get("content", "").upper()
            
            # Check for $TICKER format first
            dollar_matches = re.findall(r'\$([A-Z]{3,5})', content)
            if dollar_matches:
                return dollar_matches[-1] # Most recent match in message
            
            # Basic word analysis
            words = content.split()
            candidates = []
            for word in words:
                # Remove common punctuation but allow dots and digits for tickers like VOD.L or 3GLD
                clean_word = word.strip(".,!?;:\"'()[]{}")

                # Check if it looks like a ticker:
                # 1. 2-6 chars long
                # 2. Mostly uppercase (heuristic)
                # 3. Alphanumeric or contains dot
                if 2 <= len(clean_word) <= 6:
                     # Heuristic: mostly upper case or mixed case (not all lower)
                     if not clean_word.islower():
                         # Allow dots (VOD.L) and digits (3GLD), remove other junk
                         sanitized = "".join(ch for ch in clean_word if ch.isalnum() or ch == '.')

                         if sanitized.upper() not in safe_stop_words and len(sanitized) >= 2:
                             # Additional check: shouldn't be just digits
                             if not sanitized.isdigit():
                                 candidates.append(sanitized)
            
            # If candidates found, return the last one mentioned? Or first?
            # "Analyze AAPL and TSLA" -> usually implies plural, but if we pick one, context is maintained.
            if candidates:
                return candidates[-1] # Return most recently mentioned
                
        return None



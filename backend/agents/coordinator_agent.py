"""
Coordinator Agent - Orchestrates all specialist agents
Static SOTA model that routes queries and aggregates results
"""

from .base_agent import BaseAgent, AgentResponse
from market_context import MarketContext
from . import QuantAgent, PortfolioAgent, ForecastingAgent, ResearchAgent, SocialAgent, WhaleAgent, GoalPlannerAgent
from .decision_agent import DecisionAgent
from data_fabricator import DataFabricator
from typing import Dict, Any, List, Optional
import asyncio
import logging
import re
import json
import difflib

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

Entity Extraction:
Always extract the primary ticker symbol if present (e.g., "AAPL", "Tesla", "Lloyds").
Normalize company names to tickers if possible (e.g. "Tesla" -> "TSLA").

Output Format (JSON ONLY):
{
  "type": "exact_intent_string",
  "needs": ["list", "of", "agents"],
  "reason": "Brief explanation",
  "primary_ticker": "AAPL",
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
    
    def __init__(self, mcp_client=None, chat_manager=None, model_name: str = "granite-tiny"):
        from app_context import state
        self.model_name = model_name
        self.mcp_client = mcp_client or state.mcp_client
        self.chat_manager = chat_manager or state.chat_manager
        
        # Initialize Core Components (Centralized Arch)
        self.data_fabricator = DataFabricator()
        self.decision_agent = DecisionAgent(model_name=model_name, mcp_client=self.mcp_client)
        
        # Initialize specialist agents
        self.quant_agent = QuantAgent()
        self.portfolio_agent = PortfolioAgent()
        self.forecasting_agent = ForecastingAgent()
        self.research_agent = ResearchAgent()
        self.social_agent = SocialAgent()
        self.whale_agent = WhaleAgent()
        self.goal_planner_agent = GoalPlannerAgent()
        
        self.llm = None
        # Initialize LLM will be called on first process_query if not already done
        
        # Initialize Safe Python Executor for data cleaning/fixes
        from utils import get_safe_executor
        self.python_executor = get_safe_executor()
        
        logger.info(f"CoordinatorAgent initialized (Centralized Mode) with model: {model_name}")
    
    async def _initialize_llm(self):
        """Initialize the routing LLM using the Factory"""
        try:
            from .llm_factory import LLMFactory
            self.llm = await LLMFactory.create_llm(self.model_name)
            
            # Update local model name if auto-detected
            if hasattr(self.llm, "active_model_id"):
                self.model_name = self.llm.active_model_id
                
        except Exception as e:
            logger.warning(f"Failed to initialize Coordinator LLM: {e}. Routing will be static.")
            self.llm = None
    
    async def _classify_intent(self, query: str) -> Dict[str, Any]:
        """Classify user intent using Granite-Optimized Few-Shot Prompting"""
        # Ensure LLM is initialized (async init for LM Studio)
        if not self.llm:
            await self._initialize_llm()
            
        if not self.llm:
            return {"type": "analytical", "needs": ["portfolio", "quant", "forecast"]}
            
        clean_query = query.strip()[:500]
        # ... prompt remains same ...
        prompt = f"""You are the Coordinator Agent.
Route queries to specialist agents.
Format your response EXACTLY as:
INTENT: [intent_name]
TICKER: [symbol or NONE]
REASON: [short explanation]

Intents:
- price_check (needs quant)
- market_analysis (needs quant, forecast, research, social)
- portfolio_query (needs portfolio)
- goal_planning (needs goal_planner)

Examples:
Query: "Analyze Apple stock"
INTENT: market_analysis
TICKER: AAPL
REASON: User wants deep dive analysis

Query: "How is my portfolio doing?"
INTENT: portfolio_query
TICKER: NONE
REASON: User asking about holdings

Query: "portfolio analysis?"
INTENT: portfolio_query
TICKER: NONE
REASON: User wants holistic review of holdings

Query: "{clean_query}"
"""

        try:
            if hasattr(self.llm, "ainvoke"):
                from langchain_core.messages import HumanMessage
                response = await self.llm.ainvoke([HumanMessage(content=prompt)])
                content = response.content if hasattr(response, 'content') else str(response)
            else:
                # LMStudioClient
                from model_config import get_model_info
                info = get_model_info(self.model_name)
                model_id = info.get("model_id") or self.model_name
                
                # Check loaded models to pick one if auto
                if "auto" in model_id:
                    models = await self.llm.list_models()
                    if models:
                        model_id = models[0]["id"]
                
                resp = await self.llm.chat(model_id=model_id, input_text=prompt, temperature=0, max_tokens=512)
                content = resp.get("content", "")

            # PARSE KEY-VALUE OUTPUT (Robust Regex)
            intent_match = re.search(r'INTENT:\s*(\w+)', content, re.IGNORECASE)
            ticker_match = re.search(r'TICKER:\s*([A-Z0-9.]+)', content, re.IGNORECASE)
            
            intent_type = intent_match.group(1).lower() if intent_match else "market_analysis"
            ticker = ticker_match.group(1).upper() if ticker_match and "NONE" not in ticker_match.group(1).upper() else None
            
            # Map intents to needs (Hardcoded logic is safer than LLM predicting list)
            needs_map = {
                "price_check": ["quant"],
                "market_analysis": ["quant", "forecast", "research", "social", "whale", "portfolio"],
                "portfolio_query": ["portfolio", "quant", "forecast"],
                "goal_planning": ["goal_planner", "portfolio"],
                "educational": []
            }
            
            needs = needs_map.get(intent_type, ["quant", "forecast"])
            
            return {
                "type": intent_type,
                "needs": needs,
                "primary_ticker": ticker,
                "reason": "Parsed from KV output"
            }
                
        except Exception as e:
            logger.error(f"Intent classification failed: {e}")
            
        # Default safety fallback
        return {"type": "analytical", "needs": ["portfolio", "quant", "forecast"]}

    async def process_query(self, query: str, ticker: Optional[str] = None, account_type: Optional[str] = None, history: List[Dict] = []) -> MarketContext:
        """
        Process user query and coordinate specialist agents based on intent.
        """
        from status_manager import status_manager
        import uuid
        from app_logging import correlation_id_ctx
        
        # Ensure tracking of the decision_id/correlation_id globally for this request
        c_id = correlation_id_ctx.get() if correlation_id_ctx else None
        if not c_id or c_id == "-":
            c_id = str(uuid.uuid4())
            if correlation_id_ctx:
                correlation_id_ctx.set(c_id)
        
        # 0. Resolve Context (Ticker) from History if missing
        if not ticker and history:
            ticker = self._resolve_ticker_from_history(history)
            if ticker:
                logger.info(f"Resolved ticker from history: {ticker}")

        status_manager.set_status("coordinator", "online", f"Analyzing: '{query}'", model=self.model_name)
        
        # SOTA 2026: Emit Coordinator Start Telemetry
        from .messenger import AgentMessage, get_messenger
        messenger = get_messenger()
        await messenger.send_message(AgentMessage(
            sender="CoordinatorAgent",
            recipient="broadcast",
            subject="agent_started",
            payload={"agent": "CoordinatorAgent", "query_snippet": query[:50]},
            correlation_id=c_id
        ))

        # 1. Intent Classification
        intent = await self._classify_intent(query)
        logger.info(f"Coordinator Intent: {intent['type']} - Needs: {intent.get('needs', [])}")

        # 1a. Ticker Extraction fallback
        if not ticker:
            ticker = intent.get("primary_ticker")
            
            # Tier 3: Regex Fallback (if LLM failed to extract)
            if not ticker:
                from utils import extract_ticker_from_text
                ticker = extract_ticker_from_text(query)

            if ticker:
                logger.info(f"Coordinator extracted ticker (Fallback): {ticker}")
        
        # 1b. Account Detection
        detected_account = account_type
        if not detected_account:
            detected_account = self.decision_agent._detect_account_mentions(query)
            if detected_account == "all" and account_type: # keep explicit override if provided
                 pass 
            elif detected_account != "all":
                 account_type = detected_account

        # COORDINATOR FIX: Robust normalization via T212 rules (fast & deterministic)
        if ticker:
            try:
                from trading212_mcp_server import normalize_ticker
                original_ticker = ticker
                ticker = normalize_ticker(ticker)
                if ticker != original_ticker:
                     logger.info(f"Ticker normalized: {original_ticker} -> {ticker}")
            except ImportError:
                logger.warning("Could not import normalize_ticker from trading212_mcp_server")
            except Exception as e:
                logger.error(f"Error during ticker normalization: {e}")

        # 2. CENTRALIZED DATA FABRICATION
        status_manager.set_status("coordinator", "working", "Fabricating Market Context...")
        
        context = await self.data_fabricator.fabricate_context(
            intent=intent.get("type", "analytical"),
            ticker=ticker,
            account_type=account_type,
            user_settings={}
        )
        
        # Inject Intent metadata
        context.routing_reason = intent.get("reason", "")
        if account_type:
            context.user_context["account_type"] = account_type

        # --- SKILL INJECTION ---
        # Coordinator also benefits from skills (e.g. knowing 'tax' queries need different handling)
        from utils.skill_loader import get_skill_loader
        skills_text = get_skill_loader().get_relevant_skills(query)
        if skills_text:
             context.user_context["expert_skills"] = skills_text
        # -----------------------
        
        # COORDINATOR FIX: Ticker normalization
        if context.ticker and (not context.ticker.isalpha() or len(context.ticker) < 2):
            context.ticker = await self._attempt_ticker_fix(context.ticker)

        # 3. PARALLEL AGENT_EXECUTION (Pure Processors)
        needs = intent.get("needs", [])
        specialist_tasks = []
        
        # Broadcast intent to the agent bus (SOTA Decentralized Path)
        from .messenger import AgentMessage
        from .governance import get_governance
        from app_logging import correlation_id_ctx
        
        c_id = correlation_id_ctx.get()
        broadcast_msg = AgentMessage(
            sender="CoordinatorAgent",
            recipient="broadcast",
            subject="intent_classified",
            payload={"intent": intent, "ticker": ticker, "context_summary": context.get_summary()},
            correlation_id=c_id
        )
        await get_governance().secure_dispatch(broadcast_msg)

        # Helper to wrap agent execution
        async def run_agent(agent, ctx_data):
            return await self._run_specialist(agent, ctx_data)

        # Prepare Tasks
        # DUPLICATE REMOVED: PortfolioAgent task is handled later with correct context
            
        if "goal_planner" in needs:
            params = intent.get("params", {})
            goal_context = {
                 "initial_capital": params.get("capital", 1000.0),
                 "risk_profile": params.get("risk", "MEDIUM"),
                 "duration_years": params.get("years", 5.0)
            }
            specialist_tasks.append(run_agent(self.goal_planner_agent, goal_context))

        # Market Agents using PRE-FETCHED Data
        if "quant" in needs and context.price:
            # Map PriceData back to list of dicts for QuantAgent
            ohlcv_remapped = [{
                't': b.timestamp, 'o': b.open, 'h': b.high, 'l': b.low, 'c': b.close, 'v': b.volume
            } for b in context.price.history_series]
            
            specialist_tasks.append(run_agent(self.quant_agent, {
                "ticker": context.ticker,
                "ohlcv_data": ohlcv_remapped
            }))
            
        if "forecast" in needs and context.price:
             ohlcv_remapped = [{
                't': b.timestamp, 'o': b.open, 'h': b.high, 'l': b.low, 'c': b.close, 'v': b.volume
            } for b in context.price.history_series]
             specialist_tasks.append(run_agent(self.forecasting_agent, {
                "ticker": context.ticker,
                "ohlcv_data": ohlcv_remapped,
                "days": 5
            }))
            
        if "research" in needs:
            # Phase 1: ResearchAgent still fetches info if needed, but context is primary
            specialist_tasks.append(run_agent(self.research_agent, {"ticker": context.ticker}))
            
        if "social" in needs:
            specialist_tasks.append(run_agent(self.social_agent, {"ticker": context.ticker}))
            
        if "whale" in needs:
            specialist_tasks.append(run_agent(self.whale_agent, {"ticker": context.ticker}))

        if "portfolio" in needs:
            # Ensure account type is passed from context or analysis
            acc_type = context.user_context.get("account_type", "all")
            specialist_tasks.append(run_agent(self.portfolio_agent, {
                "account_type": acc_type,
                "force_refresh": True
            }))
            
        # Execute Specialists
        if specialist_tasks:
            status_manager.set_status("coordinator", "working", f"Coordinating {len(specialist_tasks)} specialists...")
            results = await asyncio.gather(*specialist_tasks, return_exceptions=True)
            
            for res in results:
                if isinstance(res, Exception): 
                    logger.error(f"Agent failed: {res}")
                    continue
                if isinstance(res, AgentResponse):
                    # Pass full telemetry to context
                    context.add_agent_result(
                        res.agent_name, 
                        res.success, 
                        res.latency_ms, 
                        telemetry=res.telemetry
                    )
                    if res.success:
                        await self._merge_result_into_context(context, res)

        # 4. FINAL SYNTHESIS (Decision Agent)
        status_manager.set_status("coordinator", "working", "Synthesizing Final Decision...")
        
        try:
             final_response = await self.decision_agent.make_decision(context, query)
             # Attach decision to context for API return
             context.user_context["final_answer"] = final_response
        except Exception as e:
            logger.error(f"Decision synthesis failed: {e}")
            context.user_context["final_answer"] = f"Analysis complete, but synthesis failed: {e}"

        status_manager.set_status("coordinator", "ready", "Task complete")
        
        # SOTA 2026: Emit Coordinator Completion Telemetry
        await messenger.send_message(AgentMessage(
            sender="CoordinatorAgent",
            recipient="broadcast",
            subject="agent_complete",
            payload={"agent": "CoordinatorAgent", "success": True, "intent": intent.get("type")},
            correlation_id=c_id
        ))
        
        return context
    
    async def _run_specialist(self, agent: BaseAgent, context: Dict[str, Any]) -> AgentResponse:
        """Run a specialist agent with error handling and timeout"""
        from status_manager import status_manager
        from .messenger import AgentMessage, get_messenger
        from app_logging import correlation_id_ctx
        
        agent_key = agent.config.name.lower().replace("agent", "_agent")
        c_id = correlation_id_ctx.get()
        messenger = get_messenger()
        
        status_manager.set_status(agent_key, "working", "Executing task...")
        
        # Emit telemetry: Start
        start_msg = AgentMessage(
            sender="CoordinatorAgent",
            recipient=agent.config.name,
            subject="agent_started",
            payload={"agent": agent.config.name, "ticker": context.get("ticker")},
            correlation_id=c_id
        )
        await messenger.send_message(start_msg)
        
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
                                    status_manager.set_status(agent_key, "ready", "Resolved via Tier 2")
                                    result = retry_result
                                else:
                                    result = retry_result # Update original result for Tier 3 fallthrough

                        if not result.success:
                            # Tier 3: LLM Self-Correction fallback (Reasoning)
                            logger.info(f"Coordinator: Escalating Ticker Resolution to Tier 3 (LLM) for {agent.config.name}")
                            status_manager.set_status(agent_key, "working", "Attempting LLM self-healing (Tier 3)...")
                            
                            fixed_result = await self._handle_specialist_error(agent, context, result.error)
                            if fixed_result:
                                logger.info(f"Coordinator Tier 3: Successfully healed error for {agent.config.name}")
                                status_manager.set_status(agent_key, "ready", "Fixed via Coordinator Self-Healing")
                                result = fixed_result
                
                status_manager.set_status(agent_key, "ready", "Task complete")
                
                # Emit telemetry: Success/Complete
                complete_msg = AgentMessage(
                    sender=agent.config.name,
                    recipient="CoordinatorAgent",
                    subject="agent_complete",
                    payload={
                        "agent": agent.config.name, 
                        "success": result.success,
                        "latency_ms": result.latency_ms,
                        "ticker": context.get("ticker"),
                        "error": result.error if not result.success else None
                    },
                    correlation_id=c_id
                )
                await messenger.send_message(complete_msg)
                
                return result
        except asyncio.TimeoutError:
            error_msg = "Timout after 15s"
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

    async def _attempt_ticker_fix(self, ticker: str) -> Optional[str]:
        """
        Attempt to fix malformed tickers or those containing special characters
        that might have triggered the validation check (e.g. VOD.L is not alpha).
        """
        if not ticker:
            return None

        # Strip trailing dots first
        ticker = ticker.strip('.')

        # 1. Allow dot notation for UK/Exchanges (e.g. VOD.L, BRK.B)
        if "." in ticker and ticker.replace(".", "").isalnum():
            # It's likely valid if it has 1 dot and rest are alphanum
            return ticker

        # 2. Strip noise (punctuation other than dot)
        clean = "".join(c for c in ticker if c.isalnum() or c == '.')

        # If the result is a clean ticker (e.g. "AAPL."), strip trailing dot
        clean = clean.strip('.')

        if len(clean) >= 2:
            return clean

        # 3. Fallback to search if really broken or too short
        return await self._resolve_ticker_via_search(ticker)

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
                    try:
                        from trading212_mcp_server import normalize_ticker
                        normalized = normalize_ticker(found_ticker)
                    except ImportError:
                        logger.warning("Could not import normalize_ticker from trading212_mcp_server")
                        normalized = found_ticker
                    except Exception as e:
                        logger.error(f"Error during ticker normalization in search fallback: {e}")
                        normalized = found_ticker
                    
                    logger.info(f"Coordinator Tier 2: Found best match '{best_match['name']}' ({found_ticker}) score={highest_score:.2f} -> {normalized}")
                    return normalized
                else:
                    logger.warning(f"Coordinator Tier 2: No match exceeded similarity threshold (max={highest_score:.2f})")
                    
        except Exception as e:
            logger.warning(f"Ticker search discovery failed for {term}: {e}")
        
        return None

    async def _handle_specialist_error(self, agent: BaseAgent, context: Dict[str, Any], error: str) -> Optional[AgentResponse]:
        """Tier 3: Attempt to fix a specialist error using LLM reasoning (Self-Healing via Docker Sandbox)"""
        if not self.llm:
            return None

        prompt = f"""
        Agent {agent.config.name} failed with error: "{error}" while processing {context}.
        The error suggests a ticker or data mapping issue. 
        Can we fix this by modifying the input context? 
        If it's a Ticker Not Found/Delisted error, try adding/removing .L or correcting the symbol.
        Leveraged stocks often have digits (MAG5, GLD3) - ensure they are preserved.
        
        Write a Python script that prints the JSON of the modified 'new_context'.
        The script will run in an ISOLATED Docker container.
        
        Input Context:
        {json.dumps(context)}
        
        Return ONLY a JSON block with the script:
        {{
          "reasoning": "Explain the fix",
          "code": "import json\\ncontext = {json.dumps(context)}\\n# ... fix logic ...\\nprint(json.dumps(context))"
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
                    logger.info(f"Coordinator: Delegating fix to Docker Sandbox...")
                    # SOTA 2026: Use Docker MCP for isolation
                    try:
                        # Call docker_run_python tool
                        # We assume the tool is registered in the MCP client available to the coordinator
                        result = await self.mcp_client.call_tool("docker_run_python", {"script": python_code})
                        
                        # Parse the output from the tool
                        if result and result.content:
                            output_text = result.content[0].text
                            # The tool returns a string rep of a dict: {'stdout': '...', ...}
                            # We need to parse that string safely
                            import ast
                            exec_res = ast.literal_eval(output_text)
                            
                            if exec_res.get("exit_code") == 0:
                                stdout = exec_res.get("stdout", "")
                                new_context = json.loads(stdout)
                                logger.info(f"Coordinator retry for {agent.config.name} with fixed context: {new_context}")
                                return await agent.execute(new_context)
                            else:
                                logger.warning(f"Docker execution failed: {exec_res}")
                    except Exception as e:
                        logger.error(f"Failed to execute Docker fix: {e}")
                        
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
                from market_context import TimeSeriesItem
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
            status_manager.set_status("forecasting_agent", "running", "TTM Price Prediction...")
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
        # history is passed in chronological order. history[-1] is the most recent.
        from utils import extract_ticker_from_text
        for msg in reversed(history):
            content = msg.get("content", "")
            if not content:
                continue
            found = extract_ticker_from_text(content, find_last=False)
            if found:
                return found
                
        return None

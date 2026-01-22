# Fix Portfolio Agent Initialization Error

## Problem
The portfolio endpoint is failing with error: `'MultiMCPManager' object has no attribute 'name'`

## Root Cause
In `/Users/sanketmane/Codes/Growin App/backend/routes/market_routes.py` line 46, the PortfolioAgent is being initialized incorrectly:

```python
agent = PortfolioAgent(state.mcp_client)
```

But the PortfolioAgent `__init__` method expects an `AgentConfig` object, not the MCP client. The PortfolioAgent already accesses the MCP client from the global state internally.

## Solution
Change the initialization to match the pattern used everywhere else:

```python
agent = PortfolioAgent()  # No arguments
```

## Evidence
- All other agents in the codebase are initialized with `AgentName()` (no arguments)
- The PortfolioAgent creates its own AgentConfig if none is provided
- The PortfolioAgent sets `self.mcp_client = state.mcp_client` internally
- The coordinator_agent.py correctly initializes it as `PortfolioAgent()`

## Implementation Plan
1. Edit `/Users/sanketmane/Codes/Growin App/backend/routes/market_routes.py` line 46
2. Change `PortfolioAgent(state.mcp_client)` to `PortfolioAgent()`
3. Test the portfolio endpoint to ensure it works

## Risk Assessment
- Low risk: This matches the established pattern in the codebase
- The change is minimal and aligns with how all other agents are initialized
- No breaking changes to the API or other components</content>
<parameter name="filePath">/Users/sanketmane/Codes/Growin App/.opencode/plans/fix-portfolio-agent-init.md
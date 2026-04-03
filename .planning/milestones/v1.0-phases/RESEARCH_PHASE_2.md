# Research Summary: Phase 2 (Frontend & Telemetry)

## 1. Backend Functionality Map (Source: Codebase Investigator)
The backend (v2.0.0) exposes a robust set of financial AI capabilities, primarily via REST/FastAPI.

### Active Endpoints
- **Conversational AI**: `/api/chat/message` supports JSON and **SSE Streaming**.
- **Agent Analysis**: `/agent/analyze` for ad-hoc specialist queries.
- **Financial Data**: `/portfolio/live` (T212), `/portfolio/history`, `/api/chart/{symbol}` (Historical Data).
- **Goal Planning**: `/api/goal/plan` (Monte Carlo simulations) & `/api/goal/execute`.
- **System Status**: `/api/agents/status` (Agent health) & `/api/models/available` (Local/Remote LLMs).

### Data Models (Key for Frontend Mapping)
- **`MarketContext`**: The "God Object" containing price, forecast, quant, and research data.
- **`PortfolioData`**: High-precision decimal based portfolio summary.
- **`GoalData`**: Contains simulated growth paths (perfect for charting).

### Gaps
- **Real-time Ticks**: No WebSocket for sub-second price updates (Polling required).
- **Auth**: No user authentication (Localhost trust model).

## 2. SOTA Frontend Patterns (Source: Growin Research Notebook)
To exploit the backend's "Agentic Debate" and "Precision" features, the frontend must adopt:

### Visualization Strategy
- **Swift Charts**: Use for rendering `TimeSeriesItem` arrays from `PriceData` and `ForecastData`.
- **Live P&L**: Use `@Observable` + `AsyncStream` to consume SSE events or poll `/portfolio/live` without blocking the UI.

### Observability UI (Telemetry)
- **Transparency**: Use `DisclosureGroup` or a "Reasoning Sidebar" to show the `agents_executed` trace from `MarketContext`.
- **Debate Visualization**: Color-code agent outputs (e.g., Quant=Blue, Sentiment=Red) to visualize the "Contradiction Resolution" phase.

### Streaming UX
- **Incremental Rendering**: Parse SSE tokens to render Markdown progressively.
- **Interactive Widgets**: Intercept structured JSON chunks (e.g., `GoalData`) and render native SwiftUI Views (Goal Sliders, Accept/Reject Buttons) instead of raw text.

### Human-in-the-Loop
- **Approval Gates**: For `/api/goal/execute` actions, mandate a native "Slide to Confirm" or distinct Approval UI to satisfy safety requirements.

## 3. Optimization Opportunities
- **Local Inference**: Frontend should allow toggling between "Cloud" and "Local (MLX)" models via `/api/models/available`.
- **Data Hydration**: The frontend can "hydrate" the chat by pre-fetching `/portfolio/live` in the background to make the first query faster.

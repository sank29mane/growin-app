# Phase 47: Multimodal Intelligence & Deep Integration - Context

**Gathered:** 2026-06-17 (assumptions mode)
**Status:** Ready for planning

<domain>
## Phase Boundary

Infusing vision intelligence into trading workflows and asset-level fast actions. This includes enabling Gemma 4 multimodal support for image/chart upload analysis, building dynamic interactive tiles for real-time asset monitoring, and establishing custom URL deep linking from chat reasoning results to the native SwiftUI Sovereign Ledger.
</domain>

<decisions>
## Implementation Decisions

### Technical Approach for Image Upload & Transmission
- **D-20:** Base64-encoded inline strings will be used to transmit uploaded images/charts within the existing JSON `ChatMessage` payload, rather than implementing a multipart/form-data upload system.
  - *Details:* The FastAPI backend conversation APIs (`chat_routes.py`) and the shared structures in `app_context.py` are built around JSON Pydantic payloads. SwiftUI client code will package user-selected images as Base64 strings.

### VLM Hardware Execution & Feature Extraction
- **D-21:** Fork model loading and inference paths inside `mlx_engine.py` depending on model type. If loading `gemma-4` (VLM), load via `mlx_vlm.load` and generate text via `mlx_vlm.generate(...)` with image arrays/paths instead of calling standard `mlx_lm` functions.
  - *Details:* `mlx-vlm` API expects a processed formatted prompt containing the `<|image|>` token. Google Gemma-4 prompt templates require Dialogue structure tags `<|turn>user` / `<|turn>system` and placing the `<|image|>` token before the text description.

### Real-Time Updates for Interactive Tiles
- **D-22:** Bind SwiftUI Interactive Tiles inside the chat panel directly to the existing frontend `PortfolioSummaryObserver` or `DashboardViewModel` to receive real-time price updates via SSE stream metadata events, avoiding redundant WebSocket connections.

### Deep Linking & SwiftUI Link Interception
- **D-23:** Custom URL scheme links (e.g. `growin://ledger/{ticker}`) in SwiftUI `Text` views will be intercepted locally by attaching `.environment(\.openURL, ...)` in `ThemeComponents.swift`, bypassing the macOS text selection interception bug and avoiding OS deep-linking round-trip duplication.

### Gemma 4 Multimodal Best Practices
- **D-24:** Adhere to Google's prompting conventions: Place the `<|image|>` token *before* the query content in the user's turn. Strip thought tokens between turns in multi-turn conversations if "Thinking" mode is active to preserve context window tokens.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

- `backend/mlx_engine.py` — Engine room where MLX/VLM model loading and generation logic is handled.
- `backend/routes/chat_routes.py` — Backend FastAPI route handling `/api/chat/message` payloads.
- `Growin/Views/Chat/AIChatPanelView.swift` — SwiftUI container for the AI chat panel.
- `Growin/ThemeComponents.swift` — Custom SwiftUI markdown text renderer and typography setup.
- `Growin/AgentClient.swift` — Frontend networking layer for sending chat messages.
- `Growin/GrowinApp.swift` — Custom URL scheme routing listener.
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `mlx_vlm`: The backend already has `vlm_load` imported and dynamically handles loading via `mlx_vlm` if model name matches.
- `DeepLinkTicker` notification listener: `GrowinApp.swift` has built-in handlers for triggering internal navigation when custom scheme URL notifications are posted.

### Established Patterns
- `stream_chat_generator` (in `chat_routes.py`) returns a structured Server-Sent Event stream which can yield metadata block chunks.

### Integration Points
- Chat message model `ChatMessage` in `app_context.py` can be extended with an optional `images` array of Base64 strings.
- Intercepting links: `ThemeComponents.swift` contains the standard `MarkdownText` rendering loop where `.environment(\.openURL)` should be attached.
</code_context>

<specifics>
## Specific Ideas
- Gemma-4 MoE `26B-A4B-it` (MoE) VLM will be the default target for visual chart analysis.
</specifics>

<deferred>
## Deferred Ideas
- None — analysis stayed within phase scope.
</deferred>

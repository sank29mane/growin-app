# PLAN: Phase 2 — Frontend & Telemetry Integration

## Objective
Update the SwiftUI frontend to exploit the high-precision backend, implementing real-time streaming, observability UI for agent reasoning, and native financial charting.

## Wave 1: Core Networking & Model Alignment
<task type="auto" effort="medium">
  <name>Synchronize Swift Models</name>
  <files>Growin/CoreModels.swift, Growin/Models.swift</files>
  <action>
    Update Swift structs to match Backend v2.0.0 Pydantic models.
    INCLUDE: `MarketContext`, `GoalData` (with simulated paths), `PriceData`, `QuantData`.
    ENSURE: `Decimal` types are used for all currency fields to match backend precision.
  </action>
  <verify>Build the project in Xcode to ensure no model-mismatch errors.</verify>
  <done>Swift models are synchronized with Backend v2.0.0.</done>
</task>

<task type="auto" effort="medium">
  <name>Refactor Networking Clients</name>
  <files>Growin/MarketClient.swift, Growin/AgentClient.swift</files>
  <action>
    Implement `AsyncStream` support for SSE endpoints (`/api/chat/message`).
    Update existing REST calls to consume the refined `/portfolio/live` and `/api/goal/plan` endpoints.
  </action>
  <verify>Run tests/verify-networking.sh (or manual check of JSON parsing).</verify>
  <done>Clients support SSE streaming and new backend routes.</done>
</task>

## Wave 2: Chat & Observability UI
<task type="auto" effort="high">
  <name>Implement SSE Chat Streaming</name>
  <files>Growin/ViewModels/ChatViewModel.swift, Growin/Views/ChatView.swift</files>
  <action>
    Implement an SSE parser to handle token delivery.
    Update `ChatView` to render Markdown incrementally using `AttributedString`.
    Handle `meta` and `status` events from the stream to show "Planning..." vs "Thinking..." states.
  </action>
  <verify>Manual verification of typing-effect in chat.</verify>
  <done>Chat supports real-time token streaming with status indicators.</done>
</task>

<task type="auto" effort="medium">
  <name>Create Reasoning Trace UI</name>
  <files>Growin/Views/ReasoningTraceView.swift</files>
  <action>
    Create a `DisclosureGroup` component to display inter-agent debates.
    Map `agents_executed` and `telemetry_trace` from `MarketContext`.
    COLOR-CODE: Quant=Blue, Sentiment=Red, Forecast=Green for visual distinction.
    STANDARDIZE: Define a Swift `ReasoningStep` model to parse structured telemetry logs consistently.
  </action>
  <verify>Expand an AI response bubble to see the reasoning chain.</verify>
  <done>Agent reasoning is observable via an expandable UI component.</done>
</task>

## Wave 3: Portfolio & Goal Visualization
<task type="auto" effort="high">
  <name>Build Swift Charts Visualization</name>
  <files>Growin/Views/PortfolioChartView.swift, Growin/ViewModels/PortfolioViewModel.swift</files>
  <action>
    Implement native `Swift Charts` for P&L tracking.
    OVERLAY: Real price history with AI-generated `ForecastData` predictions.
    USE: `@Observable` for smooth 120Hz updates.
  </action>
  <verify>Chart renders with both historical and future (forecast) data points.</verify>
  <done>High-fidelity financial charts integrated.</done>
</task>

<task type="checkpoint:human-verify" effort="medium">
  <name>Safety Gate & HITL Design</name>
  <files>Growin/Views/GoalPlannerView.swift</files>
  <action>
    Implement a native "Slide to Confirm" button for trade executions.
    Present agent-generated actions as proposals requiring explicit user approval.
  </action>
  <verify>Human review of the approval flow safety.</verify>
  <done>User oversight is mandated for financial executions.</done>
</task>

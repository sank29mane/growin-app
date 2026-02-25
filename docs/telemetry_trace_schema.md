# Telemetry Trace Schema (v1.0.0)

## Overview
This schema defines the real-time telemetry events emitted by the Growin Backend to power the **Intelligent Console** and **Real-time Neural Pathway** in the macOS SwiftUI application. Telemetry enables observability of agentic reasoning traces and system health.

## Transport
- **Protocol:** Server-Sent Events (SSE)
- **Endpoint:** `/api/chat/message` (Header: `Accept: text/event-stream`)
- **Event Name:** `telemetry`

## Event Structure
Each telemetry event is a JSON object wrapped in the SSE `data` field.

```json
{
  "sender": "CoordinatorAgent",
  "subject": "agent_started",
  "payload": {
    "agent": "QuantAgent",
    "ticker": "AAPL"
  },
  "timestamp": "2026-02-23T14:30:00Z"
}
```

### Core Fields
- `sender`: The component originating the event (e.g., `CoordinatorAgent`, `QuantAgent`).
- `subject`: The type of event.
- `payload`: Event-specific data dictionary.
- `timestamp`: ISO-8601 UTC timestamp.

## Event Subjects

### 1. `intent_classified`
Emitted by the `CoordinatorAgent` after analyzing the user query.
- **Payload:**
  - `intent`: The classified intent object (`type`, `needs`, `reason`).
  - `ticker`: The resolved primary ticker (normalized).

### 2. `agent_started`
Emitted when a specialist agent begins its analysis.
- **Payload:**
  - `agent`: Name of the specialist (e.g., `ForecastingAgent`).
  - `ticker`: The ticker being analyzed.

### 3. `agent_complete`
Emitted when a specialist agent finishes its analysis.
- **Payload:**
  - `agent`: Name of the specialist.
  - `success`: Boolean status.
  - `latency_ms`: Execution time in milliseconds.
  - `error`: Error message (if `success` is false).

### 4. `analysis_result` (Internal/Broadcast)
Emitted on the internal agent bus for cross-agent coordination.

## macOS Frontend Implementation
The macOS app (`IntelligentConsoleView.swift`) maps these events to the **Neural Pathway** diagram:
- `agent_started` -> Node state transitions to `working` (Pulse effect).
- `agent_complete` -> Node state transitions to `ready` or `error`.
- `intent_classified` -> Flow lines activate between the Coordinator and required specialists.

## Performance Impact
- **Latency:** Minimal (~5ms overhead per event).
- **CPU/Memory:** Telemetry processing is handled in background queues to ensure M4 Pro peak performance for LLM inference remains unaffected.

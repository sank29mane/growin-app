# Telemetry & Reasoning Trace Specification (SOTA 2026)

This document defines the interface for real-time observability and decision tracing in the Growin App.

## 1. SSE Event Types (`/api/chat/message`)

All events are delivered via Server-Sent Events (SSE). Payloads are JSON-encoded.

### `meta` Events
Used for lifecycle and session management.
- **`RUN_STARTED`**: Emitted when a new request begins processing.
  - Payload: `{"type": "RUN_STARTED", "conversation_id": "...", "request_id": "..."}`
- **`RUN_FINISHED`**: Emitted when processing is complete.
  - Payload: `{"type": "RUN_FINISHED", "status": "success", "metadata": {"tokens": 123}}`

### `token` Events
Incremental chunks of the final decision text.
- Payload: Raw text chunk string.

### `telemetry` Events (AG-UI Pattern)
Real-time status updates from the specialist swarm.
- **`STEP_STARTED`**: An agent has begun its task.
  - Payload: `{"type": "STEP_STARTED", "sender": "QuantAgent", "payload": {"ticker": "AAPL"}}`
- **`STEP_FINISHED`**: An agent has completed its task.
  - Payload: `{"type": "STEP_FINISHED", "sender": "QuantAgent", "payload": {"success": true}}`

### `error` Events
- **`RUN_ERROR`**: A terminal or transient error occurred.
  - Payload: `{"type": "RUN_ERROR", "message": "...", "recoverable": true, "retryAfterMs": 2000}`

---

## 2. Reasoning Trace API

### `GET /api/telemetry/trace/{request_id}`
Returns the granular history of all agent interactions for a single request.

**Example Response:**
```json
{
  "request_id": "...",
  "trace": [
    {
      "sender": "CoordinatorAgent",
      "subject": "agent_started",
      "payload": {"query_snippet": "Analyze Tesla"},
      "timestamp": "2026-02-25T..."
    },
    {
      "sender": "QuantAgent",
      "subject": "agent_started",
      "payload": {"ticker": "TSLA"},
      "timestamp": "..."
    }
  ],
  "count": 2
}
```

---

## 3. Audit Log Integrity

The backend maintains a tamper-evident audit log at `backend/data/audit.log`.
- **Standards**: RFC 8785 (Canonical JSON) + SHA-256.
- **Chaining**: Each entry contains a `previous_hash` linking it to the Genesis (64-zero) block.
- **Verification**: Can be verified using `scripts/verify_audit_log.py`.

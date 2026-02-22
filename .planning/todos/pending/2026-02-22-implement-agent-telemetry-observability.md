---
created: 2026-02-22T14:09:12.961Z
title: Implement Agent Telemetry & Observability
area: api
files:
  - backend/agents/base_agent.py
  - backend/agents/coordinator_agent.py
---

## Problem

Currently, we don't have a way to trace a query through the multi-agent chain (Coordinator -> Specialist -> Decision). We need to track `decision_id`, `model_version`, and `latency` for every agent hop to measure performance and debug failures in production. This is a 2026 SOTA best practice for multi-agent observability.

## Solution

Implement a standardized `TelemetryData` model in `BaseAgent` to track `decision_id`, `model_version`, and `latency` for every agent hop. Ensure `CoordinatorAgent` generates and passes a `correlation_id` (or `decision_id`) to trace requests end-to-end. Results should be recorded in a simple file-based or SQLite-based trace store for analysis.

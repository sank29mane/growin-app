# Phase 42: Model Performance Comparison Summary

## LIVE Server Benchmark Results (M4 Pro 48GB)

- **Server**: http://127.0.0.1:8001/v1/chat/completions
- **Avg TTFT**: 29.313s
- **Sequential TPS**: 0.8
- **Concurrent TPS**: 2.3

## Decision

The core inference engine will be served via this high-throughput API interface.

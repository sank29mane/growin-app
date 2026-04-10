# Phase 42: Model Performance Comparison Summary

## LIVE Server Benchmark Results (M4 Pro 48GB)

- **Server**: http://127.0.0.1:8000/v1/chat/completions
- **Avg TTFT**: 13.417s
- **Sequential TPS**: 26.2
- **Concurrent TPS**: 10.9

## Decision

The core inference engine will be served via this high-throughput API interface.

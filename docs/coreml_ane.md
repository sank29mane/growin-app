# Core ML ANE (On-device) Integration

This document outlines Growin App's implementation of Apple Neural Engine (ANE) on macOS for on-device inference via Core ML, integrated with a SwiftUI frontend and local IPC bridge.

## Current Implementation Status

### ✅ Completed (Phase A)
- **ANE Detection**: Auto-detects Apple Silicon and enables ANE by default.
- **TA-Lib Fix**: Resolved persistent import errors with proper linking and pure Python fallbacks.
- **Core ML Scaffold**: `CoreMLRunner` class with ANE compute unit prioritization.
- **IPC Bridge**: Unix domain socket scaffold for low-latency on-device communication.
- **QuantAgent Integration**: Falls back to pure Python indicators when TA-Lib unavailable.

### 🚧 In Progress
- **Core ML Model**: Placeholder at `models/coreml/forecast_small.mlmodel` (needs actual model conversion).
- **SwiftUI Integration**: Planned for Phase B - direct Core ML calls from macOS UI.

## ON-DEVICE PATH OVERVIEW
- **Compute Units**: `MLComputeUnits.ALL` prioritizes Neural Engine on Apple Silicon.
- **Model Types**: Lightweight indicators (RSI, MACD, BBANDS) and simple forecasting.
- **Data Flow**: SwiftUI → IPC Bridge → Core ML Runner → ANE Inference → Results.

## VALIDATION METRICS
- **Latency**: Target <500ms for indicator calculations on ANE.
- **Memory**: <1–2 GB resident on M1/M2.
- **Battery**: Profile energy usage; prefer ANE over CPU/GPU.
- **Fallback**: Robust CPU-based calculations when ANE unavailable.

## NEXT STEPS
- **Phase B**: Convert PyTorch models to Core ML (.mlmodel) and wire into IPC.
- **Phase C**: Full SwiftUI + ANE integration for mac-native experience.

6) REFERENCES & NOTES
- Core ML docs: https://developer.apple.com/machine-learning/core-ml/
- WWDC22/WWDC24 sessions on Core ML optimization
- Core ML Tools: https://coremltools.readme.io
- On-device LLMs: WWDC 2024 sessions on on-device LLMs (Mistral Core ML integration)

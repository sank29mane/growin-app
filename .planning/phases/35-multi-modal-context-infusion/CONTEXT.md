# Phase 35: Multi-Modal Context Infusion - Context

## Requirements
- **VLM-01**: Implement local Vision-Language Model (Qwen2.5-VL-7B) for chart analysis.
- **VLM-02**: Add a Vision Agent to the Multi-Agent Swarm.
- **VLM-03**: Integrate Visual Pattern confirmations into the DecisionAgent's context.

## Hardware Spec
- Apple Silicon (M-series).
- `mlx-vlm` framework with Metal acceleration.
- 4-bit Quantization for Qwen2.5-VL-7B to fit within unified memory limits.

## Risk Gates
- Coordinate Drift: VLM output coordinates must map correctly to the original image dimensions. (Use `mlx-vlm.utils.load_config`).
- Memory Overflow: VLM must be unloaded or dynamically managed if memory pressure gets too high, similar to `MLXInferenceEngine`.
- Resolution Loss: Need to avoid naive 224x224 downsampling which destroys candlestick wick data.

## Decisions
- Use `Qwen2.5-VL-7B-Instruct-4bit` (via `mlx-vlm`) as the primary VLM.
- Create `backend/mlx_vlm_engine.py` as a distinct engine wrapper to keep standard text generation (`mlx_engine.py`) clean.
- Create a dedicated `VisionAgent` that acts like other specialist agents and returns a standardized `VisionData` payload.
- `DecisionAgent` will digest this data as a "Visual Confidence Boost" during its synthesis phase.

## Deferred Ideas
- Real-time video stream analysis of the chart.
- Frontend swift integration (This phase is backend only; frontend will be done in a subsequent phase once the backend API is ready).

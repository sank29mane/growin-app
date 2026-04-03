# Phase 35: Multi-Modal Context Infusion - Research

**Researched:** 2026-03-14
**Domain:** Local Vision-Language Models (VLM) for Financial Chart Analysis
**Confidence:** HIGH

## Summary
Phase 35 focuses on integrating local Vision-Language Models (VLMs) to provide a "Visual Confidence Boost" to the DecisionAgent. By analyzing chart screenshots, the app can confirm structural patterns (Head & Shoulders, flags, trendline breaks) that quantitative indicators (RSI, MACD) might misinterpret in isolation. 

**Primary recommendation:** Use **Qwen2.5-VL-7B** (quantized via MLX) as the primary engine for high-precision pattern detection due to its dynamic resolution and absolute coordinate support.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `mlx-vlm` | Latest | VLM Inference | Native Metal acceleration for VLMs on Apple Silicon. |
| `Qwen2.5-VL-7B` | 4-bit/8-bit | Visual Reasoning | State-of-the-art for chart/document analysis with absolute pixel grounding. |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|--------------|
| `Moondream2` | Latest | Fast Triage | Use for low-latency "is this a chart?" checks or quick trend summaries. |
| `Pillow` | Latest | Image Pre-processing | Resizing and normalizing screenshots before VLM injection. |

**Installation:**
```bash
pip install mlx-vlm pillow
```

## Architecture Patterns

### Recommended Project Structure
```
backend/
├── mlx_vlm_engine.py    # New: VLM-specific wrapper for mlx-vlm
├── utils/
│   └── image_proc.py    # Screenshot normalization and coordinate mapping
└── agents/
    └── vision_agent.py   # New: Specialist for chart interpretation
```

### Pattern 1: Grounded Pattern Detection
**What:** Requesting the model to provide bounding boxes (`bbox_2d`) for specific patterns.
**When to use:** When confirming high-conviction trade setups.
**Example:**
```python
# Prompting Qwen2.5-VL for pattern grounding
prompt = "Locate the 'Head and Shoulders' pattern in this chart. Return as JSON: {\"bbox_2d\": [x1, y1, x2, y2], \"label\": \"head_and_shoulders\"}"
```

### Anti-Patterns to Avoid
- **Blind Resolution Downsampling:** Standard VLMs resize to 224x224, losing candle wick details. Use `min_pixels`/`max_pixels` configuration in `AutoProcessor`.
- **Normalized Coordinate Confusion:** Qwen2-VL (normalized 0-1000) vs Qwen2.5-VL (absolute pixels). Ensure mapping logic matches the model version.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Coordinate Mapping | Custom Resizer | `mlx-vlm.utils.load_config` | Handles aspect ratio and padding natively. |
| Pattern Recognition | OpenCV Template Matching | VLM (Qwen2.5-VL) | VLMs handle semantic variation better than pixel-matching. |

## Common Pitfalls

### Pitfall 1: Coordinate Drift
**What goes wrong:** Detected bounding boxes don't align with the original UI chart.
**Why it happens:** The model predicts coordinates based on its internal resized version (e.g., 1280x720) rather than the source screenshot.
**How to avoid:** Store the resize ratio and padding offsets during pre-processing to map VLM output back to SwiftUI coordinates.

## Code Examples

### VLM Inference Initialization (mlx-vlm)
```python
from mlx_vlm import load, generate
from mlx_vlm.utils import load_config

model_path = "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"
model, processor = load(model_path)
config = load_config(model_path)

# High-resolution chart processing
image = ["path/to/chart_screenshot.png"]
output = generate(model, processor, image, "Identify the primary trend and any reversal patterns.", verbose=False)
```

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | Pytest |
| Test Directory | `tests/vision/` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command |
|--------|----------|-----------|-------------------|
| VLM-01 | Detect Head & Shoulders | Integration | `pytest tests/vision/test_patterns.py -k "h_and_s"` |
| VLM-02 | Coordinate Accuracy | Unit | `pytest tests/vision/test_coords.py` |

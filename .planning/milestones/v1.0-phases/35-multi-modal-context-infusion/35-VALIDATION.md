# Phase 35 Validation: Multi-Modal Context Infusion

## 🎯 Verification Goals
Ensure the vision subsystem correctly identifies technical chart patterns and provides a reliable "Visual Confidence Boost" to the DecisionAgent without regressing system latency.

## 🧪 Automated Test Strategy
| Test Level | Scope | Command | Success Criteria |
|:--- |:--- |:--- |:--- |
| **Unit** | Image Preprocessing | `pytest tests/vision/test_image_proc.py` | Aspect ratio preserved, dimensions normalized. |
| **Unit** | VLM Inference | `pytest tests/vision/test_vlm_engine.py` | Qwen2.5-VL-7B loads and describes image. |
| **Integration** | Pattern Detection | `pytest tests/vision/test_patterns.py` | Accurate identification of Head & Shoulders/Flags. |
| **Integration** | Agent Coordination | `pytest tests/vision/test_vision_agent.py` | VisionAgent output correctly populates MarketContext. |

## 🏗️ Manual Verification Protocol
1. **Model Residency Test**: Verify VLM stays resident in memory (08:00-21:00) via `WorkerService` status.
2. **Coordinate Mapping Check**: Input a screenshot, detect a pattern, and visually confirm the bounding box maps back to the original chart pixels.
3. **End-to-End Trace**: Ask "Analyze the chart for AAPL" and verify the reasoning trace includes "Visual Pattern Confirmed: [Pattern Name]".

## ⚡ Performance Thresholds
- **VLM Load Time**: < 5s (pinned in RAM)
- **Pattern Detection Latency**: < 2s per image on M4 Pro.
- **Memory Overhead**: < 8GB (4-bit quantized).

## 🛑 Risk Gates
- **False Positive Gate**: If VLM confidence < 0.7, pattern must be marked as "Unconfirmed".
- **Hardware Contention Gate**: If GPU/NPU load > 90%, vision tasks are queued or skipped.
- **Latency Gate**: Total Decision cycle must remain < 10s including vision infusion.

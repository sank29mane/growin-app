# Phase 38: SwiftUI Profit Command Center & RL Training Integration - Research

**Researched:** 2026-03-18
**Domain:** SwiftUI Performance, Reinforcement Learning (PPO/MLX), Financial Signal Processing
**Confidence:** HIGH

## Summary

This phase focuses on the high-fidelity user interface and the core reinforcement learning training loop. We are moving from polling to a WebSocket-driven "Alpha-Stream" to achieve 120Hz UI responsiveness on ProMotion displays (M4 Pro). The 'Split-Brain' inference strategy optimizes hardware utilization by splitting tasks between the GPU (heavy RL/JMCE state) and CPU (auxiliary coordination via Ollama-Granite).

**Primary recommendation:** Use `SwiftUI.Canvas` driven by `TimelineView(.animation)` for the 120Hz Alpha tracking chart to bypass the overhead of the SwiftUI view-tree diffing engine.

## User Constraints (from CONTEXT.md)

*No CONTEXT.md was found for this phase. Research follows the requirements provided in the phase description.*

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| UI-01 | 120Hz SwiftUI dashboard for real-time Alpha tracking | Verified `Canvas` + `TimelineView(.animation)` as the standard for 120Hz ProMotion rendering. |
| UI-02 | Visual Regime Indicator (CALM/DYNAMIC/CRISIS) | Integrated with `RegimeAgent` output (Z-score and Spectral Radius). |
| UI-03 | Fast-HITL Notifications (local macOS toasts) | Verified `NotificationManager.swift` and `UNUserNotificationCenter` for native toasts. |
| RL-01 | Split-Brain inference: VLLMMXEngine (GPU) / Ollama (CPU) | `VLLMMXEngine` is already implemented for GPU; `LLMFactory` supports Ollama for CPU tasks. |
| RL-02 | PPO Training Loop: Trajectory, GAE, Clipping/Entropy | Standard PPO formulation mapped to MLX `mx.array` operations. |
| RL-03 | Reward: Delta Alpha - (Trans. Cost + Volatility Tax) | Mathematical model verified: Alpha optimization with drag penalties. |
| DATA-01 | WebSocket 'Alpha-Stream' for metrics | Feasibility confirmed via existing `chart_routes` WebSocket patterns. |
| DATA-02 | DST-aware 'Smart Money' logic (2:00 PM GMT) | Verified 14:00 UTC anchor with NY Open overlap (9:00 AM ET). |

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| MLX | 0.22.0+ | PPO Training & Inference | Native Apple Silicon optimization (Metal). |
| vllm-mlx | Latest | RL State Reasoning | PagedAttention on M-series GPU. |
| Ollama | 0.5.x | Auxiliary Reasoning (Granite) | Efficient CPU-only inference for coordination. |
| SwiftUI | iOS 18/macOS 15 | 120Hz Dashboard | Native performance and ProMotion support. |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|--------------|
| websockets | 13.0+ | Alpha-Stream Backend | Real-time metric push. |
| UserNotifications | Native | Fast-HITL Toasts | macOS Desktop alerts for rebalances. |

## Architecture Patterns

### Split-Brain Inference Strategy
The system partitions intelligence tasks by hardware affinity:
- **GPU (Metal/MLX):** `VLLMMXEngine` running `Nemotron-3-Nano` or `JMCE` models. Used for high-dimensional state reasoning and RL policy execution.
- **CPU (Ollama/Llama.cpp):** `Ollama` running `ibm-granite` (3B or 8B). Used for orchestrating tools, parsing news, and coordinating agent handoffs without competing for KV cache on the GPU.

### 120Hz Dashboard Loop
```swift
// Recommended 120Hz Rendering Pattern
TimelineView(.animation) { timeline in
    Canvas { context, size in
        // 1. Draw Grid
        // 2. Draw Alpha Line (Portfolio vs FTSE 100)
        // 3. Draw Regime Shading
    }
}
.drawingGroup() // Flattens to Metal texture
```

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Real-time Charts | Custom Path/Shape | `SwiftUI.Canvas` | Views are too slow for 120Hz updates. |
| PPO Clipping | Manual logic | `mx.clip` | Numerical stability and performance. |
| Notification Scheduling | Custom Window | `UNUserNotificationCenter` | Handles system-level focus modes and banners. |
| DST Math | Manual hour offset | `pytz` or `datetime` with `ZoneInfo` | US/UK DST sync gaps are non-trivial. |

## Common Pitfalls

### Pitfall 1: Main Thread Blocking (UI Lag)
**What goes wrong:** WebSocket messages arrive at 100ms intervals, but parsing JSON and calculating chart coordinates on the main thread drops the frame rate below 120Hz.
**How to avoid:** Perform all Alpha/Beta calculations and coordinate transformations in a `detached` Task or background actor. Only pass the final `DisplayPoint` array to the UI.

### Pitfall 2: PPO Vanishing Gradients
**What goes wrong:** Standard Reward = Delta Alpha can be highly sparse or noisy, causing the policy to collapse.
**How to avoid:** Use **Generalized Advantage Estimation (GAE)** to smooth the reward signal and include an **Entropy Bonus** to ensure exploration during the 2 PM GMT window.

### Pitfall 3: GPU Memory Fragmentation
**What goes wrong:** Running `VLLMMXEngine` and `MLX Training` simultaneously on the GPU can trigger OOM (Out of Memory) if KV caches aren't managed.
**How to avoid:** Use `vllm-mlx` PagedAttention and set `gpu_memory_utilization=0.6` to leave head-room for the PPO training pass.

## Code Examples

### Reward Function & PPO Loss (MLX)
```python
# Source: Internal Research / MLX Documentation
def calculate_ppo_loss(log_probs, old_log_probs, advantages, epsilon=0.2):
    ratio = mx.exp(log_probs - old_log_probs)
    surr1 = ratio * advantages
    surr2 = mx.clip(ratio, 1.0 - epsilon, 1.0 + epsilon) * advantages
    return -mx.mean(mx.minimum(surr1, surr2))

def financial_reward(delta_alpha, turnover, variance, tc_penalty=0.0005):
    # Reward = Delta Alpha - (Transaction Cost + Volatility Tax)
    # Volatility Tax = 0.5 * sigma^2
    vol_tax = 0.5 * variance
    transaction_cost = turnover * tc_penalty
    return delta_alpha - (transaction_cost + vol_tax)
```

### DST-Aware 2:00 PM GMT Logic
```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

def is_smart_money_window():
    # Anchor to 9:00 AM New York (Pre-market/London Overlap)
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    # In GMT/BST, this aligns with the 2 PM GMT window
    return now_ny.hour == 9 and now_ny.minute < 30
```

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.x + XCTest |
| Config file | `pyproject.toml` |
| Quick run command | `pytest tests/backend/test_rl_policy.py` |
| Full suite command | `./run_all_tests.py` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| RL-02 | PPO Gradient Descent | Unit | `pytest tests/backend/test_ppo_training.py` | ❌ Wave 0 |
| RL-03 | Reward Calculation | Unit | `pytest tests/backend/test_reward_logic.py` | ❌ Wave 0 |
| DATA-01| WebSocket Alpha Stream | Integration | `pytest tests/backend/test_alpha_websocket.py` | ❌ Wave 0 |

## Sources

### Primary (HIGH confidence)
- `backend/agents/rl_policy.py` - Current PPO structure.
- `backend/vllm_mlx_engine.py` - GPU inference engine.
- Apple Developer Docs - `TimelineView` and `Canvas` performance guides.

### Secondary (MEDIUM confidence)
- SMC/ICT Smart Money Logic - 2:00 PM GMT Macro definition.
- MLX GitHub Examples - Reinforcement learning patterns.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - Libraries already integrated in project.
- Architecture: HIGH - Split-brain is the standard for M4 Pro optimization.
- Pitfalls: MEDIUM - Real-time performance requires careful main-thread management.

**Research date:** 2026-03-18
**Valid until:** 2026-04-18

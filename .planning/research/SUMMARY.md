# Project Research Summary

**Project:** Growin App v5.0
**Domain:** macOS Native AI/ML & Financial Intelligence
**Researched:** 2026-04-10
**Confidence:** HIGH

## Executive Summary

The research for Growin App v5.0 identifies a shift towards a hardware-optimized, sovereign AI experience on macOS 2026. The core of this evolution is the integration of the **Gemma 4 26B A4B Mixture-of-Experts (MoE)** model, served locally via **vllm-mlx** with PagedAttention to ensure low-latency, high-throughput agentic workflows on M4 Pro/Max hardware. This transition is supported by a "Sovereign Ledger" UI aesthetic built on SwiftUI SDK 17+, emphasizing 0px primitives, radical asymmetry, and 120Hz responsiveness.

The recommended approach leverages **Unsloth/mlx-tune** for local LoRA fine-tuning, allowing the model to adapt to specific market regimes and extract unique alpha signals. The primary technical challenge lies in managing the 48GB unified memory ceiling of the M4 Pro, requiring strict quantization strategies (Q4 weights, FP8 KV-cache) and system-level memory limit overrides to avoid OOM crashes during concurrent serving and training.

Key risks include **Expert Collapse** during MoE fine-tuning, **VRAM exhaustion**, and **Temporal Data Leakage** in alpha extraction. These are mitigated through hardware-aware serving configurations, auxiliary loss monitoring in training, and strict chronological data validation.

## Key Findings

### Recommended Stack

The stack is optimized for maximum performance on Apple Silicon, prioritizing local GPU/ANE acceleration over cloud-based alternatives.

**Core technologies:**
- **SwiftUI SDK 17+**: macOS 2026 UX — Native support for Stage Manager 2.0, "Liquid Glass" materials, and 120Hz ProMotion.
- **Gemma 4 26B A4B (MoE)**: Primary Intelligence — SOTA 26.8B MoE model (4B active) providing deep reasoning with hardware-efficient inference.
- **vllm-mlx (v0.19.0+)**: High-Throughput Serving — Native PagedAttention and continuous batching on Apple Silicon GPU for concurrent MAS tasks.
- **Unsloth / mlx-tune**: Local Fine-Tuning — 2x faster LoRA/QLoRA training on Mac with significantly reduced memory footprint.

### Expected Features

The feature landscape balances macOS platform expectations with unique agentic capabilities.

**Must have (table stakes):**
- **Stage Manager 2.0 / Multi-Window Tiling** — users expect seamless integration with macOS window management.
- **Real-time Price Sync (<16ms)** — essential for 120Hz display fidelity.
- **Agentic Sidebar** — proactive AI-driven window and context management.

**Should have (competitive):**
- **MoE "Thinking" Mode** — transparent reasoning steps exposed in the chat UI.
- **Sovereign Ledger (0px) UI** — radical asymmetric design for professional data density.
- **Alpha Feature Evolution** — self-improving predictive signals using Unsloth-tuned models.

**Defer (v2+):**
- **VisionOS Pro Integration** — spatial trading command center.
- **Decentralized Agent Swarms** — P2P cross-device agent coordination.

### Architecture Approach

A decoupled Multi-Agent System (MAS) where specialized agents communicate via a FastAPI Orchestrator, backed by high-speed local inference and analytical storage.

**Major components:**
1. **vllm-mlx Serving Node** — High-throughput inference with PagedAttention and dynamic adapter loading.
2. **Unsloth LoRA Engine** — Asynchronous fine-tuning loop for regime adaptation and alpha extraction.
3. **Sovereign UI (SwiftUI 6+)** — Multi-window, 120Hz interface using `@Observable` domain stores for fluid performance.

### Critical Pitfalls

1. **VRAM / Unified Memory Ceiling** — Avoid by using `sysctl` memory overrides and FP8 KV-cache quantization.
2. **Expert Routing Collapse** — Avoid by monitoring auxiliary loss and using 16-bit LoRA for routing precision.
3. **Temporal Data Leakage (Look-ahead Bias)** — Avoid by enforcing strict chronological splits in fine-tuning datasets.
4. **"Thinking" Mode Token Spiraling** — Avoid with strict token limits and prompt compression for reasoning chains.

## Implications for Roadmap

### Phase 1: Local Intelligence & Serving
**Rationale:** Establishing stable local inference is the prerequisite for all other features.
**Delivers:** Gemma 4 MoE serving via vllm-mlx, PagedAttention integration, and Thinking Mode UI.
**Addresses:** Gemma 4 MoE Integration, vllm-mlx Serving.
**Avoids:** VRAM OOM (via early hardware-aware config), Thinking Token Spiraling.

### Phase 2: Sovereign UX & Stage Manager
**Rationale:** Redefines the user interaction layer to match 2026 standards before adding advanced agents.
**Delivers:** SwiftUI 17+ 0px Ledger UI, Stage Manager 2.0 integration, and Agentic Sidebar.
**Addresses:** macOS 2026 UX Redesign, Multi-Window Tiling.
**Avoids:** Cognitive Flooding (via Liquid Glass layering).

### Phase 3: Adaptive Alpha & Fine-Tuning
**Rationale:** Builds on stable serving and UI to deliver self-improving predictive power.
**Delivers:** Unsloth fine-tuning pipeline, automated alpha extraction, and dynamic LoRA switching.
**Addresses:** Alpha Feature Evolution, Strategy Calibration.
**Avoids:** Expert Collapse, Look-ahead Bias.

### Phase Ordering Rationale

- **Inference First:** Local serving stability (Phase 1) is the "engine" of the app; without it, the UI (Phase 2) is hollow.
- **UI Before Complex Agents:** The "Sovereign Ledger" (Phase 2) provides the data density needed to visualize the complex alpha signals developed in Phase 3.
- **Hardware Safety:** Early focus on vllm-mlx (Phase 1) ensures we address the VRAM ceiling before stacking training loads (Phase 3).

### Research Flags

- **Phase 1:** High priority on `vllm-mlx` PagedAttention benchmarks for M4 Pro.
- **Phase 3:** Deep research needed on "Auxiliary Loss" tuning in Unsloth for MoE models to prevent collapse.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | MLX/vLLM ecosystem is stable on Apple Silicon. |
| Features | HIGH | Aligned with macOS 2026 Tahoe previews and internal DNA. |
| Architecture | HIGH | PagedAttention/MAS pattern is industry standard for 2026. |
| Pitfalls | HIGH | Well-documented failures in early MoE and MLX transitions. |

**Overall confidence:** HIGH

### Gaps to Address

- **M4 Pro specific VRAM limits:** Needs empirical validation of 8-bit MoE + 16K KV cache stability.
- **Stage Manager 2.0 API stability:** Monitor macOS Tahoe beta updates for potential navigation breaking changes.

## Sources

### Primary (HIGH confidence)
- `/waybarrios/vllm-mlx` — PagedAttention & MoE serving.
- `/unslothai/unsloth` — MoE fine-tuning & MLX optimizations.
- `apple.com/developer` — SwiftUI 2026 (Tahoe) guidelines.

### Secondary (MEDIUM confidence)
- `google.dev/gemma` — Gemma 4 Technical Specs.
- `MLX Community Discord` — M4 Pro/Max VRAM benchmarks.

---
*Research completed: 2026-04-10*
*Ready for roadmap: yes*

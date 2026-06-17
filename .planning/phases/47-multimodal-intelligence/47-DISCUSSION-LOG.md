# Phase 47: Multimodal Intelligence & Deep Integration - Discussion Log (Assumptions Mode)

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions captured in CONTEXT.md — this log preserves the analysis.

**Date:** 2026-06-17
**Phase:** 47-multimodal-intelligence
**Mode:** assumptions
**Areas analyzed:** Technical Approach for Image Upload & Transmission, VLM Hardware Execution & Feature Extraction, Real-Time Updates for Interactive Tiles, Deep Linking Protocol

## Assumptions Presented

### Technical Approach for Image Upload & Transmission
| Assumption | Confidence | Evidence |
|------------|-----------|----------|
| Base64-encoded inline strings will be used to transmit uploaded images/charts within the existing JSON ChatMessage payload. | Likely | `backend/routes/chat_routes.py`, `backend/app_context.py` |

### VLM Hardware Execution & Feature Extraction
| Assumption | Confidence | Evidence |
|------------|-----------|----------|
| Branch model loading and inference logic inside `mlx_engine.py` to use `mlx_vlm` instead of standard `mlx_lm` when running vision tasks. | Confident | Research on VLM compatibility in python MLX runtime |

### Real-Time Updates for Interactive Tiles
| Assumption | Confidence | Evidence |
|------------|-----------|----------|
| Bind SwiftUI Interactive Tiles in the chat panel to the existing `PortfolioSummaryObserver` or `DashboardViewModel` on the frontend, using the existing SSE stream metadata events. | Likely | `AIChatPanelView.swift`, `DashboardView.swift` |

### Deep Linking Protocol
| Assumption | Confidence | Evidence |
|------------|-----------|----------|
| Standardize custom scheme routing with local environment link interception (`.environment(\.openURL)`) to resolve the macOS text selection interception bug. | Confident | SwiftUI macOS behavior analysis, `ThemeComponents.swift`, `GrowinApp.swift` |

## Corrections Made
- No direct corrections. User accepted the proposed assumptions and requested Google/web research on Gemma-4 multimodal prompting best practices.

## External Research
- **mlx-vlm Library Compatibility**: Verified API patterns for vision inference (`mlx_vlm.load` returning model & processor, generating via `mlx_vlm.generate`).
- **SwiftUI 17+ onOpenURL**: Discovered that `.textSelection(.enabled)` blocks link clicks on macOS. Best practice is to intercept via `.environment(\.openURL)` returning `.handled`.
- **Gemma-4 Multimodal Prompting Guide**: Identified the standard dialogue structure (`<|turn>`), image placeholder (`<|image|>`), and Google's recommendation to place image tokens before the text prompt.

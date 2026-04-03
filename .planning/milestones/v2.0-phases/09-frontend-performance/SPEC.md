# SPEC.md - Phase 09: Frontend Performance & Design Foundation

Status: FINALIZED
Phase: 09

## Objective
Deliver a flagship-grade user experience by optimizing the iOS/macOS frontend for 120Hz smoothness, ensuring 100% accessibility compliance, and standardizing the "Palette" design system with premium fintech visual trends, including a "Trading 212 style" overhaul that prioritizes functional data density and emotional regulation.

## 1. 120Hz Optimization (FE-01)
- **Requirement**: Eliminate frame drops during scrolling and complex transitions in the Chat and Portfolio views, achieving consistent 120Hz performance on ProMotion displays.
- **Approach**: 
  - Profile using Xcode Instruments (Animation Hitches) to identify bottlenecks.
  - Implement `drawingGroup()` for complex glassmorphic effects and background elements (`MeshBackground`).
  - Introduce **Metal-backed SwiftUI Charts** via `UIViewRepresentable` for high-performance data visualization, utilizing dual-pipeline rendering (visuals on GPU, indicator math on GPU).
  - Ensure **Main-thread Isolation** by offloading data fetching, AI reasoning, and stream parsing to background actors.
  - Leverage **Optimistic State Management** with custom `CoreHaptics` for tactile feedback on key user actions.
- **Verification**: Smooth 120fps scrolling, chart interactions, and UI transitions on ProMotion displays (targeted at M4 Pro/Max iPad/Mac) validated via Xcode Instruments.

## 2. Accessibility Deep Dive (FE-02)
- **Requirement**: Achieve full VoiceOver/ScreenReader support for all UI elements, particularly the new Portfolio charts and Agentic Reasoning Trace, including high-contrast mode compliance.
- **Features**:
  - **Accessible Chart Data Representation**: Provide tabular or descriptive fallbacks for complex chart data.
  - **Correct Focus Order**: Ensure logical focus order for all interactive elements, especially within the "Debate" and Reasoning Trace UIs.
  - **High-Contrast Mode Verification**: All components and color palettes (`Stitch` palette) must maintain readability and visual integrity in high-contrast settings.

## 3. Standardized Design Foundation (FE-04)
- **Requirement**: Extract repeated UI patterns into a unified `Palette` component library, incorporating "Liquid Glass" design language, "Calm UI" principles, and "Bento grid" modularity, while preserving functional data density.
- **Components**: `GlassCard` (enhanced with Liquid Glass effects), `PremiumButton`, `AgentStatusBadge`, `FinancialMetricView`, and new modular UI blocks for the "Bento Grid."
- **Consistency**: All views must use the canonical Stitch palette, guided by `docs/GSD-STYLE.md`, and adhere to "Calm UI" transitions to manage user emotional states.
- **Architecture**: Implement **Bento grid** layouts for dashboards to present diverse content with clear visual hierarchy, ensuring no sacrifice of data precision for aesthetics.

## 4. Reasoning Trace UI (Frontend)
- **Requirement**: Build frontend components to consume the `/api/telemetry/trace/{id}` endpoint, displaying the agent consultation process with "Confidence Visualization Patterns" and progressive disclosure.
- **Feature**: A "Thinking..." expandable view showing step-by-step agent consultation with real-time status updates via SSE, incorporating:
  - **Confidence Visualization Patterns (CVP)**: Visually represent AI confidence levels (e.g., strong indicators for high confidence, probability clouds for lower confidence).
  - **Progressive Disclosure**: Allow users to expand details of agent reasoning only when needed to manage cognitive load.

## Acceptance Criteria
- [x] No hitches detected during rapid scrolling, chart interaction, or UI transitions, maintaining 120fps.
- [x] VoiceOver can read out portfolio performance, individual agent status, and provide meaningful chart data fallbacks.
- [x] All primary UI elements are derived from centralized `Palette` components, demonstrating "Liquid Glass" effects and "Calm UI" transitions.
- [x] Dashboard layouts effectively utilize the "Bento grid" architecture, displaying financial data with clarity and precision.
- [x] Users can see a live "Reasoning Trace" with clear "Confidence Visualization Patterns" while waiting for a decision.

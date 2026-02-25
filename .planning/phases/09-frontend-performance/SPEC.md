# SPEC.md - Phase 09: Frontend Performance & Design Foundation

Status: DRAFT
Phase: 09

## Objective
Deliver a flagship-grade user experience by optimizing the iOS/macOS frontend for 120Hz smoothness, ensuring 100% accessibility compliance, and standardizing the "Palette" design system.

## 1. 120Hz Optimization (FE-01)
- **Requirement**: Eliminate frame drops during scrolling and complex transitions in the Chat and Portfolio views.
- **Approach**: 
  - Profile using Xcode Instruments (Animation Hitches).
  - Implement `drawingGroup()` for complex glassmorphic effects where beneficial.
  - Optimize `MeshBackground` performance to ensure it doesn't starve the main thread.
- **Verification**: Smooth 120fps scrolling on ProMotion displays (targeted at M4 Pro/Max iPad/Mac).

## 2. Accessibility Deep Dive (FE-02)
- **Requirement**: Full VoiceOver/ScreenReader support for the new Portfolio charts and Agentic Reasoning Trace.
- **Features**:
  - Accessible Chart data representation (tabular or descriptive fallbacks).
  - Correct focus order for the "Debate" UI.
  - High-contrast mode verification for the Stitch color palette.

## 3. Standardized Design Foundation (FE-04)
- **Requirement**: Extract repeated UI patterns into a unified `Palette` component library.
- **Components**: `GlassCard`, `PremiumButton`, `AgentStatusBadge`, and `FinancialMetricView`.
- **Consistency**: Ensure all views use the canonical Stitch palette defined in `docs/GSD-STYLE.md`.

## 4. Reasoning Trace UI (Frontend)
- **Requirement**: Build the frontend components to consume the `/api/telemetry/trace/{id}` endpoint created in Phase 08.
- **Feature**: A "Thinking..." expandable view that shows the step-by-step agent consultation process with real-time status updates via SSE.

## Acceptance Criteria
- [ ] No hitches detected during rapid scrolling in Portfolio or Chat.
- [ ] VoiceOver can read out portfolio performance and individual agent status.
- [ ] All primary UI elements are derived from centralized `Palette` components.
- [ ] Users can see a live "Reasoning Trace" while waiting for a decision.

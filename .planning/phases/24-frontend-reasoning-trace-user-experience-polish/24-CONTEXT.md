# PHASE 24 CONTEXT: Frontend Reasoning Trace & User Experience Polish

This document codifies the functional and behavioral decisions for Phase 24, providing clear guidance for research, planning, and execution agents.

---

## 1. STRATEGIC UI FOCUS: NATIVE macOS EFFICIENCY
*   **Platform Mandate**: Optimized strictly for **macOS (AppKit/SwiftUI)**. No iOS-legacy UI patterns.
*   **Target Display**: 120Hz ProMotion smoothness using `.equatable()` and optimized redraw logic.
*   **Development Mode**: Treat all logs and traces as permanent `[DEV]` audit data for system improvement.

## 2. INTERACTION & DENSITY: THE AGENTIC TRACE
*   **Lifecycle**:
    *   **Auto-Collapse**: On message delivery, the full trace collapses into a single "Intelligence Verified" chip.
    *   **Expandable Grid**: Long traces (3+ steps) show a "+N more" counter. Clicking the counter expands the row into a full **Vertical Grid** for detailed scannability.
*   **Adversarial Visuals (ACE)**:
    *   **The Conflict Stack**: Debating agents stack vertically on the same step.
    *   **Winner Feedback**: Use Split-Chip colors (Red/Green) with a sharp "!" static conflict icon.
    *   **Coordinator Pulse**: The Coordinator chip uses a refined "Breathe" pulse while resolving conflicts.

## 3. DATA & PERSISTENCE: THE AUDIT TRAIL
*   **Storage**: Save every trace step in the SQLite `chat_history` table.
*   **Schema Fields**: 
    *   `agent_name`, `action_type`, `content`, `timestamp`.
    *   `model_id` (e.g., Mistral-7B).
    *   `provider` (e.g., LM Studio, Anthropic, Google).
*   **History Visibility**: Important decisions (trades/strategies) show the full trace in history by default; others show the collapsed chip.

## 4. ACCESSIBILITY: macOS STANDARDS
*   **VoiceOver**:
    *   **Cumulative Summary**: Announce reasoning progress every 2 seconds rather than every single event.
    *   **Focus Container**: The trace row is a swipe-into container for manual step inspection.
*   **Motion Accessibility**: 
    *   Replace pulsing "Active" glows with solid high-contrast borders when **Reduce Motion** is enabled.

## 5. VISUAL POLISH: MOTION & SHADERS
*   **Transition Style**: Use high-velocity vertical "Slot Machine" animations for chip entries.
*   **Icon Animation**: Utilize macOS 14+ SF Symbol animations (`variable color` and `pulse` effects).
*   **NPU Indicator**: Refined "Breathe" pulse (scale/opacity) during local calculation steps.

---

## DEFERRED IDEAS (For Phase 25+)
*   Visual "Replay" mode for historical traces.
*   Metal-based particle fields for heavy MLX computations.
*   Reasoning Inspector Sidebar (Xcode style).

---

## NEXT STEPS
1.  **Research**: Verify macOS 14+ SF Symbol animation syntax and `accessibilityLiveRegion` behavior.
2.  **Planning**: Detail the `ReasoningStep` schema update for SQLite.
3.  **Execution**: Implement the "Slot Machine" transition and the Grid expansion logic.

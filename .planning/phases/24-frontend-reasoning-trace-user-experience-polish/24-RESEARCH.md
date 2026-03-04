# PHASE 24 RESEARCH: Frontend Reasoning Trace & User Experience Polish

Updated with deep technical insights from Growin Research Notebook (NotebookLM).

## 1. SF Symbol Animations (macOS 14+)
The `.symbolEffect` modifier is the native way to animate symbols on macOS 14+.

### Key Effects for Reasoning Trace:
*   **Indefinite Effects (Continuous)**: 
    *   `.symbolEffect(.variableColor.iterative, isActive: isThinking)`: Cycles colors through symbol layers while processing.
    *   `.symbolEffect(.pulse, isActive: isThinking)`: Continuous pulsing for active agents.
*   **Discrete Effects (Event-Triggered)**:
    *   `.symbolEffect(.bounce.down.byLayer, value: thoughtCount)`: Bounces sequentially by layer when a new thought arrives.
*   **Content Transition**: Use `.contentTransition(.symbolEffect(.replace))` when swapping icons (e.g., "Thinking" to "Success").

## 2. Accessibility & Streaming (macOS VoiceOver)
### Mechanisms:
*   **`accessibilityLiveRegion(.polite)`**: Best for non-critical streaming data. VoiceOver waits for current speech to finish.
*   **`.updatesFrequently` Trait**: For high-velocity streams (e.g., token-per-second). Tells VoiceOver to poll rather than push every update, preventing "VoiceOver flooding."
*   **Throttling**: Decouple visual stream from announcements. Use a hidden buffer or summarized readout for VoiceOver using `AccessibilityNotification`.

## 3. "Slot Machine" Vertical Transition
To achieve a physical, kinetic feel:
*   **Asymmetric Transition**: Combine vertical offsets and opacity.
    ```swift
    .asymmetric(
        insertion: .modifier(active: SlotModifier(offset: 20, opacity: 0), identity: SlotModifier(offset: 0, opacity: 1)),
        removal: .modifier(active: SlotModifier(offset: -20, opacity: 0), identity: SlotModifier(offset: 0, opacity: 1))
    )
    ```
*   **`scrollTransition` API**: Use the macOS 14 API for symmetrical roll-in/roll-out effects based on transition phase (`-1.0` to `1.0`).
*   **Numeric Transitions**: Use `.contentTransition(.numericText())` for rolling confidence scores or digits.

## 4. 120Hz/ProMotion Optimization
To maintain the **8.3ms** frame budget:
*   **`@Equatable` Macro & `.equatable()`**: Force SwiftUI to skip reflection-based diffing. Essential as the reasoning log grows.
*   **macOS Frame Rates**: `CADisableMinimumFrameDurationOnPhone` is NOT required for macOS; it scales automatically.
*   **Core Animation Hinting**: For custom visualizers (Canvas), set `preferredFrameRateRange` (min: 80, max: 120, preferred: 120) to prevent throttling.

## 5. Persistence & Schema
*   **ReasoningStep**: Conforms to `Codable`, `Sendable`, and `Identifiable`.
*   **Persistence**: Save `activeReasoningSteps` to `ChatMessageModel` upon completion of the stream.
